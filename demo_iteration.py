"""本轮迭代演示：坏块退役搬迁 + 指定页不可读的数据损失报告。

运行：python demo_iteration.py
全程确定性真实计算（无 sleep、无预录输出，通常 1 秒内完成，远低于 90 秒）。

内容：
1. 正常退役：注入“块不可再擦写但可读”故障，retire_block 把仍可读的
   有效页搬到预留块；并枚举该退役过程每个物理动作后的断电重开，
   用独立逻辑真值字典核对映射、坏块集合与数据。
2. 数据损失：注入“指定页不可读”故障，退役时无法凭空恢复，明确上报
   受损逻辑页；重启后读该逻辑页被显式阻止（不返回伪造内容），
   主机重写后恢复。
"""
from ftl import (
    FTL,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    EraseFailureError,
    LogicalDataLossError,
    PowerLossError,
    RawNand,
    UnreadablePageError,
)


def payload(i, size=20):
    return bytes(((i * 17 + j * 5 + 3) % 256 for j in range(size)))


def check_truth(ftl, truth, lost):
    """独立逻辑真值对照：truth={lpn: 原始 bytes}，lost=受损逻辑页集合。"""
    for lpn in range(24):
        if lpn in lost:
            try:
                ftl.read(lpn)
            except LogicalDataLossError:
                continue
            raise AssertionError(f"lpn={lpn} 应报告数据损失")
        elif lpn in truth:
            got = ftl.read(lpn)
            assert got == truth[lpn].ljust(PAGE_SIZE, b"\xff"), f"lpn={lpn} 不符"
        else:
            assert ftl.read(lpn) is None, f"lpn={lpn} 应为未映射"


