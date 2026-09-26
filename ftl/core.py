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
    EraseFailureError,
    FTLError,
    GCBudgetExhaustedError,
    InvalidInputError,
    LogicalDataLossError,
    OutOfSpaceError,
    PowerLossError,
    UnreadablePageError,
)
from .validation import INT_CAP, require_bytes, require_int

LOGICAL_PAGES = 24
GC_RESERVE_PAGES = PAGES_PER_BLOCK  # 为用户分配保留 1 块，保障 GC 搬迁有处可去
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
        self._reclaim_excluding: set[int] = set()
        self._view = [_BlockView() for _ in range(BLOCKS)]
        self._gc_spent = 0
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
                if self._dev.is_page_unreadable(b, p):
                    continue
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
        self._resume_pending_retirements(best)
        self._rebuild_view()

    def _resume_pending_retirements(self, best):
        """续做“提交已落稳、块尚未正式退役”的断电点：把已无有效数据的
        擦写故障块永久标记为坏块。旧提交记录在块被标记后不再参与仲裁，
        由于 best 已是更新（seq 更大）的记录，映像不受影响。"""
        dev = self._dev
        best_ppn = None if best is None else best[1]
        valid = {p for p in self._mapping if p != _UNMAPPED}
        for b in range(BLOCKS):
            if not dev.is_no_erase_block(b) or dev.is_bad_block(b):
                continue
            pages = range(b * PAGES_PER_BLOCK, (b + 1) * PAGES_PER_BLOCK)
            holds_valid = any(p in valid for p in pages)
            holds_winner = best_ppn is not None and best_ppn // PAGES_PER_BLOCK == b
            if not holds_valid and not holds_winner:
                dev.retire_block(b)

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
                elif dev.is_page_unreadable(b, p):
                    if ppn in valid:
                        v.valid += 1  # 映射仍指向它，但读取必报损失
                elif dev.is_blank(b, p):
                    if dev.is_no_erase_block(b):
                        v.dead += 1  # 擦写故障块的空白页永不可再分配
                    else:
                        v.blanks += 1
                elif ppn == self._commit_ppn:
                    v.commit += 1
                elif ppn in valid:
                    v.valid += 1
                else:
                    if dev.is_no_erase_block(b):
                        pass  # 故障块永不可擦除，其死页不构成可回收容量
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
        """
        lpn = require_int("lpn", lpn, 0, LOGICAL_PAGES - 1)
        data = require_bytes("data", data, min_len=1, max_len=PAGE_SIZE)
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
        except (BadBlockError, BlockWornError, EraseFailureError):
            self._rebuild_view()
            raise
        self._dev.clear_logical_lost(lpn)  # 新有效数据覆盖此前的损失登记
        self._logical_log.append(
            {"op": "write", "lpn": lpn, "size": len(data), "seq": self._seq}
        )

    def read(self, lpn):
        """读逻辑页：返回 32 字节槽（写入数据的前缀，余为 0xFF 填充）；未映射返回 None。"""
        lpn = require_int("lpn", lpn, 0, LOGICAL_PAGES - 1)
        if lpn in self._dev.lost_logical_pages():
            self._logical_log.append(
                {"op": "read", "lpn": lpn, "result": "lost"}
            )
            raise LogicalDataLossError(
                f"逻辑页 {lpn} 的最新物理页已不可读，数据确认损失：拒绝返回伪造内容"
            )
        ppn = self._mapping[lpn]
        if ppn == _UNMAPPED:
            self._logical_log.append({"op": "read", "lpn": lpn, "result": "unmapped"})
            return None
        data = self._dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
        self._logical_log.append(
            {"op": "read", "lpn": lpn, "result": "ok", "ppn": ppn}
        )
        return data

    def retire_block(self, block, *, gc_budget=None):
        """永久停用物理块（模拟擦写失败后退役）。

        块内仍能读出的有效页先搬到预留好的空白块，并写入新的整映像
        提交记录；在提交记录落稳之前，旧映射继续有效（任何中途断电后
        重启都读旧地址）。指定页已不可读时不凭空恢复：对应逻辑页登记
        为已损失、新映射置为未映射并明确上报；此后读取这些逻辑页抛
        LogicalDataLossError。空间/预算不足时原样抛出，映射不变、块
        不被标记，调用方可在腾出空间后重试。坏块/故障块今后不再进入
        分配与 GC 候选。

        返回报告 dict：retired_block、migrated={(lpn, old_ppn, new_ppn)}
        的元组、damaged_lpns、commit_seq（幂等重调坏块直接返回现状）。
        """
        block = require_int("block", block, 0, BLOCKS - 1)
        budget = self._resolve_budget(gc_budget)
        self._gc_spent = 0
        self._rebuild_view()
        dev = self._dev
        if dev.is_bad_block(block):
            return {
                "retired_block": block,
                "migrated": (),
                "damaged_lpns": tuple(
                    l
                    for l in range(LOGICAL_PAGES)
                    if l in dev.lost_logical_pages()
                ),
                "commit_seq": self._seq,
            }
        victim_pages = set(
            range(block * PAGES_PER_BLOCK, (block + 1) * PAGES_PER_BLOCK)
        )
        updates = {}       # lpn -> new_ppn（可读页的搬迁结果，提交后才生效）
        moved_entries = []  # (lpn, old_ppn, new_ppn)
        damaged = []       # 不可读页对应的逻辑页，无法恢复
        readable_targets = []
        for lpn, ppn in enumerate(self._mapping):
            if ppn not in victim_pages:
                continue
            try:
                dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
            except UnreadablePageError:
                damaged.append(lpn)
                continue
            readable_targets.append((lpn, ppn))
        # 一次性预留全部搬迁目标页，避免多块级联时误占尚未擦除的块
        reserved = []
        try:
            for _ in readable_targets:
                new_ppn = self._ensure_free_page(
                    {block}, 0, budget, 0, deny_ppns=reserved
                )
                self._pinned.add(new_ppn)
                reserved.append(new_ppn)
        except BaseException:
            for new_ppn in reserved:
                self._pinned.discard(new_ppn)
            raise
        try:
            for (lpn, ppn), new_ppn in zip(
                readable_targets, reserved, strict=True
            ):
                data = dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
                self._program(new_ppn, data, purpose="retire-copy")
                updates[lpn] = new_ppn
                moved_entries.append((lpn, ppn, new_ppn))
        finally:
            for new_ppn in reserved:
                self._pinned.discard(new_ppn)
        # 不可读页先登记损失（登记持久化，但映射提交落稳前旧映像仍可读
        # 旧地址——若旧地址恰不可读，损失本就已经发生）
        for lpn in damaged:
            dev.mark_logical_lost(lpn)
        for lpn in damaged:
            updates[lpn] = _UNMAPPED
        needs_commit = bool(updates) or self._commit_ppn in victim_pages
        if needs_commit:
            self._commit_mapping(
                updates, {block}, 0, budget, 0, purpose="retire-commit"
            )
        # 提交落稳（或本来就无需提交）后才永久停用物理块
        dev.retire_block(block)
        self._rebuild_view()
        self._logical_log.append(
            {
                "op": "retire",
                "block": block,
                "migrated": tuple(moved_entries),
                "damaged_lpns": tuple(damaged),
                "seq": self._seq,
            }
        )
        return {
            "retired_block": block,
            "migrated": tuple(moved_entries),
            "damaged_lpns": tuple(damaged),
            "commit_seq": self._seq,
        }

    def gc_collect(self, *, gc_budget=None):
        """手动垃圾整理：在预算内回收所有可回收块，返回成功擦除的块数。"""
        budget = self._resolve_budget(gc_budget)
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
            "no_erase_blocks": tuple(
                b
                for b in range(BLOCKS)
                if not dev.is_bad_block(b) and dev.is_no_erase_block(b)
            ),
            "lost_logical_pages": dev.lost_logical_pages(),
            "worn_blocks": tuple(
                b
                for b in range(BLOCKS)
                if not dev.is_bad_block(b) and dev.erase_count(b) >= dev.max_erases
            ),
            "erase_counts": tuple(dev.erase_count(b) for b in range(BLOCKS)),
            "commit_seq": self._seq,
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

    def _alloc_page(self, exclude, deny_ppns=()):
        """选（擦除次数最少, 空白页最多, 块号最小）的块，取其首个空白页。"""
        dev = self._dev
        candidates = []
        for b in range(BLOCKS):
            if b in exclude or dev.is_bad_block(b) or dev.is_no_erase_block(b):
                continue
            v = self._view[b]
            if v.blanks == 0:
                continue
            candidates.append(((dev.erase_count(b), -v.blanks, b), b))
        candidates.sort()
        deny = set(deny_ppns)
        for _, b in candidates:
            for p in range(PAGES_PER_BLOCK):
                ppn = b * PAGES_PER_BLOCK + p
                if ppn not in deny and dev.is_blank(b, p):
                    return ppn
        return None

    def _ensure_free_page(self, exclude, depth, budget, reserve, deny_ppns=()):
        """保证拿到一个空白页；空白不足（<= reserve）时先做垃圾整理。

        reserve：为用户态分配保留的余量（GC 内部搬迁用 reserve=0）。
        """
        exclude = set(exclude) | self._reclaim_excluding
        deny = set(deny_ppns)
        for _ in range(BLOCKS + 1):
            if self._total_blanks() > reserve:
                ppn = self._alloc_page(exclude, deny)
                if ppn is not None:
                    return ppn
            if depth >= _MAX_GC_DEPTH:
                raise FTLError("垃圾整理嵌套深度异常，判定为内部状态错误")
            self._reclaim_one_block(exclude, depth, budget, deny_ppns=deny)
        raise OutOfSpaceError("垃圾整理后仍无空白物理页")

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
            if b in exclude or dev.is_bad_block(b) or dev.is_no_erase_block(b):
                continue
            v = self._view[b]
            if v.dead == 0:
                continue
            block_pages = {
                b * PAGES_PER_BLOCK + p for p in range(PAGES_PER_BLOCK)
            }
            if block_pages & self._pinned:
                continue  # 块内有刚编程、尚未提交的搬迁页，禁止擦除
            if dev.erase_count(b) >= dev.max_erases:
                continue  # 已达磨损上限，擦除无意义
            key = (v.valid, dev.erase_count(b), b)
            if best is None or key < best[0]:
                best = (key, b)
        return None if best is None else best[1]

    def _reclaim_one_block(self, exclude, depth, budget, *, deny_ppns=()):
        if self._gc_spent >= budget:
            raise GCBudgetExhaustedError(
                f"本次操作的垃圾整理擦除预算 {budget} 已耗尽"
                "（仍有可回收块，属预算耗尽而非证明无解）"
            )
        victim = self._pick_victim(exclude | self._reclaim_excluding)
        if victim is None:
            raise OutOfSpaceError("无空白页且无可回收块：可证明当前无解")
        self._reclaim_excluding.add(victim)
        try:
            self._do_reclaim(victim, exclude, depth, budget, deny_ppns)
        finally:
            self._reclaim_excluding.discard(victim)

    def _do_reclaim(self, victim, exclude, depth, budget, deny_ppns=()):
        victim_pages = set(
            range(victim * PAGES_PER_BLOCK, (victim + 1) * PAGES_PER_BLOCK)
        )
        # 先数出需要搬迁的可读有效页（不可读页计入损失、不占空间），
        # 并一次性预留全部目标页：逐页分配会在多块级联搬迁时把
        # “尚未擦除的受害块”当成可用空间，造成无解死锁。
        readable_targets = []
        damaged = []
        for lpn, ppn in enumerate(self._mapping):
            if ppn not in victim_pages:
                continue
            try:
                self._dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
            except UnreadablePageError:
                damaged.append(lpn)
                continue
            readable_targets.append((lpn, ppn))
        reserved_pages = []
        try:
            for _ in readable_targets:
                new_ppn = self._ensure_free_page(
                    exclude | {victim},
                    depth + 1,
                    budget,
                    0,
                    deny_ppns=set(deny_ppns) | set(reserved_pages),
                )
                self._pinned.add(new_ppn)
                reserved_pages.append(new_ppn)
        except BaseException:
            for new_ppn in reserved_pages:
                self._pinned.discard(new_ppn)
            raise
        moved = False
        try:
            for (lpn, ppn), new_ppn in zip(
                readable_targets, reserved_pages, strict=True
            ):
                data = self._dev.read_page(
                    ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK
                )
                self._program(new_ppn, data, purpose="gc-copy")
                self._mapping[lpn] = new_ppn
                moved = True
                self._rebuild_view()
        finally:
            for new_ppn in reserved_pages:
                self._pinned.discard(new_ppn)
        for lpn in damaged:
            self._dev.mark_logical_lost(lpn)
            self._mapping[lpn] = _UNMAPPED
            moved = True
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
        except EraseFailureError:
            # 擦写突然失败：块仍可读，走安全退役（有效页已全部处理完）
            self._dev.retire_block(victim)
            self._rebuild_view()
            raise
        self._gc_spent += 1
        self._rebuild_view()
