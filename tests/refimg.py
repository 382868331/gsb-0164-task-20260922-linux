"""独立参考实现：不调用 ftl 核心算法得到期望值。

- parse_committed_image：仅用 RawNand 的只读设备接口扫描物理页，
  独立复现“提交记录解析 + 序列号仲裁”，重建最后已提交逻辑映像。
  格式常量在此独立重复定义，是被测核心之外的第二份实现。
- assert_image_matches：用测试侧自行维护的写影子字典（只记录调用
  序列，不调用被测核心）与 FTL 读回结果逐页比较（往返一致，仅作补充）。
"""
import zlib

BLOCKS = 16
PAGES_PER_BLOCK = 8
PAGE_SIZE = 32
LOGICAL_PAGES = 24
MAGIC = 0xA5
SNAP_MAGIC = 0x5A
UNMAPPED = 0xFF


def _parse_record(raw, self_ppn):
    if len(raw) != PAGE_SIZE or raw[0] != MAGIC:
        return None
    if (zlib.crc32(raw[:28]) & 0xFFFFFFFF) != int.from_bytes(raw[28:32], "little"):
        return None
    seq = int.from_bytes(raw[1:4], "little")
    mapping = list(raw[4:28])
    seen = set()
    for x in mapping:
        if x == UNMAPPED:
            continue
        if x >= BLOCKS * PAGES_PER_BLOCK or x in seen or x == self_ppn:
            return None
        seen.add(x)
    return seq, mapping


def parse_committed_image(dev):
    """扫描设备，返回 {lpn: 32 字节页内容}（最后一条完整提交记录的映像）。"""
    best = None  # (seq, ppn, mapping)
    for b in range(BLOCKS):
        if dev.is_bad_block(b):
            continue
        for p in range(PAGES_PER_BLOCK):
            rec = _parse_record(dev.read_page(b, p), b * PAGES_PER_BLOCK + p)
            if rec is None:
                continue
            seq, mapping = rec
            ppn = b * PAGES_PER_BLOCK + p
            if best is None or (seq, ppn) > (best[0], best[1]):
                best = (seq, ppn, mapping)
    image = {}
    if best is not None:
        for lpn, ppn in enumerate(best[2]):
            if ppn != UNMAPPED:
                image[lpn] = dev.read_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
    return image


def assert_image_matches(case, ftl, shadow):
    """shadow: {lpn: 原始写入字节串}；FTL 读回应等于其 0xFF 填充的 32 字节槽。"""
    for lpn in range(LOGICAL_PAGES):
        got = ftl.read(lpn)
        exp = shadow.get(lpn)
        if exp is None:
            case.assertIsNone(got, f"lpn={lpn} 应为未映射")
        else:
            case.assertEqual(got, exp.ljust(PAGE_SIZE, b"\xff"), f"lpn={lpn} 数据不符")


def assert_matches_reference(case, ftl, dev):
    """FTL 读回结果须与独立参考解析器重建的已提交映像一致。"""
    image = parse_committed_image(dev)
    for lpn in range(LOGICAL_PAGES):
        got = ftl.read(lpn)
        exp = image.get(lpn)
        if exp is None:
            case.assertIsNone(got, f"lpn={lpn} 应为未映射（参考映像）")
        else:
            case.assertEqual(got, exp, f"lpn={lpn} 与参考映像不符")


def _parse_snapshot(raw, self_ppn):
    """独立解析快照描述符（不复用被测核心）。"""
    if len(raw) != PAGE_SIZE or raw[0] != SNAP_MAGIC:
        return None
    if (zlib.crc32(raw[:28]) & 0xFFFFFFFF) != int.from_bytes(raw[28:32], "little"):
        return None
    if any(b != 0xFF for b in raw[11:28]):
        return None
    op_seq = int.from_bytes(raw[1:4], "little")
    op = raw[4]
    snap_id = int.from_bytes(raw[5:7], "little")
    cseq = int.from_bytes(raw[7:10], "little")
    cppn = raw[10]
    if op_seq == 0 or op not in (0, 1) or self_ppn == cppn:
        return None
    if op == 1:
        if cppn != 0xFF and (cseq == 0 or cppn >= BLOCKS * PAGES_PER_BLOCK):
            return None
    elif (cseq, cppn) != (0, 0xFF):
        return None
    return op_seq, op, snap_id, cseq, cppn


def parse_snapshots(dev):
    """独立重建存活快照：{snap_id: {lpn: 页内容}}（空映像为 {}）。"""
    commits = {}  # ppn -> (seq, mapping)
    best = None
    for b in range(BLOCKS):
        if dev.is_bad_block(b):
            continue
        for p in range(PAGES_PER_BLOCK):
            ppn = b * PAGES_PER_BLOCK + p
            rec = _parse_record(dev.read_page(b, p), ppn)
            if rec is not None:
                commits[ppn] = rec
                if best is None or (rec[0], ppn) > (best[0], best[1]):
                    best = (rec[0], ppn, rec[1])
    winners = {}
    for b in range(BLOCKS):
        if dev.is_bad_block(b):
            continue
        for p in range(PAGES_PER_BLOCK):
            ppn = b * PAGES_PER_BLOCK + p
            srec = _parse_snapshot(dev.read_page(b, p), ppn)
            if srec is None:
                continue
            key = (srec[0], ppn)
            cur = winners.get(srec[2])
            if cur is None or key > (cur[0][0], cur[0][1]):
                winners[srec[2]] = (key, srec)
    out = {}
    for snap_id, (_, srec) in winners.items():
        op_seq, op, _, cseq, cppn = srec
        if op == 0:
            continue
        if cppn == 0xFF:
            out[snap_id] = {}
            continue
        hit = commits.get(cppn)
        if hit is None or hit[0] != cseq:
            continue
        image = {}
        for lpn, dppn in enumerate(hit[1]):
            if dppn != UNMAPPED:
                image[lpn] = dev.read_page(dppn // PAGES_PER_BLOCK, dppn % PAGES_PER_BLOCK)
        out[snap_id] = image
    return out
