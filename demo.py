"""固定输入演示：正常结果 + 实际触发的拒绝/异常边界。

运行：python demo.py（全程确定性计算，无 sleep、无预录结果，约 1 秒内完成）
"""
from collections import Counter

from ftl import (
    FTL,
    BLOCKS,
    PAGE_SIZE,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
    RawNand,
    encode_commit_record,
    require_finite_number,
)
from ftl.errors import BlockWornError


def payload(i, size=24):
    """确定性示例数据（仓库内代码生成，无外部依赖）。"""
    return bytes(((i * 31 + j * 7 + 1) % 256 for j in range(size)))


def show_capacity(tag, ftl):
    cap = ftl.capacity()
    print(
        f"  [{tag}] 空白物理页={cap['free_physical_pages']:3d} "
        f"可回收死页={cap['reclaimable_dead_pages']:3d} "
        f"已映射逻辑页={cap['mapped_pages']:2d}/{cap['logical_pages']} "
        f"坏块={list(cap['bad_blocks'])} 磨损块={list(cap['worn_blocks'])} "
        f"提交序列号={cap['commit_seq']}"
    )


def part_normal():
    print("=== 第一部分（正常结果）：读写、垃圾整理、断电重启恢复 ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)

    expected = {}
    for i in range(80):  # 80 次覆盖写，必然触发垃圾整理
        lpn = i % 24
        ftl.write(lpn, payload(i))
        expected[lpn] = payload(i)
    ok = all(ftl.read(l) == expected[l].ljust(PAGE_SIZE, b"\xff") for l in expected)
    print(f"  80 次覆盖写后 24 个逻辑页全部读回一致: {ok}")
    show_capacity("GC 后", ftl)

    kinds = Counter((a.kind, a.purpose) for a in ftl.physical_log())
    print("  物理动作日志统计: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    print(f"  逻辑日志最近 2 条: {list(ftl.logical_log())[-2:]}")

    reclaimed = ftl.gc_collect()  # 手动整理：含有效页的块也会先搬迁再擦除
    ok = all(ftl.read(l) == expected[l].ljust(PAGE_SIZE, b"\xff") for l in expected)
    print(f"  手动垃圾整理回收块数={reclaimed}，整理后数据仍全部一致: {ok}")
    kinds = Counter((a.kind, a.purpose) for a in ftl.physical_log())
    print("  物理动作日志统计: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    show_capacity("手动 GC 后", ftl)

    print("  -- 注入掉电：下一次写的第 1 个物理动作后断电 --")
    dev.set_crash_plan([1])
    dev.arm_crash()
    try:
        ftl.write(7, b"LOST-WRITE")
        print("  错误：应发生掉电")
    except PowerLossError as e:
        print(f"  捕获 PowerLossError: {e}")
    dev.reboot()
    ftl2 = FTL(dev)  # 重启后重新挂载 = 恢复
    lost = ftl2.read(7) != b"LOST-WRITE".ljust(PAGE_SIZE, b"\xff")
    kept = ftl2.read(7) == expected[7].ljust(PAGE_SIZE, b"\xff")
    print(f"  重启恢复：未提交写已丢失={lost}，最后已提交映像保留={kept}")
    ftl2.write(7, b"RECOVERED")
    print(f"  恢复后继续写并读回: {ftl2.read(7)[:9].decode()!r}")
    show_capacity("恢复后", ftl2)


def part_boundary():
    print("=== 第二部分（实际触发的拒绝/异常边界）：磨损极限与非法输入 ===")
    dev = RawNand(max_erases=1)  # 每块只允许擦除 1 次
    ftl = FTL(dev)
    count = 0
    try:
        for i in range(400):
            ftl.write(i % 24, payload(i))
            count += 1
        print("  错误：应触发空间不足")
    except OutOfSpaceError as e:
        print(f"  磨损极限：第 {count + 1} 次写被拒绝 OutOfSpaceError: {e}")
    cap = ftl.capacity()
    print(f"  磨损块={list(cap['worn_blocks'])}，各块擦除次数={list(cap['erase_counts'])}")
    worn = cap["worn_blocks"][0]
    try:
        dev.erase(worn)
        print("  错误：应触发磨损拒绝")
    except BlockWornError as e:
        print(f"  直接擦除磨损块 {worn} 被拒绝 BlockWornError: {e}")

    cases = [
        ("lpn=True（整数参数拒绝 bool）", lambda: ftl.write(True, b"x")),
        ("data 33 字节（超过页长 32）", lambda: ftl.write(0, b"z" * 33)),
        ("max_erases=float('inf')", lambda: RawNand(max_erases=float("inf"))),
        ("数值参数 NaN", lambda: require_finite_number("ratio", float("nan"))),
    ]
    for label, thunk in cases:
        try:
            thunk()
            print(f"  错误：{label} 应被拒绝")
        except InvalidInputError as e:
            print(f"  非法输入被拒绝 [{label}]: {e}")


def build_cold_hot_layout():
    """真实写负载确定性地构造冷热分化：4 坏块；24 冷页写一次，3 热页反复覆盖。"""
    dev = RawNand(max_erases=32)
    for b in range(4):
        dev.mark_factory_bad(b)
    ftl = FTL(dev)
    shadow = {}
    for i in range(24):
        d = payload(0x10 + i)
        ftl.write(i, d)
        shadow[i] = d
    for r in range(60):
        for h in (0, 1, 2):
            d = payload(0x100 + r * 3 + h)
            ftl.write(h, d)
            shadow[h] = d
    return dev, ftl, shadow


def image_ok(ftl, shadow):
    return all(
        ftl.read(l) == shadow[l].ljust(PAGE_SIZE, b"\xff") for l in shadow
    ) and all(ftl.read(l) is None for l in range(24) if l not in shadow)


def print_layout(tag, dev, ftl):
    counts = ftl.capacity()["erase_counts"]
    rows = []
    for b in range(4, BLOCKS):
        v = ftl._view[b]
        rows.append(
            f"块{b:2d}(ec={dev.erase_count(b)},有效{v.valid},空白{v.blanks})"
        )
    print(f"  [{tag}] " + " ".join(rows))
    print(f"  擦除次数序列（块4..15）: {counts[4:]}")


def part_wear_level():
    print("=== 第三部分（显式磨损均衡）：冷页搬迁、断电重挂载、无候选与空间不足 ===")
    dev, ftl, shadow = build_cold_hot_layout()
    print_layout("维护前", dev, ftl)
    image_before = {l: ftl.read(l) for l in range(24)}
    coldest = min(
        b
        for b in range(BLOCKS)
        if not dev.is_bad_block(b) and dev.erase_count(b) == 0
    )
    print(f"  最冷块 {coldest}（ec=0）持有 {ftl._view[coldest].valid} 个长期不动的有效页")

    # 一次性有限维护：预算 3 个有效页，恰好腾空最冷块
    n0 = len(dev.action_log())
    report = ftl.wear_level(3)
    actions = dev.action_log()[n0:]
    print(f"  wear_level(3) 报告: {report}")
    print("  实际物理动作（按实结算）: " + ", ".join(
        f"{a.kind}:块{a.block}" + (f"/页{a.page}" if a.page is not None else "")
        + f"({a.purpose})"
        for a in actions
    ))
    identical = all(ftl.read(l) == image_before[l] for l in range(24))
    print(f"  维护前后逻辑映像逐页一致: {identical}；影子字典往返一致: {image_ok(ftl, shadow)}")
    print(f"  源块 {coldest} 擦除次数={dev.erase_count(coldest)}，"
          f"空白页={ftl._view[coldest].blanks}（已可重新分配）")

    # 枚举这 5 个物理动作的每个断电点：重启重挂载后映像必须仍是维护前映像
    print("  -- 枚举维护内部每个动作后的掉电，reboot()+重挂载核对 --")
    dev2, _, shadow2 = build_cold_hot_layout()
    snap = dev2.snapshot()
    base = len(dev2.action_log())
    probe = FTL(dev2)
    probe.wear_level(3)
    total = len(dev2.action_log()) - base
    all_ok = True
    for k in range(1, total + 1):
        dev2.restore(snap)
        dev2.set_crash_plan([k])
        dev2.arm_crash()
        ftl_c = FTL(dev2)
        try:
            ftl_c.wear_level(3)
            raised = False
        except PowerLossError:
            raised = True
        dev2.reboot()
        ftl_r = FTL(dev2)
        ok = raised and image_ok(ftl_r, shadow2)
        all_ok = all_ok and ok
        last_a = dev2.action_log()[base + k - 1].purpose
        print(f"    第 {k}/{total} 个动作({last_a})后掉电: 掉电发生={raised}，"
              f"重挂载映像完整={ok}")
    print(f"  全部 {total} 个断电点恢复映像均与维护前一致: {all_ok}")

    # 低磨损块重新进入分配
    ftl.write(5, b"post-level")
    landed = ftl.mapping_snapshot()[5] // 8
    print(f"  维护后新写 lpn5 落在块 {landed}（=被腾空的最冷块 {coldest}）: "
          f"{landed == coldest}")

    # 边界 1：全新设备等磨损 → 无候选、无动作
    d3 = RawNand(max_erases=8)
    f3 = FTL(d3)
    f3.write(0, b"equal-wear")
    r3 = f3.wear_level(4)
    print(f"  等磨损设备（无擦写更多的块）: stop_reason={r3['stop_reason']}, "
          f"moved={r3['moved_pages']}, 物理动作={r3['program_actions'] + r3['erase_actions']}")

    # 边界 2：仅剩一个可用空块且仅 1 个空白页 → 空间不足（与预算耗尽分开）
    d4 = RawNand(max_erases=8)
    for b in range(14):
        d4.mark_factory_bad(b)
    d4.program(14, 0, payload(0x80), purpose="history")
    d4.program(14, 1, payload(0x81), purpose="history")
    mapping = [0xFF] * 24
    mapping[0], mapping[1] = 14 * 8, 14 * 8 + 1
    d4.program(14, 2, encode_commit_record(1, mapping), purpose="history")
    d4.erase(15)
    for p in range(7):
        d4.program(15, p, payload(0x40 + p), purpose="history")
    f4 = FTL(d4)
    r4 = f4.wear_level(4)
    print(f"  仅剩 1 个空白页（需 3 页）: stop_reason={r4['stop_reason']}, "
          f"moved={r4['moved_pages']}, 物理动作={r4['program_actions'] + r4['erase_actions']}"
          "（空间不足与预算耗尽分开报告）")


if __name__ == "__main__":
    part_normal()
    part_wear_level()
    part_boundary()
    print("演示结束。")
