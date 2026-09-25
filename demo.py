"""固定输入演示：正常结果 + 实际触发的拒绝/异常边界。

运行：python demo.py（全程确定性计算，无 sleep、无预录结果，约 1 秒内完成）
"""
from collections import Counter

from ftl import (
    FTL,
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


def part_wear_level():
    print("=== 第三部分（主动磨损均衡 wear_level）：冷页搬迁、预算报告、断电恢复 ===")
    # 确定性构造：块 0/1 装满冷数据（擦除 0 次），块 12/13 已擦 3 次、
    # 其余空白块已擦 2 次；提交记录用公开编码器写入块 2。
    dev = RawNand(max_erases=10)
    for b, times in [(12, 3), (13, 3)] + [(b, 2) for b in range(2, 16) if b not in (12, 13)]:
        for _ in range(times):
            dev.erase(b, purpose="setup-wear")
    mapping = [0xFF] * 24
    expected = {}
    for b, lpns in ((0, range(0, 8)), (1, range(8, 16))):
        for i, lpn in enumerate(lpns):
            data = payload(lpn)
            dev.program(b, i, data, purpose="cold-data")
            mapping[lpn] = b * 8 + i
            expected[lpn] = data
    dev.program(2, 0, encode_commit_record(1, mapping), purpose="setup-commit")

    ftl = FTL(dev)
    counts = ftl.capacity()["erase_counts"]
    print(f"  维护前擦除次数: 块0={counts[0]} 块1={counts[1]} 块12={counts[12]} 块13={counts[13]}")

    snap = dev.snapshot()
    report = ftl.wear_level(64, threshold=2)
    print(f"  wear_level(64, threshold=2) 报告: {report}")
    counts = ftl.capacity()["erase_counts"]
    print(f"  维护后擦除次数: 块0={counts[0]} 块1={counts[1]} 块12={counts[12]} 块13={counts[13]}")
    ok = all(ftl.read(l) == expected[l].ljust(PAGE_SIZE, b"\xff") for l in expected)
    print(f"  搬迁后 16 个冷页逻辑内容全部不变: {ok}")
    landed = {ftl.mapping_snapshot()[l] // 8 for l in range(16)}
    print(f"  冷页现位于块 {sorted(landed)}（擦写较多的空块），块 0/1 已腾空待重新分配")

    print("  -- 预算按实际物理动作计：max_moves=10 恰好够整理一个块 --")
    dev.restore(snap)
    ftl = FTL(dev)
    report = ftl.wear_level(10, threshold=2)
    print(f"  wear_level(10, threshold=2) 报告: {report}（预算耗尽与空间不足分开报告）")

    print("  -- 枚举维护过程每个断电点：掉电重挂载后逻辑映像不得丢失 --")
    dev.restore(snap)
    ftl = FTL(dev)
    total = ftl.wear_level(64, threshold=2)["actions"]
    recovered = 0
    for k in range(1, total + 1):
        dev.restore(snap)
        dev.set_crash_plan([k])
        dev.arm_crash()
        try:
            FTL(dev).wear_level(64, threshold=2)
            print("  错误：应发生掉电")
            return
        except PowerLossError:
            pass
        dev.reboot()
        ftl2 = FTL(dev)  # 重启重挂载 = 恢复
        if all(ftl2.read(l) == expected[l].ljust(PAGE_SIZE, b"\xff") for l in expected):
            recovered += 1
    print(f"  枚举 {total} 个断电点：{recovered}/{total} 恢复到最后已提交映像")


if __name__ == "__main__":
    part_normal()
    part_boundary()
    part_wear_level()
    print("演示结束。")
