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


def part_transaction():
    print("=== 第三部分（原子事务）：整笔全有或全无、逐点掉电、容量先拒绝 ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    old = [(i, payload(100 + i)) for i in range(4)]
    for lpn, data in old:
        ftl.write(lpn, data)
    new = [(i, payload(200 + i)) for i in range(4)]

    seq0 = ftl.commit_seq
    ftl.transaction(new)  # 4 页一笔事务：4 个数据页 + 1 条提交记录
    ok = all(ftl.read(l) == d.ljust(PAGE_SIZE, b"\xff") for l, d in new)
    print(f"  4 页事务提交成功，4 页全部读回新值: {ok}，"
          f"提交序列号 {seq0} -> {ftl.commit_seq}（恰好 +1）")

    print("  -- 注入掉电：事务第 2 个物理动作（第 2 个数据页）后断电 --")
    newer = [(i, payload(250 + i)) for i in range(4)]
    dev.set_crash_plan([2])
    dev.arm_crash()
    try:
        ftl.transaction(newer)
        print("  错误：应发生掉电")
    except PowerLossError as e:
        print(f"  捕获 PowerLossError: {e}")
    dev.reboot()
    ftl2 = FTL(dev)  # 重启后重新挂载 = 恢复
    kept = all(ftl2.read(l) == d.ljust(PAGE_SIZE, b"\xff") for l, d in new)
    leaked = any(ftl2.read(l) == d.ljust(PAGE_SIZE, b"\xff") for l, d in newer)
    print(f"  重启恢复：整笔旧值保留={kept}，半笔新值泄漏={leaked}（应为 False）")

    print("  -- 容量不足必须先拒绝（max_erases=0，空白页不足以容纳整笔） --")
    dev2 = RawNand(max_erases=0)  # 禁止擦除：死页不可回收
    ftl3 = FTL(dev2)
    i = 0
    while ftl3.capacity()["free_physical_pages"] > 8:  # 写满到 GC 保留页边界
        ftl3.write(i % 24, payload(i))
        i += 1
    n_actions = len(dev2.action_log())
    try:
        ftl3.transaction([(0, b"a"), (1, b"b"), (2, b"c"), (3, b"d")])
        print("  错误：应触发空间不足")
    except OutOfSpaceError as e:
        print(f"  4 页事务被拒绝 OutOfSpaceError: {e}")
    untouched = len(dev2.action_log()) == n_actions
    print(f"  拒绝发生在任何事务写入之前（物理动作数不变）: {untouched}")


if __name__ == "__main__":
    part_normal()
    part_boundary()
    part_transaction()
    print("演示结束。")
