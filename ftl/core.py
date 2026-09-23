"""合成 FTL 核心：页映射、断电安全提交、垃圾整理与磨损管理。

设计要点（与 README 一致）：
- 逻辑页覆盖写 = 写新数据页 + 写一条带 CRC32 的映射提交记录；
  提交记录整页写成功后才可见（设备保证页写原子，掉电只发生在动作边界）。
- 提交记录为整映像快照，单页 32 字节：magic(1) + seq(3, 小端)
  + mapping(24) + crc32(4)。因此逻辑页数固定为 24。
- 恢复时扫描全部物理页，取序列号最大（同号取物理页号大者）的
  完整提交记录重建映射；未提交的数据页/残缺记录一律视为死页。
- 垃圾整理：先把受害块内仍有效的页搬迁他处并提交新映射，
  提交成功后才擦除受害块——掉电安全。若受害块含有当前最新提交记录，
  擦除前强制先写一条新提交记录（即使无需搬迁数据页）。
- GC 保留页：为用户态分配保留 1 块（8 页）余量，保证任何单块回收的
  搬迁（最多 7 个有效页 + 1 条提交记录）总能容纳；保留页不可被用户写耗尽。
- 分配按（擦除次数升序, 空白页数降序, 块号升序）选块，实现静态磨损均衡；
  坏块与磨损块（擦除次数达上限）永不分配、永不作为整理对象。
"""
from __future__ import annotations

import zlib

from .device import BLOCKS, PAGE_SIZE, PAGES_PER_BLOCK, TOTAL_PAGES, RawNand
from .errors import (
    BadBlockError,
    BlockWornError,
    FTLError,
    GCBudgetExhaustedError,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
    TransactionError,
)
from .validation import INT_CAP, require_bytes, require_int

LOGICAL_PAGES = 24
GC_RESERVE_PAGES = PAGES_PER_BLOCK  # 为用户分配保留 1 块，保障 GC 搬迁有处可去
TRANSACTION_MAX_PAGES = 4  # 单笔原子事务最多覆盖的不同逻辑页数
_UNMAPPED = 0xFF
_COMMIT_MAGIC = 0xA5
_SEQ_MAX = 0xFFFFFF  # 24 位序列号空间
_DEFAULT_GC_BUDGET = 2 * BLOCKS  # 单次写操作默认的 GC 擦除预算
_MAX_GC_DEPTH = 4 * BLOCKS  # GC 嵌套深度保险上限


# ----------------------------------------------------------------------
# 提交记录编解码（公开：格式即契约）
# ----------------------------------------------------------------------
def encode_commit_record(seq, mapping):
    """把 (序列号, 24 项映射) 编码为 32 字节提交记录页。"""
    seq = require_int("seq", seq, 0, _SEQ_MAX)
    if len(mapping) != LOGICAL_PAGES:
        raise InvalidInputError(f"mapping 必须有 {LOGICAL_PAGES} 项")
    entries = []
    for x in mapping:
        x = require_int("mapping 项", x, 0, _UNMAPPED)
        if x != _UNMAPPED and x >= TOTAL_PAGES:
            raise InvalidInputError(f"mapping 项 {x} 超出物理页范围 [0, {TOTAL_PAGES})")
        entries.append(x)
    body = bytes([_COMMIT_MAGIC]) + seq.to_bytes(3, "little") + bytes(entries)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + crc.to_bytes(4, "little")


def decode_commit_record(page):
    """解析 32 字节物理页；是完整合法提交记录则返回 (seq, mapping)，否则 None。

    结构校验：magic、CRC32、映射项合法且互不重复。
    """
    if len(page) != PAGE_SIZE or page[0] != _COMMIT_MAGIC:
        return None
    body = page[:28]
    if (zlib.crc32(body) & 0xFFFFFFFF) != int.from_bytes(page[28:32], "little"):
        return None
    seq = int.from_bytes(page[1:4], "little")
    mapping = list(page[4:28])
    seen = set()
    for ppn in mapping:
        if ppn == _UNMAPPED:
            continue
        if ppn >= TOTAL_PAGES or ppn in seen:
            return None
        seen.add(ppn)
    return seq, mapping


