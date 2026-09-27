"""合成 FTL 核心：页映射、只读快照、断电安全提交、垃圾整理与磨损管理。

设计要点（与 README 一致）：
- 逻辑页覆盖写 = 写新数据页 + 写一条带 CRC32 的映射提交记录；
  提交记录整页写成功后才可见（设备保证页写原子，掉电只发生在动作边界）。
- 提交记录为整映像快照，单页 32 字节：magic(1) + seq(3, 小端)
  + mapping(24) + crc32(4)。因此逻辑页数固定为 24。
- 恢复时扫描全部物理页，取序列号最大（同号取物理页号大者）的
  完整提交记录重建映射；未提交的数据页/残缺记录一律视为死页。
- 只读快照：create_snapshot() 固化当前已提交映像，另写一页“快照描述符”
  （magic 0x5A + 全局序号 + 快照 ID + 被引用提交记录的序号/物理页 + CRC32）。
  删除快照同样写一页删除描述符（墓碑），恢复时按全局序号仲裁每个快照 ID
  的最后一条描述符。任何被存活快照引用的提交记录页与数据页在 GC 视图中
  计为 snap_meta/snap_data，受害块选择排除这些块，因此旧版本数据绝不被
  提前擦掉；删除后这些页才降为死页并可回收。
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
)
from .validation import INT_CAP, require_bytes, require_int

LOGICAL_PAGES = 24
GC_RESERVE_PAGES = PAGES_PER_BLOCK  # 为用户分配保留 1 块，保障 GC 搬迁有处可去
_UNMAPPED = 0xFF
_COMMIT_MAGIC = 0xA5
_SNAP_MAGIC = 0x5A
_SNAP_OP_CREATE = 1
_SNAP_OP_DELETE = 0
_SEQ_MAX = 0xFFFFFF  # 24 位序列号空间
_SNAP_ID_MAX = 0xFFFF  # 快照 ID：16 位
_SNAP_COMMIT_NONE = 0xFF  # 空映像快照：无提交记录可引用
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
# 快照描述符编解码（公开：格式即契约）
# ----------------------------------------------------------------------
# 32 字节布局：magic(0x5A,1B) | op_seq(3B 小端) | op(1B) | snap_id(2B 小端)
# | commit_seq(3B 小端) | commit_ppn(1B) | reserved(17B, 全 0xFF)
# | crc32(4B 小端)
#
# op=1 表示创建：commit_seq 给出被固化的提交记录序号，commit_ppn 给出其
# 物理页号；空映像快照为 commit_seq=0、commit_ppn=0xFF。
# op=0 表示删除（墓碑）：commit_seq/commit_ppn 必须为 0/0xFF。
# op_seq 为全局单调序号（与提交记录共用同一 24 位序号空间），恢复时
# 同一 snap_id 取 op_seq 最大（同号取物理页号大）的描述符仲裁。
def encode_snapshot_record(op_seq, op, snap_id, commit_seq, commit_ppn):
    """把快照描述符编码为 32 字节页。op=1 创建，op=0 删除。"""
    op_seq = require_int("op_seq", op_seq, 1, _SEQ_MAX)
    op = require_int("op", op, _SNAP_OP_DELETE, _SNAP_OP_CREATE)
    snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
    commit_seq = require_int("commit_seq", commit_seq, 0, _SEQ_MAX)
    commit_ppn = require_int("commit_ppn", commit_ppn, 0, _SNAP_COMMIT_NONE)
    if op == _SNAP_OP_CREATE:
        if commit_ppn != _SNAP_COMMIT_NONE and (
            commit_seq == 0 or commit_ppn >= TOTAL_PAGES
        ):
            raise InvalidInputError(
                "创建描述符必须引用一条真实提交记录（或空映像 0/0xFF）"
            )
    elif (commit_seq, commit_ppn) != (0, _SNAP_COMMIT_NONE):
        raise InvalidInputError("删除描述符不得携带提交引用")
    body = (
        bytes([_SNAP_MAGIC])
        + op_seq.to_bytes(3, "little")
        + bytes([op])
        + snap_id.to_bytes(2, "little")
        + commit_seq.to_bytes(3, "little")
        + bytes([commit_ppn])
        + bytes([_UNMAPPED]) * 17
    )
    crc = zlib.crc32(body) & 0xFFFFFFFF
    return body + crc.to_bytes(4, "little")


def decode_snapshot_record(page):
    """解析快照描述符页；合法则返回 (op_seq, op, snap_id, commit_seq,
    commit_ppn)，否则 None。结构校验：magic、CRC32、保留字节与字段域。"""
    if len(page) != PAGE_SIZE or page[0] != _SNAP_MAGIC:
        return None
    if (zlib.crc32(page[:28]) & 0xFFFFFFFF) != int.from_bytes(page[28:32], "little"):
        return None
    if any(b != _UNMAPPED for b in page[11:28]):
        return None
    op_seq = int.from_bytes(page[1:4], "little")
    op = page[4]
    snap_id = int.from_bytes(page[5:7], "little")
    commit_seq = int.from_bytes(page[7:10], "little")
    commit_ppn = page[10]
    if op_seq == 0 or op not in (_SNAP_OP_DELETE, _SNAP_OP_CREATE):
        return None
    if op == _SNAP_OP_CREATE:
        if commit_ppn != _SNAP_COMMIT_NONE and (
            commit_seq == 0 or commit_ppn >= TOTAL_PAGES
        ):
            return None
    elif (commit_seq, commit_ppn) != (0, _SNAP_COMMIT_NONE):
        return None
    return op_seq, op, snap_id, commit_seq, commit_ppn


class _BlockView:
    __slots__ = ("blanks", "dead", "valid", "pinned", "commit", "snap_data", "snap_meta")

    def __init__(self):
        self.blanks = 0  # 空白页数
        self.dead = 0  # 死页数（可经擦除回收）
        self.valid = 0  # 当前映射引用的有效数据页数
        self.pinned = 0  # 进行中的操作占用的页数（不可动）
        self.commit = 0  # 当前最新提交记录页（不可回收）
        self.snap_data = 0  # 存活快照引用的数据页（删除快照前不可回收）
        self.snap_meta = 0  # 快照引用的提交记录页或存活描述符页（不可回收）


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
        # 存活快照：snap_id -> {"commit_seq", "commit_ppn" 或 None（空映像）,
        #                         "mapping", "desc_ppn", "op_seq"}
        self._snapshots: dict[int, dict] = {}
        # 全部经仲裁认可的描述符：ppn -> (op_seq, op, snap_id, commit_seq, commit_ppn)
        self._snap_records: dict[int, tuple] = {}
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
        snap_descs = []  # (op_seq, ppn, op, snap_id, commit_seq, commit_ppn)
        for b in range(BLOCKS):
            if self._dev.is_bad_block(b):
                continue
            for p in range(PAGES_PER_BLOCK):
                ppn = b * PAGES_PER_BLOCK + p
                raw = self._dev.read_page(b, p)
                if raw[0] == _COMMIT_MAGIC:
                    rec = decode_commit_record(raw)
                    if rec is None:
                        continue
                    seq, mapping = rec
                    if ppn in mapping:
                        continue  # 提交记录不得引用自身所在页
                    if best is None or (seq, ppn) > (best[0], best[1]):
                        best = (seq, ppn, mapping)
                elif raw[0] == _SNAP_MAGIC:
                    srec = decode_snapshot_record(raw)
                    if srec is None:
                        continue
                    op_seq, op, snap_id, cseq, cppn = srec
                    if ppn == cppn:
                        continue  # 描述符不得引用自身所在页
                    snap_descs.append((op_seq, ppn, op, snap_id, cseq, cppn))
        commits: dict[int, tuple] = {}  # ppn -> (seq, mapping)
        if best is not None:
            # 恢复时需要按物理页回查任意被引用提交记录，故再扫一次建立索引
            for b in range(BLOCKS):
                if self._dev.is_bad_block(b):
                    continue
                for p in range(PAGES_PER_BLOCK):
                    ppn = b * PAGES_PER_BLOCK + p
                    if ppn == best[1]:
                        commits[ppn] = (best[0], best[2])
                        continue
                    raw = self._dev.read_page(b, p)
                    if raw[0] != _COMMIT_MAGIC:
                        continue
                    rec = decode_commit_record(raw)
                    if rec is None or ppn in rec[1]:
                        continue
                    commits[ppn] = rec
            self._seq = best[0]
            self._commit_ppn = best[1]
            self._mapping = list(best[2])
        # 按 snap_id 建立描述符候选栈（op_seq 大、同号取物理页号大者优先）
        stacks: dict[int, list[tuple]] = {}
        for desc in snap_descs:
            op_seq, ppn, op, snap_id, cseq, cppn = desc
            self._seq = max(self._seq, op_seq)
            self._snap_records[ppn] = (op_seq, op, snap_id, cseq, cppn)
            stacks.setdefault(snap_id, []).append(desc)
        for snap_id, descs in stacks.items():
            descs.sort(key=lambda d: (d[0], d[1]), reverse=True)
            for desc in descs:
                op_seq, ppn, op, snap_id2, cseq, cppn = desc
                if op == _SNAP_OP_DELETE:
                    break  # 墓碑生效：该快照当前不存在
                if cppn == _SNAP_COMMIT_NONE:
                    mapping = [_UNMAPPED] * LOGICAL_PAGES  # 空映像快照
                else:
                    hit = commits.get(cppn)
                    # 被引用提交记录必须仍完整存在且序号一致，此创建描述符
                    # 才有效；无效则继续尝试同 ID 的次新描述符
                    if hit is None or hit[0] != cseq or cppn in hit[1]:
                        continue
                    mapping = list(hit[1])
                self._snapshots[snap_id] = {
                    "commit_seq": cseq,
                    "commit_ppn": None if cppn == _SNAP_COMMIT_NONE else cppn,
                    "mapping": mapping,
                    "desc_ppn": ppn,
                    "op_seq": op_seq,
                }
                break
        self._rebuild_view()

    def _rebuild_view(self):
        dev = self._dev
        valid = {p for p in self._mapping if p != _UNMAPPED}
        # 存活快照引用的数据页 / 提交记录页
        snap_data: set[int] = set()
        snap_commits: set[int] = set()
        for snap in self._snapshots.values():
            snap_data.update(
                ppn for ppn in snap["mapping"] if ppn != _UNMAPPED
            )
            if snap["commit_ppn"] is not None:
                snap_commits.add(snap["commit_ppn"])
        # 仲裁当前仍应存在的描述符（按 snap_id 取最后一条）
        winners: dict[int, tuple] = {}
        for ppn, rec in list(self._snap_records.items()):
            if not self._record_present(ppn):
                del self._snap_records[ppn]  # 所在块已被擦除
                continue
            key = (rec[0], ppn)
            cur = winners.get(rec[2])
            if cur is None or key > cur[0]:
                winners[rec[2]] = (key, ppn, rec)
        # 仲裁生效的“创建”描述符始终存活；墓碑只在同 ID 仍存在更旧描述符
        # 时需要保留（防止旧创建页在后续重挂载时复活）。一旦更旧描述符随
        # 所在块擦除而消失，墓碑本身即降为死页可被回收。
        id_counts: dict[int, int] = {}
        for rec in self._snap_records.values():
            id_counts[rec[2]] = id_counts.get(rec[2], 0) + 1
        live_descs = set()
        for snap_id, item in winners.items():
            ppn, rec = item[1], item[2]
            if rec[1] == _SNAP_OP_CREATE or id_counts[snap_id] > 1:
                live_descs.add(ppn)
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
                elif ppn in live_descs:
                    v.snap_meta += 1
                elif ppn in self._snap_records:
                    v.dead += 1  # 已被同 ID 更新描述符取代的旧描述符
                elif ppn in snap_commits:
                    # 快照引用的提交记录页：即使它同时是当前提交页也不可擦
                    v.snap_meta += 1
                elif ppn == self._commit_ppn:
                    v.commit += 1
                elif ppn in snap_data:
                    # 快照引用的数据页：GC 不得搬迁（搬迁会改变物理位置）
                    v.snap_data += 1
                else:
                    if ppn in valid:
                        v.valid += 1
                    else:
                        v.dead += 1
        self._view = views

    def _record_present(self, ppn):
        """描述符页是否仍存在（所在块未被擦除）。"""
        b, p = divmod(ppn, PAGES_PER_BLOCK)
        return not self._dev.is_bad_block(b) and not self._dev.is_blank(b, p)

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

    # ------------------------------------------------------------------
    # 只读快照
    # ------------------------------------------------------------------
    def create_snapshot(self, snap_id):
        """把当前已提交映像固化为只读快照。

        只写一页快照描述符（页写原子）：掉电发生在描述符写入前则快照
        不存在，发生在其后则快照完整可见。空间不足且无可回收块时抛
        OutOfSpaceError，当前版本不受影响。snap_id 已存在抛 InvalidInputError。
        """
        snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
        if snap_id in self._snapshots:
            raise InvalidInputError(f"快照 {snap_id} 已存在，不得覆盖；请先删除")
        self._gc_spent = 0
        self._rebuild_view()
        if self._seq >= _SEQ_MAX:
            raise FTLError(f"提交序列号空间（{_SEQ_MAX}）已耗尽")
        ppn = self._ensure_free_page(set(), 0, self._gc_budget, GC_RESERVE_PAGES)
        commit_ppn = self._commit_ppn
        record = encode_snapshot_record(
            self._seq + 1,
            _SNAP_OP_CREATE,
            snap_id,
            self._seq if commit_ppn is not None else 0,
            _SNAP_COMMIT_NONE if commit_ppn is None else commit_ppn,
        )
        self._pinned.add(ppn)
        try:
            self._program(ppn, record, purpose="snapshot-create")
        finally:
            self._pinned.discard(ppn)
        self._seq += 1
        self._snap_records[ppn] = (
            self._seq,
            _SNAP_OP_CREATE,
            snap_id,
            self._seq - 1 if commit_ppn is not None else 0,
            _SNAP_COMMIT_NONE if commit_ppn is None else commit_ppn,
        )
        self._snapshots[snap_id] = {
            "commit_seq": self._seq - 1 if commit_ppn is not None else 0,
            "commit_ppn": commit_ppn,
            "mapping": list(self._mapping),
            "desc_ppn": ppn,
            "op_seq": self._seq,
        }
        self._rebuild_view()
        self._logical_log.append(
            {"op": "snapshot-create", "snap_id": snap_id, "seq": self._seq}
        )

    def delete_snapshot(self, snap_id):
        """删除快照：写入一页墓碑描述符。仅在墓碑完整写入后，快照引用的
        旧版本页才降为可回收；失败不改变现有快照。"""
        snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
        if snap_id not in self._snapshots:
            raise InvalidInputError(f"快照 {snap_id} 不存在（或已删除）")
        self._gc_spent = 0
        self._rebuild_view()
        if self._seq >= _SEQ_MAX:
            raise FTLError(f"提交序列号空间（{_SEQ_MAX}）已耗尽")
        # 墓碑是释放快照占用空间的唯一途径：允许它使用 GC 保留页，
        # 否则“设备恰好只剩保留页且无其他可回收块”时将永久无法删除快照。
        ppn = self._ensure_free_page(set(), 0, self._gc_budget, 0)
        record = encode_snapshot_record(
            self._seq + 1, _SNAP_OP_DELETE, snap_id, 0, _SNAP_COMMIT_NONE
        )
        self._pinned.add(ppn)
        try:
            self._program(ppn, record, purpose="snapshot-delete")
        finally:
            self._pinned.discard(ppn)
        self._seq += 1
        self._snap_records[ppn] = (
            self._seq, _SNAP_OP_DELETE, snap_id, 0, _SNAP_COMMIT_NONE,
        )
        del self._snapshots[snap_id]
        self._rebuild_view()
        self._logical_log.append(
            {"op": "snapshot-delete", "snap_id": snap_id, "seq": self._seq}
        )

    def read_snapshot(self, snap_id, lpn):
        """从只读快照读取逻辑页：返回 32 字节槽；快照中未映射返回 None。"""
        snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
        lpn = require_int("lpn", lpn, 0, LOGICAL_PAGES - 1)
        snap = self._snapshots.get(snap_id)
        if snap is None:
            raise InvalidInputError(f"快照 {snap_id} 不存在（或已删除）")
        ppn = snap["mapping"][lpn]
        if ppn == _UNMAPPED:
            self._logical_log.append(
                {"op": "snapshot-read", "snap_id": snap_id, "lpn": lpn,
                 "result": "unmapped"}
            )
            return None
        data = self._dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
        self._logical_log.append(
            {"op": "snapshot-read", "snap_id": snap_id, "lpn": lpn,
             "result": "ok", "ppn": ppn}
        )
        return data

    def list_snapshots(self):
        """返回存活快照 ID 的有序元组。"""
        return tuple(sorted(self._snapshots))

    def snapshot_info(self, snap_id):
        """返回快照信息：{'snap_id', 'commit_seq', 'op_seq', 'mapped_pages'}。"""
        snap_id = require_int("snap_id", snap_id, 0, _SNAP_ID_MAX)
        snap = self._snapshots.get(snap_id)
        if snap is None:
            raise InvalidInputError(f"快照 {snap_id} 不存在（或已删除）")
        return {
            "snap_id": snap_id,
            "commit_seq": snap["commit_seq"],
            "op_seq": snap["op_seq"],
            "mapped_pages": sum(
                1 for p in snap["mapping"] if p != _UNMAPPED
            ),
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
            "snapshots": len(self._snapshots),
            "snapshot_ids": self.list_snapshots(),
            "snapshot_retained_pages": sum(
                v.snap_data + v.snap_meta for v in self._view
            ),
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
            # 含任何存活快照引用页（数据页/提交记录页/描述符页）的块
            # 绝不能作为受害块：这些页在快照删除前不得被擦掉
            if v.snap_data > 0 or v.snap_meta > 0:
                continue
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
