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


def part_trim():
    print("=== 第三部分（逻辑页释放 trim）：整组原子释放、断电恢复、非法整组拒绝 ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    expected = {}
    for i in range(10):
        ftl.write(i, payload(i))
        expected[i] = payload(i)
    show_capacity("释放前", ftl)

    n0 = len(dev.action_log())
    released = ftl.trim([1, 3, 5, 3, 20])  # 3 重复、20 未映射：幂等处理
    actions = dev.action_log()[n0:]
    for lpn in (1, 3, 5):
        del expected[lpn]
    ok = all(ftl.read(l) is None for l in (1, 3, 5, 20)) and all(
        ftl.read(l) == d.ljust(PAGE_SIZE, b"\xff") for l, d in expected.items()
    )
    print(
        f"  trim([1,3,5,3,20]) 实际释放 {released} 页，物理动作 "
        f"{len(actions)} 个（{actions[0].purpose}）：释放后读回与其余页保持: {ok}"
    )
    show_capacity("释放后", ftl)

    print("  -- 注入掉电：trim 提交记录落页完成的瞬间断电 --")
    dev.set_crash_plan([1])  # trim 的唯一物理动作（trim-commit）完成后掉电
    dev.arm_crash()
    try:
        ftl.trim([0, 2, 4])
        print("  错误：应发生掉电")
    except PowerLossError as e:
        print(f"  捕获 PowerLossError: {e}")
    dev.reboot()
    ftl2 = FTL(dev)  # 重启重挂载：提交已完成 → 整组释放全部可见
    for lpn in (0, 2, 4):
        del expected[lpn]
    gone = all(ftl2.read(l) is None for l in (0, 2, 4))
    kept = all(ftl2.read(l) == d.ljust(PAGE_SIZE, b"\xff") for l, d in expected.items())
    print(f"  重启恢复：整组释放全部生效={gone}，其余页无损={kept}（无半释放状态）")

    print("  -- 释放后重写：照旧走新页分配 --")
    ftl2.write(2, b"REWRITTEN")
    expected[2] = b"REWRITTEN"
    print(f"  重写 lpn=2 并读回: {ftl2.read(2)[:9].decode()!r}")
    reclaimed = ftl2.gc_collect()
    ok = all(ftl2.read(l) == d.ljust(PAGE_SIZE, b"\xff") for l, d in expected.items())
    print(f"  手动 GC 回收 {reclaimed} 块（含 trim 死页），活页数据完整: {ok}")
    show_capacity("GC 后", ftl2)

    print("  -- 非法页号：整次请求在任何物理动作之前拒绝 --")
    n0 = len(dev.action_log())
    seq = ftl2.commit_seq
    try:
        ftl2.trim([6, 24])  # 24 越界 → 整组拒绝，lpn=6 也不得释放
        print("  错误：应拒绝非法页号")
    except InvalidInputError as e:
        print(f"  捕获 InvalidInputError: {e}")
    untouched = (
        len(dev.action_log()) == n0
        and ftl2.commit_seq == seq
        and ftl2.read(6) == expected[6].ljust(PAGE_SIZE, b"\xff")
    )
    print(f"  零物理动作、序列号未推进、lpn=6 未被波及: {untouched}")
    n0 = len(dev.action_log())
    r = ftl2.trim([])  # 空列表：幂等空操作
    print(f"  trim([]) 返回 {r}，物理动作增加 {len(dev.action_log()) - n0} 个")


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


if __name__ == "__main__":
    part_normal()
    part_trim()
    part_boundary()
    print("演示结束。")