class _BlockView:
    __slots__ = ("blanks", "dead", "valid", "pinned", "commit")

    def __init__(self):
        self.blanks = 0  # 空白页数
        self.dead = 0  # 死页数（可经擦除回收）
        self.valid = 0  # 当前映射引用的有效数据页数
        self.pinned = 0  # 进行中的操作占用的页数（不可动）
        self.commit = 0  # 当前最新提交记录页（不可回收）


class FTL:
    """闪存翻译层。挂载即恢复：扫描设备，取最后一条完整提交记录。"""

    def __init__(self, device, *, gc_budget=None):
        if not isinstance(device, RawNand):
            raise InvalidInputError(
                f"device 必须是 RawNand，得到 {type(device).__name__}"
            )
        if device.crashed:
            raise PowerLossError("设备处于掉电冻结态：请先 device.reboot() 再挂载")
        self._dev = device
        self._gc_budget = (
            _DEFAULT_GC_BUDGET
            if gc_budget is None
            else require_int("gc_budget", gc_budget, 0, INT_CAP)
        )
        self._mapping = [_UNMAPPED] * LOGICAL_PAGES
        self._seq = 0
        self._commit_ppn: int | None = None  # 最新提交记录所在物理页
        self._logical_log: list[dict] = []
        self._pinned: set[int] = set()
        self._view = [_BlockView() for _ in range(BLOCKS)]
        self._gc_spent = 0
        # 事务状态：None 表示无进行中事务；否则为 {lpn: data} 的暂存区。
        # 暂存只存在于内存——断电时随主机内存丢失，闪存上不会留下事务痕迹，
        # 因此恢复后必然无进行中事务。
        self._txn: dict[int, bytes] | None = None
        self._mount()

    # ------------------------------------------------------------------
    # 挂载 / 视图
    # ------------------------------------------------------------------
    def _mount(self):
        best = None  # (seq, ppn, mapping)
        for b in range(BLOCKS):
            if self._dev.is_bad_block(b):
                continue
            for p in range(PAGES_PER_BLOCK):
                raw = self._dev.read_page(b, p)
                if raw[0] != _COMMIT_MAGIC:
                    continue
                rec = decode_commit_record(raw)
                if rec is None:
                    continue
                seq, mapping = rec
                ppn = b * PAGES_PER_BLOCK + p
                if ppn in mapping:
                    continue  # 提交记录不得引用自身所在页
                if best is None or (seq, ppn) > (best[0], best[1]):
                    best = (seq, ppn, mapping)
        if best is not None:
            self._seq = best[0]
            self._commit_ppn = best[1]
            self._mapping = list(best[2])
        self._rebuild_view()

    def _rebuild_view(self):
        dev = self._dev
        valid = {p for p in self._mapping if p != _UNMAPPED}
        views = [_BlockView() for _ in range(BLOCKS)]
        for b in range(BLOCKS):
            if dev.is_bad_block(b):
                continue
            v = views[b]
            for p in range(PAGES_PER_BLOCK):
                ppn = b * PAGES_PER_BLOCK + p
                if ppn in self._pinned:
                    v.pinned += 1
                elif ppn in valid:
                    # 映射引用的页恒为有效页：全 0xFF 的数据页与空白页
                    # 物理上不可区分，必须以映射（元数据）为准，否则会被
                    # 误当空白页重复分配（已知陷阱，见 README 设计取舍）
                    v.valid += 1
                elif ppn == self._commit_ppn:
                    v.commit += 1
                elif dev.is_blank(b, p):
                    v.blanks += 1
                else:
                    v.dead += 1
        self._view = views

    def _total_blanks(self):
        return sum(v.blanks for v in self._view)

    # ------------------------------------------------------------------
    # 公开逻辑接口
    # ------------------------------------------------------------------
    def write(self, lpn, data, *, gc_budget=None):
        """覆盖写逻辑页：新数据页 + 提交记录。失败不改变已提交逻辑映像。

        物理层可能留下未提交的孤儿页（属已说明的部分变更，GC 会回收）。
        事务进行中禁止调用本方法（请用事务接口或先 abort_transaction）。
        """
        lpn = require_int("lpn", lpn, 0, LOGICAL_PAGES - 1)
        data = require_bytes("data", data, min_len=1, max_len=PAGE_SIZE)
        if self._txn is not None:
            raise TransactionError(
                "事务进行中不能调用 write()：请先 commit_transaction/abort_transaction"
            )
        budget = self._resolve_budget(gc_budget)
        self._gc_spent = 0
        self._rebuild_view()
        try:
            data_ppn = self._ensure_free_page(set(), 0, budget, GC_RESERVE_PAGES)
            self._pinned.add(data_ppn)
            try:
                self._program(data_ppn, data, purpose="user-data")
                # 提交内容在 _commit_mapping 内部基于最新映射构建：
                # 提交页分配可能触发 GC 搬迁，预先快照映射会指回已擦除页
                self._commit_mapping(
                    {lpn: data_ppn}, set(), 0, budget, GC_RESERVE_PAGES, purpose="commit"
                )
            finally:
                self._pinned.discard(data_ppn)
        except (BadBlockError, BlockWornError):
            self._rebuild_view()
            raise
        self._logical_log.append(
            {"op": "write", "lpn": lpn, "size": len(data), "seq": self._seq}
        )

    def read(self, lpn):
        """读逻辑页：返回 32 字节槽（写入数据的前缀，余为 0xFF 填充）；未映射返回 None。"""
        lpn = require_int("lpn", lpn, 0, LOGICAL_PAGES - 1)
        ppn = self._mapping[lpn]
        if ppn == _UNMAPPED:
            self._logical_log.append({"op": "read", "lpn": lpn, "result": "unmapped"})
            return None
        data = self._dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
        self._logical_log.append(
            {"op": "read", "lpn": lpn, "result": "ok", "ppn": ppn}
        )
        return data

    # ------------------------------------------------------------------
    # 公开逻辑接口：原子事务（最多 TRANSACTION_MAX_PAGES 个不同逻辑页）
    # ------------------------------------------------------------------
    def begin_transaction(self):
        """开启一笔事务：之后用 txn_put 暂存（内存），commit 原子生效或 abort 丢弃。

        事务进行中禁止 write()/gc_collect()；暂存只在内存，断电随主机丢失。
        """
        if self._txn is not None:
            raise TransactionError("已有事务进行中：不支持嵌套事务")
        self._txn = {}
        self._logical_log.append({"op": "txn-begin"})

    def txn_put(self, lpn, data):
        """向当前事务暂存一页（覆盖本事务内对同一 lpn 的先前暂存）。

        暂存不产生任何物理写入；一笔事务最多包含 4 个不同 lpn。
        """
        lpn = require_int("lpn", lpn, 0, LOGICAL_PAGES - 1)
        data = require_bytes("data", data, min_len=1, max_len=PAGE_SIZE)
        if self._txn is None:
            raise TransactionError("txn_put() 前必须先 begin_transaction()")
        if lpn not in self._txn and len(self._txn) >= TRANSACTION_MAX_PAGES:
            raise InvalidInputError(
                f"单笔事务最多 {TRANSACTION_MAX_PAGES} 个不同逻辑页，"
                f"当前已有 {len(self._txn)} 个"
            )
        self._txn[lpn] = data
        self._logical_log.append(
            {"op": "txn-put", "lpn": lpn, "size": len(data), "pages": len(self._txn)}
        )

    def transaction_put(self, items, *, gc_budget=None):
        """便捷原子接口：items 为 (lpn, data) 序列，内部 begin/puts/commit。

        返回提交后的提交序列号；任何输入非法或容量不足都整体不生效。
        要求不存在进行中的事务。
        """
        if self._txn is not None:
            raise TransactionError(
                "已有事务进行中：transaction_put 不能与显式事务嵌套"
            )
        # 先完整复制并校验，任何非法项都不得留下进行中事务
        pairs = []
        seen = set()
        try:
            iterator = iter(items)
        except TypeError:
            raise InvalidInputError(
                f"items 必须是 (lpn, data) 序列，得到 {items!r}"
            ) from None
        for idx, item in enumerate(iterator):
            try:
                lpn_raw, data_raw = item
            except (TypeError, ValueError):
                raise InvalidInputError(
                    f"items[{idx}] 必须是 (lpn, data) 二元组，得到 {item!r}"
                ) from None
            lpn = require_int(f"items[{idx}] 的 lpn", lpn_raw, 0, LOGICAL_PAGES - 1)
            data = require_bytes(
                f"items[{idx}] 的 data", data_raw, min_len=1, max_len=PAGE_SIZE
            )
            pairs.append((lpn, data))
            seen.add(lpn)
        if len(seen) > TRANSACTION_MAX_PAGES:
            raise InvalidInputError(
                f"单笔事务最多 {TRANSACTION_MAX_PAGES} 个不同逻辑页，"
                f"得到 {len(seen)} 个"
            )
        self.begin_transaction()
        try:
            for lpn, data in pairs:
                self.txn_put(lpn, data)
            seq = self.commit_transaction(gc_budget=gc_budget)
        except BaseException:
            # 提交期掉电时设备冻结、内存事务清空；其它异常一律回滚暂存
            self._txn = None
            raise
        return seq

    def commit_transaction(self, *, gc_budget=None):
        """原子提交当前事务，返回提交后的序列号。

        顺序保证原子性：先做容量决策（必要时 GC，全部旧映像仍受既有提交
        记录保护），再顺序写 n 个 txn-data 页，最后用唯一一条 txn-commit
        记录一次性公开全部新映射。提交记录落盘前掉电 → 恢复得整笔旧值；
        落盘后掉电 → 恢复得整笔新值；不存在中间态。

        容量/预算不足在写任何事务数据页之前拒绝（InvalidInputError 的空
        事务同样先拒绝），不产生任何事务页；GC 动作可能已经发生，但它们
        只回收死页、不改变已提交映像。提交因任何原因失败时，内存暂存
        一律清空（不留半更新状态）；需要重试请重新 begin/put。
        """
        if self._txn is None:
            raise TransactionError("没有进行中的事务可提交")
        staging = self._txn
        if not staging:
            self._txn = None
            raise InvalidInputError("空事务不能提交：至少需要 txn_put 一页")
        budget = self._resolve_budget(gc_budget)
        n = len(staging)
        self._gc_spent = 0
        self._rebuild_view()
        updates: dict[int, int] = {}
        lpns = sorted(staging)
        try:
            # 阶段 1（决策/整理）：在写任何事务数据页之前，保证空白页足够
            # n 个数据页 + 1 个提交页。此阶段失败：提交映像不变，直接拒绝。
            self._reserve_pages(n + 1, budget)
            # 阶段 2：顺序写 n 个数据页（尚无提交记录引用它们 → 对恢复不可见）。
            # 这些页必须全程 pin 到提交记录落盘：全 0xFF 数据页与空白页不可
            # 区分，否则同一事务后续分配可能重复占用本事务早先的数据页。
            for lpn in lpns:
                ppn = self._alloc_page(set())
                # _reserve_pages 已保证有足够空白页（且未向设备写入），
                # 分配必成功；防御性检查：
                if ppn is None:
                    raise FTLError("内部错误：预留后仍无法分配事务数据页")
                self._pinned.add(ppn)
                self._program(ppn, staging[lpn], purpose="txn-data")
                updates[lpn] = ppn
            # 阶段 3：唯一一条提交记录原子公开全部新映射
            self._commit_mapping(
                updates, set(), 0, budget, GC_RESERVE_PAGES, purpose="txn-commit"
            )
        except (BadBlockError, BlockWornError):
            self._rebuild_view()
            self._txn = None
            raise
        except BaseException:
            # PowerLossError/容量拒绝等：清空内存暂存；已提交映像语义由恢复保证
            self._txn = None
            raise
        finally:
            # 事务数据页从分配到提交记录落盘全程 pin；无论成功失败都释放
            self._pinned.difference_update(updates.values())
        self._txn = None
        self._logical_log.append(
            {"op": "txn-commit", "pages": n, "seq": self._seq}
        )
        return self._seq

    def abort_transaction(self):
        """丢弃当前事务的全部暂存（纯内存操作，无物理写入）。幂等于无事务时抛状态错误。"""
        if self._txn is None:
            raise TransactionError("没有进行中的事务可搁置")
        n = len(self._txn)
        self._txn = None
        self._logical_log.append({"op": "txn-abort", "pages": n})

    def in_transaction(self):
        """是否存在进行中的事务（暂存尚未提交/搁置）。"""
        return self._txn is not None

    def transaction_staging(self):
        """当前事务暂存区只读副本：((lpn, data), ...)，按 lpn 排序；无事务返回 ()。"""
        if self._txn is None:
            return ()
        return tuple((lpn, self._txn[lpn]) for lpn in sorted(self._txn))

    def gc_collect(self, *, gc_budget=None):
        """手动垃圾整理：在预算内回收所有可回收块，返回成功擦除的块数。"""
        budget = self._resolve_budget(gc_budget)
        if self._txn is not None:
            raise TransactionError(
                "事务进行中不能调用 gc_collect()：请先 commit_transaction/abort_transaction"
            )
        self._gc_spent = 0
        self._rebuild_view()
        reclaimed = 0
        while self._pick_victim(set()) is not None:
            self._reclaim_one_block(set(), 0, budget)
            reclaimed += 1
        self._logical_log.append({"op": "gc", "reclaimed": reclaimed})
        return reclaimed

    def capacity(self):
        """剩余容量与磨损快照（全部为精确整数，无浮点）。"""
        self._rebuild_view()
        dev = self._dev
        return {
            "logical_pages": LOGICAL_PAGES,
            "mapped_pages": sum(1 for p in self._mapping if p != _UNMAPPED),
            "free_physical_pages": self._total_blanks(),
            "gc_reserved_pages": GC_RESERVE_PAGES,
            "reclaimable_dead_pages": sum(v.dead for v in self._view),
            "bad_blocks": dev.bad_blocks(),
            "worn_blocks": tuple(
                b
                for b in range(BLOCKS)
                if not dev.is_bad_block(b) and dev.erase_count(b) >= dev.max_erases
            ),
            "erase_counts": tuple(dev.erase_count(b) for b in range(BLOCKS)),
            "commit_seq": self._seq,
            "transaction_open": self._txn is not None,
            "transaction_staged_pages": 0 if self._txn is None else len(self._txn),
            "transaction_max_pages": TRANSACTION_MAX_PAGES,
        }

    def logical_log(self):
        """逻辑读写日志（只读副本）。"""
        return tuple(dict(e) for e in self._logical_log)

    def physical_log(self):
        """物理动作日志（代理设备日志）。"""
        return self._dev.action_log()

    def mapping_snapshot(self):
        """当前映射快照：24 项，元素为物理页号或 None（未映射）。"""
        return tuple(None if p == _UNMAPPED else p for p in self._mapping)

    @property
    def commit_seq(self):
        return self._seq

    @property
    def device(self):
        return self._dev

    # ------------------------------------------------------------------
    # 内部：预算 / 分配 / 提交 / 垃圾整理
    # ------------------------------------------------------------------
    def _resolve_budget(self, override):
        if override is None:
            return self._gc_budget
        return require_int("gc_budget", override, 0, INT_CAP)

    def _program(self, ppn, data, *, purpose):
        self._dev.program(
            ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK, data, purpose=purpose
        )
        self._rebuild_view()

    def _alloc_page(self, exclude):
        """选（擦除次数最少, 空白页最多, 块号最小）的块，取其首个可写页。

        可写判定不信赖物理 blank 位：映射引用页（即使内容恰为全 0xFF）、
        pinned 页（进行中的写入/搬迁/事务数据页）、当前提交记录页一律跳过。
        """
        dev = self._dev
        referenced = {p for p in self._mapping if p != _UNMAPPED}
        referenced.update(self._pinned)
        if self._commit_ppn is not None:
            referenced.add(self._commit_ppn)
        candidates = []
        for b in range(BLOCKS):
            if b in exclude or dev.is_bad_block(b):
                continue
            v = self._view[b]
            if v.blanks == 0:
                continue
            candidates.append(((dev.erase_count(b), -v.blanks, b), b))
        candidates.sort()
        for _, b in candidates:
            for p in range(PAGES_PER_BLOCK):
                ppn = b * PAGES_PER_BLOCK + p
                if ppn in referenced:
                    continue
                if dev.is_blank(b, p):
                    return ppn
        return None

    def _ensure_free_page(self, exclude, depth, budget, reserve):
        """保证拿到一个空白页；空白不足（<= reserve）时先做垃圾整理。

        reserve：为用户态分配保留的余量（GC 内部搬迁用 reserve=0）。
        """
        for _ in range(BLOCKS + 1):
            if self._total_blanks() > reserve:
                ppn = self._alloc_page(exclude)
                if ppn is not None:
                    return ppn
            if depth >= _MAX_GC_DEPTH:
                raise FTLError("垃圾整理嵌套深度异常，判定为内部状态错误")
            self._reclaim_one_block(exclude, depth, budget)
        raise OutOfSpaceError("垃圾整理后仍无空白物理页")

    def _reserve_pages(self, need, budget):
        """在不消耗空白页的前提下，保证可立即分配 need 个用户页。

        空白页（保留 GC_RESERVE_PAGES 后）不足时先做垃圾整理；整理仍不足
        抛 OutOfSpaceError，预算不允许抛 GCBudgetExhaustedError。
        供事务提交在写任何事务数据页之前完成“容量必须先拒绝”的决策。
        """
        # 每次整理擦除一个不同的块（擦后无死页，保留循环内不会再被选中），
        # 因此至多 BLOCKS 次；超过即内部状态错误。
        for _ in range(BLOCKS + 1):
            if self._total_blanks() >= GC_RESERVE_PAGES + need:
                return
            self._reclaim_one_block(set(), 0, budget)
        raise FTLError("容量预留循环异常，判定为内部状态错误")

    def _commit_mapping(self, updates, exclude, depth, budget, reserve, *, purpose):
        """写一条提交记录：先分配提交页（可能触发 GC 搬迁并改变当前映射），
        再把 updates（lpn -> ppn）应用到分配之后的最新映射上编码提交。
        提交记录整页写成功后，新映射才对后续挂载可见。"""
        if self._seq >= _SEQ_MAX:
            raise FTLError(f"提交序列号空间（{_SEQ_MAX}）已耗尽")
        ppn = self._ensure_free_page(exclude, depth + 1, budget, reserve)
        new_mapping = list(self._mapping)
        for lpn, ppn_new in updates.items():
            new_mapping[lpn] = ppn_new
        record = encode_commit_record(self._seq + 1, new_mapping)
        self._pinned.add(ppn)
        try:
            self._program(ppn, record, purpose=purpose)
        finally:
            self._pinned.discard(ppn)
        self._mapping = new_mapping
        self._seq += 1
        self._commit_ppn = ppn
        self._rebuild_view()

    def _pick_victim(self, exclude):
        """选受害块：含死页、无 pinned、未达磨损上限；有效页最少者优先。"""
        dev = self._dev
        best = None
        for b in range(BLOCKS):
            if b in exclude or dev.is_bad_block(b):
                continue
            v = self._view[b]
            if v.dead == 0 or v.pinned > 0:
                continue
            if dev.erase_count(b) >= dev.max_erases:
                continue  # 已达磨损上限，擦除无意义
            key = (v.valid, dev.erase_count(b), b)
            if best is None or key < best[0]:
                best = (key, b)
        return None if best is None else best[1]

    def _reclaim_one_block(self, exclude, depth, budget):
        if self._gc_spent >= budget:
            raise GCBudgetExhaustedError(
                f"本次操作的垃圾整理擦除预算 {budget} 已耗尽"
                "（仍有可回收块，属预算耗尽而非证明无解）"
            )
        victim = self._pick_victim(exclude)
        if victim is None:
            raise OutOfSpaceError("无空白页且无可回收块：可证明当前无解")
        victim_pages = set(
            range(victim * PAGES_PER_BLOCK, (victim + 1) * PAGES_PER_BLOCK)
        )
        moved = False
        for lpn, ppn in enumerate(self._mapping):
            if ppn not in victim_pages:
                continue
            data = self._dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
            new_ppn = self._ensure_free_page(exclude | {victim}, depth + 1, budget, 0)
            self._pinned.add(new_ppn)
            try:
                self._program(new_ppn, data, purpose="gc-copy")
            finally:
                self._pinned.discard(new_ppn)
            self._mapping[lpn] = new_ppn
            moved = True
            self._rebuild_view()
        # 擦除受害块前，必须保证最新提交记录不在受害块内：
        # 搬过数据页、或受害块恰好持有当前提交记录时，先写一条新提交记录。
        if moved or self._commit_ppn in victim_pages:
            self._commit_mapping(
                {},
                exclude | {victim},
                depth + 1,
                budget,
                0,
                purpose="gc-commit",
            )
        try:
            self._dev.erase(victim, purpose="gc-erase")
        except BlockWornError:
            # 防御路径（正常受害块筛选已排除磨损块）：退役该块，永不再用
            self._dev.retire_block(victim)
            self._rebuild_view()
            raise
        self._gc_spent += 1
        self._rebuild_view()
