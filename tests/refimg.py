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
TOMB_MAGIC = 0x5B
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


def parse_snapshots(dev):
    """独立扫描设备，返回 {snap_id: {lpn: 32 字节页内容}}（仅有效快照）。

    独立复现“快照创建记录 + 墓碑仲裁”：快照 ID 不复用，存在墓碑即删除。
    不调用 ftl 核心代码，格式常量在此独立维护。
    """
    creates = {}  # sid -> (ppn, mapping)
    tombs = set()
    for b in range(BLOCKS):
        if dev.is_bad_block(b):
            continue
        for p in range(PAGES_PER_BLOCK):
            ppn = b * PAGES_PER_BLOCK + p
            raw = dev.read_page(b, p)
            if raw[0] == SNAP_MAGIC:
                if (zlib.crc32(raw[:27]) & 0xFFFFFFFF) != int.from_bytes(
                    raw[27:31], "little"
                ) or raw[31] != UNMAPPED:
                    continue
                sid = int.from_bytes(raw[1:3], "little")
                mapping = list(raw[3:27])
                ok = ppn not in mapping and all(
                    x == UNMAPPED or x < BLOCKS * PAGES_PER_BLOCK for x in mapping
                ) and len(set(x for x in mapping if x != UNMAPPED)) == len(
                    [x for x in mapping if x != UNMAPPED]
                )
                if ok and (sid not in creates or ppn > creates[sid][0]):
                    creates[sid] = (ppn, mapping)
            elif raw[0] == TOMB_MAGIC:
                if (zlib.crc32(raw[:3]) & 0xFFFFFFFF) == int.from_bytes(
                    raw[3:7], "little"
                ) and all(x == UNMAPPED for x in raw[7:]):
                    tombs.add(int.from_bytes(raw[1:3], "little"))
    result = {}
    for sid, (_, mapping) in creates.items():
        if sid in tombs:
            continue
        image = {}
        for lpn, dppn in enumerate(mapping):
            if dppn != UNMAPPED:
                image[lpn] = dev.read_page(dppn // PAGES_PER_BLOCK, dppn % PAGES_PER_BLOCK)
        result[sid] = image
    return result
