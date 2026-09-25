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
- 显式磨损均衡 wear_level(max_moves)：一次性有限维护（无后台线程、不被
  write/gc 自动调用）。把低磨损块中长期持有的有效页整块迁往擦除次数严格
  更多但仍可用（未坏、未达磨损上限）的空白页，新映像提交可见后才擦除
  源块，使低磨损块以空白块身份重新进入分配。源块按（擦除次数升序,
  块号升序）选取；预算按有效页搬迁数封顶，实际物理动作按实结算。
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

    def wear_level(self, max_moves):
        """显式磨损均衡：一次性有限维护（无后台线程，write/gc 不会自动调用）。

        把低磨损块中长期持有的有效（冷）页迁往擦除次数严格更多、但仍可用
        （非坏块、未达磨损上限）的空白页，新映像提交可见后才擦除源块，
        使低磨损块以空白块身份重新进入分配。最多搬迁 max_moves 个有效页
        （预算按有效页数封顶，实际物理动作按实结算并如实报告）。

        返回报告 dict：
        - moved_pages：实际搬迁的有效页数
        - leveled_blocks：被腾空并擦除的源块（按处理顺序）
        - program_actions / erase_actions：本次维护实际发生的物理动作数
        - stop_reason：no-candidate（无候选/未达阈值，无动作）、
          budget-exhausted（max_moves 用尽或不足搬迁下一块）、
          insufficient-space（可用空白页不足以腾空下一块；空间不足与预算
          耗尽分开报告）
        逻辑页内容不被改变；中途掉电可能留下孤儿页，但恢复的逻辑数据不丢。
        """
        max_moves = require_int("max_moves", max_moves, 0, INT_CAP)
        self._rebuild_view()
        log0 = len(self._dev.action_log())
        moved = 0
        leveled = []
        stop = "no-candidate"
        while moved < max_moves:
            src = self._pick_wear_source()
            if src is None:
                break
            n = self._view[src].valid
            if moved + n > max_moves:
                stop = "budget-exhausted"
                break
            if self._wear_slots(src) < n or self._blank_pages_outside(src) < n + 1:
                stop = "insufficient-space"
                break
            self._wear_evacuate(src)
            moved += n
            leveled.append(src)
        else:
            stop = "budget-exhausted"  # 预算恰好用尽：不再探查是否还有候选
        actions = self._dev.action_log()[log0:]
        report = {
            "moved_pages": moved,
            "leveled_blocks": tuple(leveled),
            "program_actions": sum(1 for a in actions if a.kind == "program"),
            "erase_actions": sum(1 for a in actions if a.kind == "erase"),
            "stop_reason": stop,
        }
        self._logical_log.append({"op": "wear_level", **report})
        return report

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

    def _program(self, ppn, data, *, purpose):
        self._dev.program(
            ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK, data, purpose=purpose
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
        self._write_commit(ppn, updates, purpose)

    def _write_commit(self, ppn, updates, purpose):
        """在指定（已确保空白的）页上写整映像提交记录并更新映射状态。"""
        if self._seq >= _SEQ_MAX:
            raise FTLError(f"提交序列号空间（{_SEQ_MAX}）已耗尽")
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

    # ------------------------------------------------------------------
    # 内部：显式磨损均衡（一次性维护，不与 GC 预算/嵌套共用）
    # ------------------------------------------------------------------
    def _usable(self, b):
        """块仍可用：非坏块且未达磨损上限（擦除仍被允许）。"""
        dev = self._dev
        return not dev.is_bad_block(b) and dev.erase_count(b) < dev.max_erases

    def _pick_wear_source(self):
        """选磨损均衡源块：持有有效页、可擦除，且存在擦除次数严格更高的
        可用块。按（擦除次数升序, 块号升序）确定；无候选返回 None。"""
        dev = self._dev
        best = None
        for b in range(BLOCKS):
            if not self._usable(b) or self._view[b].pinned > 0:
                continue
            if self._view[b].valid == 0:
                continue
            if not any(
                self._usable(h) and dev.erase_count(h) > dev.erase_count(b)
                for h in range(BLOCKS)
            ):
                continue  # 没有擦写更多的可用块：未达阈值，不动
            key = (dev.erase_count(b), b)
            if best is None or key < best[0]:
                best = (key, b)
        return None if best is None else best[1]

    def _wear_target_block(self, src):
        """选搬迁目标块：非源块、可用、擦除次数严格高于源块且有空白页；
        按（擦除次数升序, 块号升序）确定——等磨损平局由块号打破，
        同一块填满后再换下一块（在更高磨损块中取最保守者）。"""
        dev = self._dev
        src_count = dev.erase_count(src)
        candidates = []
        for b in range(BLOCKS):
            if b == src or not self._usable(b):
                continue
            if dev.erase_count(b) <= src_count or self._view[b].blanks == 0:
                continue
            candidates.append((dev.erase_count(b), b))
        candidates.sort()
        return None if not candidates else candidates[0][1]

    def _wear_alloc(self, src):
        """在合格目标块上分配一个空白页；无合格页返回 None。"""
        b = self._wear_target_block(src)
        if b is None:
            return None
        for p in range(PAGES_PER_BLOCK):
            if self._dev.is_blank(b, p):
                return b * PAGES_PER_BLOCK + p
        return None

    def _wear_slots(self, src):
        """合格目标块上的空白页总数。"""
        dev = self._dev
        src_count = dev.erase_count(src)
        total = 0
        for b in range(BLOCKS):
            if b == src or not self._usable(b):
                continue
            if dev.erase_count(b) > src_count:
                total += self._view[b].blanks
        return total

    def _blank_pages_outside(self, src):
        """源块之外所有可用块的空白页总数（供提交记录页落脚）。"""
        return sum(
            self._view[b].blanks
            for b in range(BLOCKS)
            if b != src and self._usable(b)
        )

    def _wear_alloc_commit(self, src):
        """为提交记录分配空白页：源块之外、任何仍可用（未坏、未磨损）的
        块均可，按分配器常规次序（擦除次数升序, 空白页数降序, 块号升序）。"""
        dev = self._dev
        candidates = []
        for b in range(BLOCKS):
            if b == src or not self._usable(b) or self._view[b].blanks == 0:
                continue
            candidates.append(((dev.erase_count(b), -self._view[b].blanks, b), b))
        candidates.sort()
        for _, b in candidates:
            for p in range(PAGES_PER_BLOCK):
                if dev.is_blank(b, p):
                    return b * PAGES_PER_BLOCK + p
        return None

    def _wear_evacuate(self, src):
        """把源块全部有效页迁往更高磨损的可用空白页，提交完整映像后才擦源块。

        与 GC 同一协议：搬迁页先编程、映射在内存中更新，整映像提交记录
        成功（新映像可见）之后才擦除源块；任何中途掉电只留孤儿页，
        重挂载恢复到最后已提交映像，逻辑数据不丢。
        """
        dev = self._dev
        src_pages = set(range(src * PAGES_PER_BLOCK, (src + 1) * PAGES_PER_BLOCK))
        for lpn, ppn in enumerate(self._mapping):
            if ppn not in src_pages:
                continue
            data = dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
            new_ppn = self._wear_alloc(src)
            if new_ppn is None:
                # 防御路径：空间已在调用前预检；此处失败即内部状态错误
                raise FTLError("磨损均衡目标页在搬迁中途消失，判定为内部状态错误")
            self._pinned.add(new_ppn)
            try:
                self._program(new_ppn, data, purpose="wear-copy")
            finally:
                self._pinned.discard(new_ppn)
            self._mapping[lpn] = new_ppn
        # 提交页可落在源块之外任何仍可用（未坏、未磨损）的空白页
        commit_ppn = self._wear_alloc_commit(src)
        if commit_ppn is None:
            raise FTLError("磨损均衡提交记录无空白页可写，判定为内部状态错误")
        self._write_commit(commit_ppn, {}, "wear-commit")
        dev.erase(src, purpose="wear-erase")
        self._rebuild_view()
