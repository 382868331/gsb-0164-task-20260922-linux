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
from .ecc import CLEAN, CORRECTED, UNCORRECTABLE, decode_page, encode_page
from .errors import (
    BadBlockError,
    BlockWornError,
    FTLError,
    GCBudgetExhaustedError,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
    UncorrectableError,
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
        self._scrub_queue: list[int] = []  # 待清洗逻辑页（纠正成功后排队）
        self._scrub_queued: set[int] = set()  # 去重：同页不重复排队
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
                elif dev.is_blank(b, p):
                    v.blanks += 1
                elif ppn == self._commit_ppn:
                    v.commit += 1
                elif ppn in valid:
                    v.valid += 1
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
                slot = data.ljust(PAGE_SIZE, b"\xff")
                self._program(
                    data_ppn, slot, spare=encode_page(slot), purpose="user-data"
                )
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
        """读逻辑页：返回 32 字节槽（写入数据的前缀，余为 0xFF 填充）；未映射返回 None。

        SECDED：单比特错（含校验位自身翻转）自动纠正后返回，并把该页
        加入待清洗队列；双比特错抛 UncorrectableError，绝不返回伪造数据。
        """
        lpn = require_int("lpn", lpn, 0, LOGICAL_PAGES - 1)
        status, slot = self._read_decoded(lpn)
        if status == "unmapped":
            return None
        if status == UNCORRECTABLE:
            raise UncorrectableError(
                f"逻辑页 {lpn} 存在双比特错，SECDED 不可纠正，数据已损坏"
            )
        return slot

    def read_status(self, lpn):
        """读逻辑页的健康状态，不抛损坏错误：
        返回 "unmapped" / "ok" / "corrected" / "corrupted"。
        副作用与 read 一致（纠正成功的页进入待清洗队列）。"""
        lpn = require_int("lpn", lpn, 0, LOGICAL_PAGES - 1)
        status, _slot = self._read_decoded(lpn)
        return {"clean": "ok", UNCORRECTABLE: "corrupted"}.get(status, status)

    def _read_decoded(self, lpn):
        """读并译码当前映射页，返回 (状态, 槽)。状态为
        "unmapped" / "clean" / "corrected" / "uncorrectable"。"""
        ppn = self._mapping[lpn]
        if ppn == _UNMAPPED:
            self._logical_log.append({"op": "read", "lpn": lpn, "result": "unmapped"})
            return "unmapped", None
        block, page = ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK
        raw = self._dev.read_page(block, page)
        spare = self._dev.read_spare(block, page)
        status, slot = decode_page(raw, spare)
        if status == UNCORRECTABLE:
            self._logical_log.append(
                {"op": "read", "lpn": lpn, "result": "corrupted", "ppn": ppn}
            )
            return UNCORRECTABLE, None
        if status == CORRECTED:
            self._enqueue_scrub(lpn)
        self._logical_log.append(
            {"op": "read", "lpn": lpn, "result": status if status != CLEAN else "ok", "ppn": ppn}
        )
        return status, slot

    # ------------------------------------------------------------------
    # 待清洗队列：纠正成功的页稍后通过安全搬迁写入新页
    # ------------------------------------------------------------------
    def _enqueue_scrub(self, lpn):
        if lpn in self._scrub_queued:
            return  # 重复读同一待清洗页不重复排队
        self._scrub_queued.add(lpn)
        self._scrub_queue.append(lpn)

    def scrub_pending(self):
        """待清洗的逻辑页号（按入队顺序，只读副本）。"""
        return tuple(self._scrub_queue)

    def scrub(self, *, gc_budget=None):
        """清洗：把待清洗页通过原安全搬迁过程（新数据页 + 提交记录）
        重写到新物理页，返回成功清洗的页数。

        - 清洗前重新译码：已变干净的页（如被覆盖写）直接出队；
        - 已不可纠正的页无法清洗（数据已损坏），保留在队列中；
        - 清洗中途掉电：提交记录协议保证映射可恢复，重启重新挂载后
          再次读到该页会重新入队。
        """
        scrubbed = 0
        for lpn in list(self._scrub_queue):
            if self._mapping[lpn] == _UNMAPPED:
                self._dequeue_scrub(lpn)
                continue
            status, slot = self._read_decoded(lpn)
            if status == CLEAN:
                self._dequeue_scrub(lpn)
                continue
            if status == UNCORRECTABLE:
                continue  # 数据已损坏，不能改写为看似合法的数据
            self.write(lpn, slot, gc_budget=gc_budget)
            self._dequeue_scrub(lpn)
            scrubbed += 1
        if scrubbed:
            self._logical_log.append({"op": "scrub", "scrubbed": scrubbed})
        return scrubbed

    def _dequeue_scrub(self, lpn):
        self._scrub_queued.discard(lpn)
        self._scrub_queue.remove(lpn)

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

    def _program(self, ppn, data, *, spare=None, purpose):
        self._dev.program(
            ppn // PAGES_PER_BLOCK,
            ppn % PAGES_PER_BLOCK,
            data,
            spare=spare,
            purpose=purpose,
        )
        self._rebuild_view()

    def _alloc_page(self, exclude):
        """选（擦除次数最少, 空白页最多, 块号最小）的块，取其首个空白页。"""
        dev = self._dev
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
                if dev.is_blank(b, p):
                    return b * PAGES_PER_BLOCK + p
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
            block, page = ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK
            raw = self._dev.read_page(block, page)
            spare = self._dev.read_spare(block, page)
            status, slot = decode_page(raw, spare)
            if status == UNCORRECTABLE:
                # 数据已损坏：原样搬迁原始比特（含原校验码），不伪造
                # 合法数据；读出时仍会明确报告损坏。
                slot, out_spare = raw, spare
            else:
                # 无错或已纠正：写入纠正后的数据并重新生成校验码
                out_spare = encode_page(slot)
            new_ppn = self._ensure_free_page(exclude | {victim}, depth + 1, budget, 0)
            self._pinned.add(new_ppn)
            try:
                self._program(new_ppn, slot, spare=out_spare, purpose="gc-copy")
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
