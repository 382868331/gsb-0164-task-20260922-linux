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


def part_trim():
    print("=== 第三部分（逻辑页释放 trim）：整组原子生效、断电恢复、非法拒绝 ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    shadow = {}
    for i in range(12):
        ftl.write(i, payload(i))
        shadow[i] = payload(i)
    n0 = len(ftl.physical_log())
    released = ftl.trim([2, 5, 7, 7])  # 重复页号按幂等处理
    n1 = len(ftl.physical_log())
    for l in (2, 5, 7):
        shadow.pop(l)
    none_ok = all(ftl.read(l) is None for l in (2, 5, 7))
    kept = all(ftl.read(l) == shadow[l].ljust(PAGE_SIZE, b"\xff") for l in shadow)
    print(f"  trim([2,5,7,7]) 实际释放 {released} 页，物理动作 {n1 - n0} 次（单条提交记录）")
    print(f"  释放后读返回 None: {none_ok}；其余活页数据保留: {kept}")
    show_capacity("trim 后", ftl)
    reclaimed = ftl.gc_collect()
    kept = all(ftl.read(l) == shadow[l].ljust(PAGE_SIZE, b"\xff") for l in shadow)
    print(f"  手动 GC 回收 {reclaimed} 块（含被释放死页），活页数据仍一致: {kept}")
    show_capacity("GC 后", ftl)

    print("  -- 释放后重写：照旧走分配流程 --")
    ftl.write(5, b"REWRITE")
    print(f"  重写 lpn=5 并读回: {ftl.read(5)[:7].decode()!r}")

    print("  -- trim 中途掉电：恢复后只见全部释放前或全部释放后 --")
    dev2 = RawNand(max_erases=8)
    for b in range(9):
        dev2.mark_factory_bad(b)  # 仅剩 7 块：trim 的提交页必须先做带搬迁的 GC
    ftl2 = FTL(dev2)
    i = 0
    while ftl2.capacity()["free_physical_pages"] > 8:
        ftl2.write(i % 24, payload(i))
        i += 1
    snap = dev2.snapshot()
    n0 = len(dev2.action_log())
    ftl2.trim([0, 1, 2, 3])
    total = len(dev2.action_log()) - n0
    mid = total // 2  # 提交记录落盘之前的某个断电点（GC 搬迁中途）
    dev2.restore(snap)
    dev2.set_crash_plan([mid])
    dev2.arm_crash()
    try:
        FTL(dev2).trim([0, 1, 2, 3])
        print("  错误：应发生掉电")
    except PowerLossError as e:
        print(f"  trim 第 {mid}/{total} 个物理动作后掉电: {e}")
    dev2.reboot()
    ftl3 = FTL(dev2)
    kept = all(ftl3.read(l) == payload(l).ljust(PAGE_SIZE, b"\xff") for l in (0, 1, 2, 3))
    print(f"  恢复：提交未落盘 → 4 页全部保持释放前映射: {kept}")
    dev2.restore(snap)  # 重放同一轨迹，这次让提交落盘后才掉电
    dev2.set_crash_plan([total])
    dev2.arm_crash()
    try:
        FTL(dev2).trim([0, 1, 2, 3])
        print("  错误：应发生掉电")
    except PowerLossError as e:
        print(f"  trim 第 {total}/{total} 个物理动作后掉电: {e}")
    dev2.reboot()
    ftl4 = FTL(dev2)
    gone = all(ftl4.read(l) is None for l in (0, 1, 2, 3))
    print(f"  恢复：提交已落盘 → 4 页全部释放生效: {gone}")

    print("  -- 非法页号：任何物理动作之前整次拒绝 --")
    n0 = len(ftl4.physical_log())
    try:
        ftl4.trim([5, 24])  # 24 越界：整次拒绝，合法的 5 也不释放
        print("  错误：应拒绝非法页号")
    except InvalidInputError as e:
        print(f"  trim([5, 24]) 被拒绝 InvalidInputError: {e}")
    unchanged = len(ftl4.physical_log()) == n0
    alive = ftl4.read(5) == payload(5).ljust(PAGE_SIZE, b"\xff")
    print(f"  拒绝前后物理动作数不变: {unchanged}；同组合法页 lpn=5 未受影响: {alive}")


if __name__ == "__main__":
    part_normal()
    part_boundary()
    part_trim()
    print("演示结束。")
