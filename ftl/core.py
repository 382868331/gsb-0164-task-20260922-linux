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
- 只读快照：创建时把当前整映像写入单页快照记录（CRC 保护、原子可见），
  快照引用的数据页所在块在快照存活期间不参与 GC；删除写墓碑记录，
  墓碑与创建记录的擦除顺序保证已删除快照断电后不复活。挂载时扫描
  快照/墓碑记录重建全部有效快照，ID 永不复用。
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
_SNAP_MAGIC = 0x5A  # 快照创建记录
_TOMB_MAGIC = 0x5B  # 快照删除（墓碑）记录
_SEQ_MAX = 0xFFFFFF  # 24 位序列号空间
_SNAP_ID_MAX = 0xFFFF  # 快照 ID 为 2 字节，永不复用
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


# ----------------------------------------------------------------------
# 只读快照记录编解码（公开：格式即契约）
#
# 快照创建记录（31 字节内容，余 1 字节为空白）：
#   magic(0x5A, 1B) | snap_id(2B 小端) | mapping(24B) | crc32(4B 小端)
# 快照删除/墓碑记录：
#   magic(0x5B, 1B) | snap_id(2B 小端) | crc32(4B 小端)
#
# 快照 ID 在设备生命周期内单调递增、永不复用：因此恢复时无需比较
# 创建/删除记录的先后顺序——某 ID 存在墓碑即永久删除。
# ----------------------------------------------------------------------
def encode_snapshot_record(snap_id, mapping):
    """把 (快照 ID, 24 项映射) 编码为快照创建记录（32 字节槽）。"""
    snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
    if len(mapping) != LOGICAL_PAGES:
        raise InvalidInputError(f"mapping 必须有 {LOGICAL_PAGES} 项")
    entries = []
    for x in mapping:
        x = require_int("mapping 项", x, 0, _UNMAPPED)
        if x != _UNMAPPED and x >= TOTAL_PAGES:
            raise InvalidInputError(f"mapping 项 {x} 超出物理页范围 [0, {TOTAL_PAGES})")
        entries.append(x)
    body = bytes([_SNAP_MAGIC]) + snap_id.to_bytes(2, "little") + bytes(entries)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + crc.to_bytes(4, "little") + bytes([_UNMAPPED])


def decode_snapshot_record(page):
    """解析快照创建记录；完整合法返回 (snap_id, mapping)，否则 None。"""
    if len(page) != PAGE_SIZE or page[0] != _SNAP_MAGIC:
        return None
    body = page[:27]
    if (zlib.crc32(body) & 0xFFFFFFFF) != int.from_bytes(page[27:31], "little"):
        return None
    if page[31] != _UNMAPPED:
        return None
    snap_id = int.from_bytes(page[1:3], "little")
    mapping = list(page[3:27])
    seen = set()
    for ppn in mapping:
        if ppn == _UNMAPPED:
            continue
        if ppn >= TOTAL_PAGES or ppn in seen:
            return None
        seen.add(ppn)
    return snap_id, mapping


def encode_tombstone_record(snap_id):
    """把快照 ID 编码为删除（墓碑）记录（32 字节槽）。"""
    snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
    body = bytes([_TOMB_MAGIC]) + snap_id.to_bytes(2, "little")
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + crc.to_bytes(4, "little") + bytes([_UNMAPPED]) * 25


def decode_tombstone_record(page):
    """解析墓碑记录；完整合法返回 snap_id，否则 None。"""
    if len(page) != PAGE_SIZE or page[0] != _TOMB_MAGIC:
        return None
    body = page[:3]
    if (zlib.crc32(body) & 0xFFFFFFFF) != int.from_bytes(page[3:7], "little"):
        return None
    if any(b != _UNMAPPED for b in page[7:]):
        return None
    return int.from_bytes(page[1:3], "little")


