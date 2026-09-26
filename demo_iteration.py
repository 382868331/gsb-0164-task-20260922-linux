"""本轮迭代演示：闪存块报废时把仍可读的数据安全搬出去。

运行：python demo_iteration.py（全程确定性真实计算，无 sleep、无预录结果）

包含两个场景：
1. 正常结果：块擦写失败 → 退役搬迁成功，逐个断电点核对后数据零损失；
2. 边界结果：指定页已不可读 → 退役如实报告受损逻辑页，拒绝伪造数据。
"""
from ftl import (
    BLOCKS,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    DataLossError,
    FTL,
    OutOfSpaceError,
    PowerLossError,
    RawNand,
)


def payload(i, size=24):
    """确定性示例数据（仓库内代码生成，无外部依赖）。"""
    return bytes(((i * 31 + j * 7 + 1) % 256 for j in range(size)))


def slot(data):
    return data.ljust(PAGE_SIZE, b"\xff")


def block_with_most_valid(ftl):
    counts = {}
    for ppn in ftl.mapping_snapshot():
        if ppn is not None:
            counts[ppn // PAGES_PER_BLOCK] = counts.get(ppn // PAGES_PER_BLOCK, 0) + 1
    return max(counts, key=lambda b: (counts[b], -b))


def check_truth(ftl, truth, damaged=()):
    """与独立真值字典逐页核对；受损页必须显式报 DataLossError。"""
    for lpn, data in truth.items():
        if lpn in damaged:
            try:
                ftl.read(lpn)
            except DataLossError:
                continue
            return False
        if ftl.read(lpn) != slot(data):
            return False
    return True


def part_success():
    print("=== 第一部分（正常结果）：擦写故障块退役，数据安全搬迁 ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    truth = {}
    for i in range(12):  # 12 个不同逻辑页
        ftl.write(i, payload(i))
        truth[i] = payload(i)

    block = block_with_most_valid(ftl)
    dev.inject_erase_failure(block)  # 注入故障：块不可再擦写，但仍可读
    print(f"  块 {block} 注入擦写故障（可读不可擦），当前坏块={list(dev.bad_blocks())}")

    result = ftl.retire_block(block)
    print(f"  retire_block({block}) -> 搬迁 {len(result['moved'])} 页，受损 {list(result['damaged'])}")
    print(f"  退役后坏块集合={list(dev.bad_blocks())}，受损逻辑页={list(ftl.damaged_pages())}")
    ok = check_truth(ftl, truth)
    print(f"  与独立真值字典逐页核对（12 个逻辑页）: {ok}")

    print("  -- 重放：在退役过程的每个物理动作后断电重开 --")
    dev2 = RawNand(max_erases=8)
    f = FTL(dev2)
    for i in range(12):
        f.write(i, payload(i))
    blk = block_with_most_valid(f)
    dev2.inject_erase_failure(blk)
    snap = dev2.snapshot()
    before = len(dev2.action_log())
    f.retire_block(blk)
    n_actions = len(dev2.action_log()) - before
    verified = 0
    for k in range(1, n_actions + 1):  # 逐个断电点重放并核对
        dev2.restore(snap)
        dev2.set_crash_plan([k])
        dev2.arm_crash()
        g = FTL(dev2)
        try:
            g.retire_block(blk)
            raise AssertionError("应发生掉电")
        except PowerLossError:
            pass
        dev2.reboot()
        g2 = FTL(dev2)  # 重开：核对映射、坏块集合与数据
        assert check_truth(g2, truth), f"断电点 {k} 数据不符"
        g2.retire_block(blk)  # 重试至完成
        assert blk in g2.capacity()["bad_blocks"]
        assert check_truth(g2, truth)
        verified += 1
    print(f"  共 {verified} 个断电点全部核对通过：旧映射在提交落稳前始终有效，无数据丢失")


def part_data_loss():
    print("=== 第二部分（边界结果）：页已不可读，如实报告数据损失 ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    truth = {}
    for i in range(12):
        ftl.write(i, payload(i))
        truth[i] = payload(i)

    block = block_with_most_valid(ftl)
    victim_lpn = next(
        lpn
        for lpn, ppn in enumerate(ftl.mapping_snapshot())
        if ppn is not None and ppn // PAGES_PER_BLOCK == block
    )
    ppn = ftl.mapping_snapshot()[victim_lpn]
    dev.inject_unreadable_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)
    print(f"  逻辑页 {victim_lpn}（物理页 {ppn}）注入不可读故障")

    result = ftl.retire_block(block)
    print(f"  retire_block({block}) -> 搬迁 {len(result['moved'])} 页，受损逻辑页 {list(result['damaged'])}")
    try:
        ftl.read(victim_lpn)
        print("  错误：受损页不应可读")
    except DataLossError as e:
        print(f"  读取受损逻辑页被拒绝 DataLossError: {e}")
    ok = check_truth(ftl, truth, damaged={victim_lpn})
    print(f"  其余 11 页与真值字典核对一致，受损页不伪造内容: {ok}")

    dev.reboot()
    ftl2 = FTL(dev)  # 断电重开后损失依然明确，不会“恢复”出假数据
    print(f"  重启后受损逻辑页仍报告为 {list(ftl2.damaged_pages())}，坏块={list(dev.bad_blocks())}")

    print("  -- 边界：搬迁空间不足时保留可恢复的原状态 --")
    dev3 = RawNand(max_erases=0)  # 禁止擦除：死页不可回收
    f3 = FTL(dev3)
    for i in range(60):  # 耗尽用户可用页，仅剩 8 页保留余量
        f3.write(i % 24, payload(i))
    failing = None
    for b in range(BLOCKS):
        if dev3.is_bad_block(b):
            continue
        try:
            f3.retire_block(b)
        except OutOfSpaceError:
            failing = b
            break
    seq = f3.commit_seq
    try:
        f3.retire_block(failing)
        print("  错误：应触发空间不足")
    except OutOfSpaceError as e:
        print(f"  退役块 {failing} 被拒绝 OutOfSpaceError: {e}")
    intact = (not dev3.is_bad_block(failing)) and f3.commit_seq == seq
    ok = check_truth(f3, {i % 24: payload(i) for i in range(36, 60)})
    print(f"  块未提前标坏、提交序列号不变: {intact}，全部数据仍可读: {ok}")


if __name__ == "__main__":
    part_success()
    part_data_loss()
    print("演示结束。")