def scenario_successful_retire():
    print("=== 场景一：擦写故障块成功退役（有效页搬迁 + 逐动作断电核对） ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    truth = {}
    for i in range(40):  # 40 次覆盖写，含 GC，占用多块
        lpn = i % 24
        ftl.write(lpn, payload(i))
        truth[lpn] = payload(i)

    victim = next(
        p // PAGES_PER_BLOCK for p in ftl.mapping_snapshot() if p is not None
    )
    lpns = [
        l for l, p in enumerate(ftl.mapping_snapshot())
        if p is not None and p // PAGES_PER_BLOCK == victim
    ]
    print(f"  受害块={victim}，其上有效逻辑页={sorted(lpns)}")
    dev.inject_block_erase_failure(victim)
    try:
        dev.erase(victim)
    except EraseFailureError:
        print("  注入故障后：erase/program 被拒（EraseFailureError），read_page 仍可读")

    # 枚举退役过程中每个物理动作后的断电点
    snap = dev.snapshot()
    n0 = len(snap["log"])
    FTL(dev).retire_block(victim)
    total = len(dev.action_log()) - n0
    verified = 0
    for k in range(1, total + 1):
        dev.restore(snap)
        dev.inject_block_erase_failure(victim)
        dev.set_crash_plan([k])
        dev.arm_crash()
        ftl_c = FTL(dev)
        try:
            ftl_c.retire_block(victim)
        except PowerLossError:
            pass
        dev.reboot()
        ftl_r = FTL(dev)
        if k < total:
            assert victim not in dev.bad_blocks(), "提交落稳前块不得提前退役"
            for lpn in lpns:
                assert ftl_r.mapping_snapshot()[lpn] // PAGES_PER_BLOCK == victim
        else:
            assert victim in dev.bad_blocks(), "提交落稳、重开后续做退役"
        check_truth(ftl_r, truth, set())
        ftl_r.retire_block(victim)  # 收敛：重放后必成功
        check_truth(ftl_r, truth, set())
        verified += 1
    print(f"  枚举 {total} 个物理动作断电点并全部重开核对：{verified == total}")

    dev.restore(snap)
    dev.inject_block_erase_failure(victim)
    ftl = FTL(dev)
    report = ftl.retire_block(victim)
    print(
        f"  退役报告：搬迁 {len(report['migrated'])} 页，受损逻辑页={report['damaged_lpns']}，"
        f"提交序列号 {report['commit_seq'] - 1} -> {report['commit_seq']}"
    )
    assert victim in dev.bad_blocks()
    # 坏块今后不再进入分配：再写 30 次，动作日志不碰该块
    mark = len(dev.action_log())
    for i in range(40, 70):
        ftl.write(i % 24, payload(i))
        truth[i % 24] = payload(i)
    assert all(a.block != victim for a in dev.action_log()[mark:])
    check_truth(ftl, truth, set())
    print("  退役后 30 次覆盖写均未触碰坏块，24 个逻辑页独立真值核对通过")


def scenario_data_loss():
    print("=== 场景二：指定页不可读，退役必须如实报告数据损失 ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    truth = {}
    for i in range(12):
        ftl.write(i, payload(i))
        truth[i] = payload(i)
    ppn = ftl.mapping_snapshot()[7]
    block, page = divmod(ppn, PAGES_PER_BLOCK)
    dev.inject_unreadable_page(block, page)
    print(f"  注入物理页 {ppn}（块 {block} 页 {page}）永久不可读，它持有逻辑页 7")
    try:
        dev.read_page(block, page)
    except UnreadablePageError:
        pass
    try:
        ftl.read(7)
    except UnreadablePageError:
        print("  退役前直接读取：UnreadablePageError，未返回任何伪造内容")

    dev.inject_block_erase_failure(block)
    report = ftl.retire_block(block)
    print(f"  退役报告：搬迁={[(l, o, n) for l, o, n in report['migrated']]}")
    print(f"            受损逻辑页={report['damaged_lpns']}（不凭空恢复）")
    assert report["damaged_lpns"] == (7,)
    assert block in dev.bad_blocks()

    # 断电重开后损失登记仍在，读取被显式阻止
    dev.reboot()
    ftl2 = FTL(dev)
    try:
        ftl2.read(7)
    except LogicalDataLossError as exc:
        print(f"  重开后读逻辑页 7 被阻止：{exc}")
    truth.pop(7)
    check_truth(ftl2, truth, {7})
    # 主机拿到新数据重写后恢复正常
    ftl2.write(7, b"host-recovered-data")
    got = ftl2.read(7)
    assert got == b"host-recovered-data".ljust(PAGE_SIZE, b"\xff")
    assert 7 not in dev.lost_logical_pages()
    print("  主机重写逻辑页 7 后恢复可读，损失登记解除")


def scenario_space_shortage():
    print("=== 场景三：搬迁空间不足时保留可恢复原状态 ===")
    dev = RawNand(max_erases=8)
    for b in range(4, 16):
        dev.mark_factory_bad(b)
    ftl = FTL(dev)
    i = 0
    while ftl.capacity()["free_physical_pages"] > 8:
        ftl.write(i % 24, payload(i))
        i += 1
    victim = next(p // PAGES_PER_BLOCK for p in ftl.mapping_snapshot() if p is not None)
    taken = 0
    for b in range(4):
        if b == victim:
            continue
        for p in range(PAGES_PER_BLOCK):
            if dev.is_blank(b, p):
                dev.program(b, p, b"\x03" * 4)
                taken += 1
            if taken >= 7:
                break
        if taken >= 7:
            break
    dev.inject_block_erase_failure(victim)
    seq_before = ftl.commit_seq
    mapping_before = ftl.mapping_snapshot()
    try:
        ftl.retire_block(victim, gc_budget=0)
    except Exception as exc:
        print(f"  gc_budget=0 退役被拒：{type(exc).__name__}")
    assert not dev.is_bad_block(victim), "空间不足不得把整块提前标成已迁完"
    assert ftl.commit_seq == seq_before
    assert ftl.mapping_snapshot() == mapping_before
    report = ftl.retire_block(victim, gc_budget=8)
    print(
        f"  放宽预算后退役成功：搬迁 {len(report['migrated'])} 页，块 {victim} 进入坏块集合"
    )
    assert dev.is_bad_block(victim)


if __name__ == "__main__":
    scenario_successful_retire()
    scenario_data_loss()
    scenario_space_shortage()
    print("全部演示完成。")