class _BlockView:
    __slots__ = ("blanks", "dead", "valid", "pinned", "commit", "snap")

    def __init__(self):
        self.blanks = 0  # 空白页数
        self.dead = 0  # 死页数（可经擦除回收）
        self.valid = 0  # 当前映射引用的有效数据页数
        self.pinned = 0  # 进行中的操作占用的页数（不可动）
        self.commit = 0  # 当前最新提交记录页（不可回收）
        self.snap = 0  # 有效快照引用的数据页/记录（块因此不可作为 GC 受害块）


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
        self._snaps: dict[int, list[int]] = {}  # 有效快照 ID -> 创建时映射
        self._snap_ppn: dict[int, int] = {}  # 快照 ID -> 创建记录所在物理页
        self._dead_create_ppn: dict[int, int] = {}  # 已删除快照 ID -> 创建记录页（擦除前保留索引）
        self._tomb_blocks: dict[int, set[int]] = {}  # 快照 ID -> 墓碑所在块集合
        self._next_snap_id = 0
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
        snap_info = self._scan_snapshot_pages()
        if best is not None:
            self._seq = best[0]
            self._commit_ppn = best[1]
            self._mapping = list(best[2])
        self._snaps = dict(snap_info["snaps"])
        self._snap_ppn = dict(snap_info["snap_ppn"])
        self._dead_create_ppn = {
            sid: ppn
            for sid, ppn in snap_info["create_ppn"].items()
            if sid not in snap_info["snaps"]
        }
        self._tomb_blocks = {
            sid: set(blocks) for sid, blocks in snap_info["tomb_blocks"].items()
        }
        self._next_snap_id = (
            max(
                [*self._snaps, *self._snap_ppn, *self._tomb_blocks],
                default=-1,
            )
            + 1
        )
        self._rebuild_view()

    def _scan_snapshot_pages(self):
        """扫描全部物理页，解析快照创建记录与墓碑记录。

        快照 ID 永不复用：存在墓碑的 ID 一律视为已删除（恢复无需排序）。
        同一 ID 存在多份创建记录（理论上不会发生）时取物理页号最大者。
        """
        dev = self._dev
        creates: dict[int, tuple[int, list[int]]] = {}  # sid -> (ppn, mapping)
        tombs: dict[int, set[int]] = {}
        for b in range(BLOCKS):
            if dev.is_bad_block(b):
                continue
            for p in range(PAGES_PER_BLOCK):
                ppn = b * PAGES_PER_BLOCK + p
                raw = dev.read_page(b, p)
                if raw[0] == _SNAP_MAGIC:
                    rec = decode_snapshot_record(raw)
                    if rec is None:
                        continue
                    sid, mapping = rec
                    if ppn in mapping:
                        continue  # 快照记录不得引用自身所在页
                    if sid not in creates or ppn > creates[sid][0]:
                        creates[sid] = (ppn, list(mapping))
                elif raw[0] == _TOMB_MAGIC:
                    sid = decode_tombstone_record(raw)
                    if sid is None:
                        continue
                    tombs.setdefault(sid, set()).add(b)
        snaps = {}
        snap_ppn = {}
        for sid, (ppn, mapping) in creates.items():
            if sid in tombs:
                continue
            snaps[sid] = mapping
            snap_ppn[sid] = ppn
        return {
            "snaps": snaps,
            "snap_ppn": snap_ppn,
            "tomb_blocks": tombs,
            "create_ppn": {sid: ppn for sid, (ppn, _) in creates.items()},
        }

    def _rebuild_view(self):
        dev = self._dev
        self._purge_dead_snapshot_index()
        valid = {p for p in self._mapping if p != _UNMAPPED}
        snap_pages = set()
        for mapping in self._snaps.values():
            snap_pages.update(p for p in mapping if p != _UNMAPPED)
        live_create = set(self._snap_ppn.values())
        # 墓碑仅在“创建记录仍存在于其他块”时不可回收：
        # 同块的创建记录与墓碑随块擦除原子消失，不存在复活窗口；
        # 创建记录先被擦除后，墓碑才成为可回收死页。
        pinned_tomb_ppns: set[int] = set()
        for sid, blocks in self._tomb_blocks.items():
            create_ppn = self._snap_ppn.get(sid, self._dead_create_ppn.get(sid))
            if create_ppn is not None:
                cb, cp = divmod(create_ppn, PAGES_PER_BLOCK)
                create_alive = not dev.is_bad_block(cb) and not dev.is_blank(cb, cp)
            else:
                create_alive = False
            for b in blocks:
                if not create_alive or cb != b:
                    # 创建记录仍在其他块：墓碑先被擦掉会让已删除快照复活
                    pinned_tomb_ppns.update(
                        range(b * PAGES_PER_BLOCK, (b + 1) * PAGES_PER_BLOCK)
                    )
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
                elif ppn in pinned_tomb_ppns:
                    v.snap += 1
                elif ppn in live_create:
                    v.snap += 1
                elif ppn in snap_pages:
                    v.snap += 1
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

    # ------------------------------------------------------------------
    # 只读快照
    # ------------------------------------------------------------------
    def create_snapshot(self):
        """为当前已提交映像创建只读快照，返回快照 ID（非负整数）。

        快照记录为单页原子写入：整页编程成功后快照才存在；之后的覆盖写
        与垃圾整理都不会改动快照引用的数据页。快照在设备生命周期内
        ID 单调递增、不复用；耗尽（65535 个）抛 FTLError。
        """
        if self._next_snap_id > _SNAP_ID_MAX:
            raise FTLError(f"快照 ID 空间（{_SNAP_ID_MAX + 1} 个）已耗尽")
        budget = self._gc_budget
        snap_id = self._next_snap_id
        self._gc_spent = 0
        self._rebuild_view()
        ppn = self._ensure_free_page(set(), 0, budget, 0)
        record = encode_snapshot_record(snap_id, self._mapping)
        self._pinned.add(ppn)
        try:
            self._program(ppn, record, purpose="snapshot")
        finally:
            self._pinned.discard(ppn)
        self._snaps[snap_id] = list(self._mapping)
        self._snap_ppn[snap_id] = ppn
        self._next_snap_id += 1
        self._logical_log.append(
            {"op": "create_snapshot", "snapshot_id": snap_id, "seq": self._seq}
        )
        return snap_id

    def read_snapshot(self, snap_id, lpn):
        """读快照中的逻辑页：返回 32 字节槽；快照中未映射返回 None。

        未知或已删除的快照 ID 抛 InvalidInputError。
        """
        snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
        lpn = require_int("lpn", lpn, 0, LOGICAL_PAGES - 1)
        mapping = self._snaps.get(snap_id)
        if mapping is None:
            raise InvalidInputError(f"快照 {snap_id} 不存在或已删除")
        ppn = mapping[lpn]
        if ppn == _UNMAPPED:
            self._logical_log.append(
                {
                    "op": "read_snapshot",
                    "snapshot_id": snap_id,
                    "lpn": lpn,
                    "result": "unmapped",
                }
            )
            return None
        data = self._dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
        self._logical_log.append(
            {
                "op": "read_snapshot",
                "snapshot_id": snap_id,
                "lpn": lpn,
                "result": "ok",
                "ppn": ppn,
            }
        )
        return data

    def delete_snapshot(self, snap_id):
        """删除只读快照：写墓碑记录（单页原子）后释放快照占用。

        墓碑写入成功前快照仍有效；删除未知/已删除 ID 抛 InvalidInputError。
        被快照引用的数据页与创建记录随后可被垃圾整理回收。
        """
        snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
        if snap_id not in self._snaps:
            raise InvalidInputError(f"快照 {snap_id} 不存在或已删除")
        budget = self._gc_budget
        self._gc_spent = 0
        self._rebuild_view()
        ppn = self._ensure_free_page(set(), 0, budget, 0)
        record = encode_tombstone_record(snap_id)
        self._pinned.add(ppn)
        try:
            self._program(ppn, record, purpose="snapshot-delete")
        finally:
            self._pinned.discard(ppn)
        create_ppn = self._snap_ppn.pop(snap_id)
        del self._snaps[snap_id]
        self._dead_create_ppn[snap_id] = create_ppn
        self._tomb_blocks.setdefault(snap_id, set()).add(
            ppn // PAGES_PER_BLOCK
        )
        self._rebuild_view()
        self._logical_log.append(
            {"op": "delete_snapshot", "snapshot_id": snap_id}
        )

    def snapshots(self):
        """当前有效只读快照 ID 元组（按创建顺序）。"""
        return tuple(sorted(self._snaps))

    def snapshot_mapping(self, snap_id):
        """快照映射副本：24 项，元素为物理页号或 None（未映射）。"""
        snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
        mapping = self._snaps.get(snap_id)
        if mapping is None:
            raise InvalidInputError(f"快照 {snap_id} 不存在或已删除")
        return tuple(None if p == _UNMAPPED else p for p in mapping)

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
            "live_snapshots": len(self._snaps),
            "snapshot_pinned_pages": sum(v.snap for v in self._view),
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

    def _purge_dead_snapshot_index(self):
        """已删除快照的创建记录已被擦除时，丢弃其墓碑钉住索引。"""
        dev = self._dev
        stale = []
        for sid, create_ppn in self._dead_create_ppn.items():
            b, p = divmod(create_ppn, PAGES_PER_BLOCK)
            if dev.is_bad_block(b) or dev.is_blank(b, p):
                stale.append(sid)
        for sid in stale:
            self._dead_create_ppn.pop(sid, None)
            # 墓碑可能仍在设备上，但已没有任何创建记录可复活，成为普通死页
            self._tomb_blocks.pop(sid, None)

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
            if v.dead == 0 or v.pinned > 0 or v.snap > 0:
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
            pinned_blocks = sum(1 for v in self._view if v.snap > 0)
            hint = (
                f"（其中 {pinned_blocks} 个块被有效快照引用，删除快照后可回收）"
                if pinned_blocks
                else ""
            )
            raise OutOfSpaceError(
                f"无空白页且无可回收块：可证明当前无解{hint}"
            )
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
