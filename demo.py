"""固定输入演示：正常结果 + 实际触发的拒绝/异常边界。

运行：python demo.py（全程确定性计算，无 sleep、无预录结果，约 1 秒内完成）
"""
from collections import Counter

from ftl import (
    FTL,
    PAGE_SIZE,
    GCBudgetExhaustedError,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
    RawNand,
    TransactionError,
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
    print("=== 第三部分（本次迭代）：最多 4 页的原子事务 ===")
    items = [(i, f"NEW-{i}".encode()) for i in range(4)]

    # --- 正常路径：暂存不可见 → 单记录原子提交 → 重挂载持久 ---
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    for i in range(4):
        ftl.write(i, f"old-{i}".encode())
    n0 = len(dev.action_log())
    ftl.begin_transaction()
    for lpn, data in items:
        ftl.txn_put(lpn, data)
    hidden = all(ftl.read(i) == f"old-{i}".encode().ljust(PAGE_SIZE, b"\xff") for i in range(4))
    print(f"  暂存 4 页后、提交前：read 仍全部返回旧值（暂存不可见）= {hidden}")
    seq = ftl.commit_transaction()
    acts = [a.purpose for a in dev.action_log()[n0:]]
    visible = all(ftl.read(i) == items[i][1].ljust(PAGE_SIZE, b"\xff") for i in range(4))
    print(f"  提交后 4 页整笔可见 = {visible}；物理动作 = {acts}（4 数据页 + 恰好 1 条提交记录）")
    print(f"  序列号只前进 1 次：commit_seq={seq}")
    ftl2 = FTL(dev)
    persisted = all(ftl2.read(i) == items[i][1].ljust(PAGE_SIZE, b"\xff") for i in range(4))
    print(f"  重新挂载后整笔新映像持久保留 = {persisted}")

    # --- 原子性实证：逐物理动作注入掉电（真实枚举，非预录） ---
    print("  -- 掉电原子性枚举：覆盖 4 个已映射页的事务，共 5 个物理动作 --")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    for i in range(4):
        ftl.write(i, f"old-{i}".encode())
    snap_pre = dev.snapshot()
    base = len(dev.action_log())
    ftl.transaction_put(items)
    total = len(dev.action_log()) - base
    old_img = {i: f"old-{i}".encode().ljust(PAGE_SIZE, b"\xff") for i in range(4)}
    new_img = {i: items[i][1].ljust(PAGE_SIZE, b"\xff") for i in range(4)}
    verdict = {}
    for k in range(1, total + 1):  # 在第 k 个动作后掉电
        dev.restore(snap_pre)
        dev.set_crash_plan([k])
        dev.arm_crash()
        f = FTL(dev)
        f.begin_transaction()
        for lpn, data in items:
            f.txn_put(lpn, data)
        try:
            f.commit_transaction()
        except PowerLossError:
            pass
        dev.reboot()
        r = FTL(dev)
        got = {i: r.read(i) for i in range(4)}
        verdict[k] = "整笔新值" if got == new_img else ("整笔旧值" if got == old_img else "混合(错误!)")
    for k, v in verdict.items():
        print(f"    第 {k} 个物理动作后掉电 → 恢复映像：{v}")
    ok_atomic = set(verdict.values()) <= {"整笔旧值", "整笔新值"} and verdict[total] == "整笔新值"
    print(f"  5 个断电点只出现整旧/整新，且提交动作后为整笔新值：{ok_atomic}")

    # --- 边界与实际错误：容量必须先拒绝 ---
    print("  -- 容量边界：填满至 GC 保留线，预算为 0 时事务必须先拒绝 --")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev, gc_budget=0)
    i = 0
    while ftl.capacity()["free_physical_pages"] > 8:
        ftl.write(i % 24, bytes([i % 256]) * 8)
        i += 1
    cap = ftl.capacity()
    print(f"  填充后：空白页={cap['free_physical_pages']}（4 数据+1 提交需 13）"
          f"，可回收死页={cap['reclaimable_dead_pages']}")
    before = len(dev.action_log())
    shadow = {l: ftl.read(l) for l in range(24)}
    try:
        ftl.transaction_put(items)
        print("  错误：容量不足的事务应被拒绝")
    except GCBudgetExhaustedError as e:
        print(f"  捕获 GCBudgetExhaustedError: {e}")
    wrote_txn_page = any(a.purpose == "txn-data" for a in dev.action_log()[before:])
    unchanged = all(ftl.read(l) == shadow[l] for l in range(24))
    print(f"  拒绝后：无任何 txn-data 物理页={not wrote_txn_page}，映像逐页不变={unchanged}，"
          f"无残留事务={not ftl.in_transaction()}")
    seq_after = ftl.transaction_put(items, gc_budget=8)
    ok = all(ftl.read(i) == new_img[i] for i in range(4))
    print(f"  同一事务放宽预算至 8 后提交成功（seq={seq_after}），4 页新值可读 = {ok}")

    # --- 输入与状态机错误 ---
    print("  -- 非法输入与事务状态机错误（每个用例独立设备）--")
    cases = [
        ("第 5 个不同逻辑页", _fifth_page),
        ("未 begin 就 commit", lambda f: f.commit_transaction()),
        ("事务进行中调用 write", _write_inside),
    ]
    for label, make_call in cases:
        f = FTL(RawNand(max_erases=4))
        try:
            make_call(f)
            print(f"  错误：{label} 应被拒绝")
        except (InvalidInputError, TransactionError) as e:
            print(f"  捕获 {type(e).__name__} [{label}]: {e}")
    f = FTL(RawNand(max_erases=4))
    f.begin_transaction()
    f.txn_put(0, b"will-abort")
    n_phys = len(f.physical_log())
    f.abort_transaction()
    print(f"  abort 丢弃暂存且零物理写入 = {len(f.physical_log()) == n_phys}，"
          f"read(0) 未映射 = {f.read(0) is None}")


def _fifth_page(ftl):
    ftl.begin_transaction()
    for i in range(5):
        ftl.txn_put(i, b"x")


def _write_inside(ftl):
    ftl.begin_transaction()
    ftl.txn_put(0, b"x")
    ftl.write(1, b"y")


if __name__ == "__main__":
    part_normal()
    part_boundary()
    part_transaction()
    print("演示结束。")
