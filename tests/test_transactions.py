"""原子事务（最多 4 页）：正常、边界、失败与逐物理动作掉电枚举。

期望值来源（不调用被测核心生成）：
- 测试侧手算/维护的影子字典 shadow：逐 lpn 给出“整笔旧映像”和“整笔新映像”；
- tests/refimg.py 的独立提交记录解析器（只使用设备只读接口）交叉验证；
- 物理动作数与动作目的（txn-data n 个 + 恰好 1 个 txn-commit）按手算枚举。
"""
import unittest

from ftl import (
    FTL,
    PAGE_SIZE,
    GCBudgetExhaustedError,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
    RawNand,
    TransactionError,
    TRANSACTION_MAX_PAGES,
)
from refimg import assert_matches_reference


def pad(data):
    return data.ljust(PAGE_SIZE, b"\xff")


# 固定事务负载：覆盖 lpn 0..3（其中旧映像 0..3 均已映射）
TXN_ITEMS = [(i, b"T%d" % i) for i in range(4)]


def image_of(ftl, lpns=range(24)):
    return {l: ftl.read(l) for l in lpns}


class TestTransactionNormal(unittest.TestCase):
    def setUp(self):
        self.dev = RawNand(max_erases=8)
        self.ftl = FTL(self.dev)
        # 手算旧映像：lpn0..3 = old0..old3，lpn4 未映射
        for i in range(4):
            self.ftl.write(i, b"old%d" % i)

    def test_staging_invisible_until_commit(self):
        self.ftl.begin_transaction()
        for lpn, data in TXN_ITEMS:
            self.ftl.txn_put(lpn, data)
        # 提交前 read 一律返回已提交旧映像
        for i in range(4):
            self.assertEqual(self.ftl.read(i), pad(b"old%d" % i))
        self.ftl.commit_transaction()
        for i in range(4):
            self.assertEqual(self.ftl.read(i), pad(b"T%d" % i))

    def test_commit_uses_single_record_and_seq_plus_one(self):
        seq_before = self.ftl.commit_seq
        n0 = len(self.dev.action_log())
        self.ftl.begin_transaction()
        for lpn, data in TXN_ITEMS:
            self.ftl.txn_put(lpn, data)
        seq = self.ftl.commit_transaction()
        acts = self.dev.action_log()[n0:]
        self.assertEqual(
            [a.purpose for a in acts],
            ["txn-data"] * 4 + ["txn-commit"],
        )
        self.assertEqual(seq, seq_before + 1)  # 4 页只推进 1 个序列号
        self.assertEqual(self.ftl.commit_seq, seq_before + 1)

    def test_persistence_on_clean_remount(self):
        self.ftl.begin_transaction()
        for lpn, data in TXN_ITEMS:
            self.ftl.txn_put(lpn, data)
        self.ftl.commit_transaction()
        ftl2 = FTL(self.dev)
        for i in range(4):
            self.assertEqual(ftl2.read(i), pad(b"T%d" % i))
        assert_matches_reference(self, ftl2, self.dev)

    def test_same_lpn_put_twice_counts_one_page(self):
        self.ftl.begin_transaction()
        self.ftl.txn_put(0, b"first-ver")
        self.assertEqual(self.ftl.transaction_staging(), ((0, b"first-ver"),))
        self.ftl.txn_put(0, b"second-ver")  # 覆盖本事务内暂存，页数仍为 1
        self.assertEqual(len(self.ftl.transaction_staging()), 1)
        seq = self.ftl.commit_transaction()
        self.assertEqual(seq, self.ftl.commit_seq)
        self.assertEqual(self.ftl.read(0), pad(b"second-ver"))
        # 其它 3 页保持旧值
        for i in range(1, 4):
            self.assertEqual(self.ftl.read(i), pad(b"old%d" % i))

    def test_convenience_transaction_put(self):
        seq = self.ftl.transaction_put(
            [(5, b"five"), (6, b"six"), (5, b"five2")]
        )
        self.assertEqual(seq, self.ftl.commit_seq)
        self.assertEqual(self.ftl.read(5), pad(b"five2"))
        self.assertEqual(self.ftl.read(6), pad(b"six"))
        self.assertFalse(self.ftl.in_transaction())

    def test_abort_discards_staging_without_writes(self):
        n0 = len(self.dev.action_log())
        self.ftl.begin_transaction()
        self.ftl.txn_put(0, b"dropped")
        self.ftl.abort_transaction()
        self.assertFalse(self.ftl.in_transaction())
        self.assertEqual(len(self.dev.action_log()), n0)  # 无任何物理动作
        self.assertEqual(self.ftl.read(0), pad(b"old0"))

    def test_read_allowed_inside_transaction(self):
        self.ftl.begin_transaction()
        self.ftl.txn_put(0, b"hidden")
        self.assertEqual(self.ftl.read(0), pad(b"old0"))  # 读已提交映像
        self.ftl.abort_transaction()

    def test_capacity_and_logical_log_report_txn(self):
        cap = self.ftl.capacity()
        self.assertFalse(cap["transaction_open"])
        self.assertEqual(cap["transaction_staged_pages"], 0)
        self.assertEqual(cap["transaction_max_pages"], 4)
        self.ftl.begin_transaction()
        self.ftl.txn_put(1, b"a")
        self.ftl.txn_put(2, b"b")
        cap = self.ftl.capacity()
        self.assertTrue(cap["transaction_open"])
        self.assertEqual(cap["transaction_staged_pages"], 2)
        self.ftl.abort_transaction()
        ops = [e["op"] for e in self.ftl.logical_log()]
        self.assertIn("txn-begin", ops)
        self.assertIn("txn-put", ops)
        self.assertIn("txn-abort", ops)
        self.ftl.transaction_put([(0, b"z")])
        self.assertEqual(self.ftl.logical_log()[-1]["op"], "txn-commit")

    def test_capacity_accounting_after_commit(self):
        # 手算：4 覆盖写 = 4 新数据页 + 1 提交记录，free 减 5
        before = self.ftl.capacity()
        self.ftl.transaction_put(TXN_ITEMS)
        after = self.ftl.capacity()
        self.assertEqual(
            after["free_physical_pages"], before["free_physical_pages"] - 5
        )
        self.assertEqual(after["mapped_pages"], 4)

    def test_plain_writes_still_work_around_transaction(self):
        self.ftl.write(10, b"plain-before")
        self.ftl.transaction_put([(10, b"txn-mid")])
        self.ftl.write(10, b"plain-after")
        self.assertEqual(self.ftl.read(10), pad(b"plain-after"))


class TestTransactionBoundaries(unittest.TestCase):
    def test_one_to_four_pages_all_commit(self):
        for n in range(1, TRANSACTION_MAX_PAGES + 1):
            dev = RawNand(max_erases=8)
            ftl = FTL(dev)
            items = [(i, b"v%d" % i) for i in range(n)]
            ftl.transaction_put(items)
            for i in range(n):
                self.assertEqual(ftl.read(i), pad(b"v%d" % i), f"n={n}")

    def test_fifth_distinct_lpn_rejected(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.begin_transaction()
        for i in range(4):
            ftl.txn_put(i, b"x")
        with self.assertRaises(InvalidInputError):
            ftl.txn_put(4, b"y")
        # 被拒后暂存仍是原来的 4 页，可正常提交
        ftl.commit_transaction()
        for i in range(4):
            self.assertEqual(ftl.read(i), pad(b"x"))

    def test_convenience_rejects_five_pages_without_side_effects(self):
        dev = RawNand(max_erases=4)
        ftl = FTL(dev)
        with self.assertRaises(InvalidInputError):
            ftl.transaction_put([(i, b"d") for i in range(5)])
        self.assertFalse(ftl.in_transaction())
        self.assertEqual(len(dev.action_log()), 0)  # 无物理写入
        self.assertEqual(ftl.commit_seq, 0)

    def test_empty_transaction_commit_rejected(self):
        dev = RawNand(max_erases=4)
        ftl = FTL(dev)
        ftl.begin_transaction()
        with self.assertRaises(InvalidInputError):
            ftl.commit_transaction()
        self.assertFalse(ftl.in_transaction())  # 空事务提交失败不残留事务
        n0 = len(dev.action_log())
        self.assertEqual(n0, 0)

    def test_exact_capacity_boundary_needs_13_free_for_four_page_txn(self):
        # 手算：4 数据页 + 1 提交记录；保留页 8，故需 free >= 8+5 = 13。
        # 单页写每次占 2 页，free=128-2w 恒为偶数，因此真实临界点为
        # free=14（可直接提交，无擦除）与 free=12（必须先 GC）。
        def drive_to_free(target):
            dev = RawNand(max_erases=8)
            ftl = FTL(dev)
            i = 0
            while ftl.capacity()["free_physical_pages"] > target:
                ftl.write(i % 24, bytes([i % 256]) * 8)
                i += 1
                self.assertLess(i, 200)
            self.assertEqual(ftl.capacity()["free_physical_pages"], target)
            return dev, ftl

        # 状态 A：free=14，恰好无需 GC
        dev, ftl = drive_to_free(14)
        self.assertGreater(ftl.capacity()["reclaimable_dead_pages"], 0)
        n0 = len(dev.action_log())
        ftl.transaction_put(TXN_ITEMS)
        self.assertFalse(
            any(a.kind == "erase" for a in dev.action_log()[n0:]),
            "free=14 >= 13 时不应触发 GC",
        )
        for j in range(4):
            self.assertEqual(ftl.read(j), pad(b"T%d" % j))

        # 状态 B：free=12，gc_budget=0：先拒绝且无任何事务页
        dev, ftl = drive_to_free(12)
        self.assertGreater(ftl.capacity()["reclaimable_dead_pages"], 0)
        snap = dev.snapshot()
        shadow = image_of(ftl)
        n0 = len(dev.action_log())
        with self.assertRaises(GCBudgetExhaustedError):
            ftl.transaction_put(TXN_ITEMS, gc_budget=0)
        self.assertFalse(ftl.in_transaction())
        self.assertEqual(image_of(ftl), shadow)
        self.assertFalse(
            any(a.purpose == "txn-data" for a in dev.action_log()[n0:])
        )
        # 同一状态放宽预算后成功：证明刚才是容量决策先行拒绝而非无解
        dev.restore(snap)
        ftl2 = FTL(dev)
        ftl2.transaction_put(TXN_ITEMS, gc_budget=8)
        for j in range(4):
            self.assertEqual(ftl2.read(j), pad(b"T%d" % j))

    def test_out_of_space_rejects_before_txn_pages(self):
        # max_erases=0：死页不可回收；填到仅剩保留页时事务必被拒
        dev = RawNand(max_erases=0)
        ftl = FTL(dev)
        i = 0
        while ftl.capacity()["free_physical_pages"] > 8:
            ftl.write(i % 24, bytes([i % 256]) * 4)
            i += 1
        before_seq = ftl.commit_seq
        n0 = len(dev.action_log())
        shadow = image_of(ftl)
        # 显式 begin/put/commit 路径：失败同样不得残留事务
        ftl.begin_transaction()
        ftl.txn_put(0, b"a")
        ftl.txn_put(1, b"b")
        with self.assertRaises(OutOfSpaceError):
            ftl.commit_transaction()
        self.assertFalse(ftl.in_transaction())
        self.assertEqual(ftl.commit_seq, before_seq)
        self.assertEqual(image_of(ftl), shadow)  # 无半更新
        self.assertFalse(
            any(a.purpose == "txn-data" for a in dev.action_log()[n0:])
        )

    def test_budget_exhausted_rejects_before_txn_pages(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev, gc_budget=0)
        i = 0
        while ftl.capacity()["free_physical_pages"] > 8:
            ftl.write(i % 24, bytes([i % 256]) * 8)
            i += 1
        self.assertGreater(ftl.capacity()["reclaimable_dead_pages"], 0)
        shadow = image_of(ftl)
        seq = ftl.commit_seq
        n0 = len(dev.action_log())
        # 显式接口路径
        ftl.begin_transaction()
        for lpn, data in TXN_ITEMS:
            ftl.txn_put(lpn, data)
        with self.assertRaises(GCBudgetExhaustedError):
            ftl.commit_transaction()
        self.assertFalse(ftl.in_transaction(), "提交失败不得残留进行中事务")
        self.assertEqual(ftl.commit_seq, seq)
        self.assertEqual(image_of(ftl), shadow)
        self.assertFalse(
            any(a.purpose == "txn-data" for a in dev.action_log()[n0:])
        )
        # 暂存已清空：重新 begin 后是一笔全新事务
        ftl.begin_transaction()
        self.assertEqual(ftl.transaction_staging(), ())
        ftl.abort_transaction()

    def test_txn_with_gc_commits_and_preserves_all_data(self):
        # 6 个好块（48 页）：4 页事务在边界触发含搬迁的 GC 后成功
        dev = RawNand(max_erases=8)
        for b in range(10):
            dev.mark_factory_bad(b)
        ftl = FTL(dev)
        for i in range(4):
            ftl.write(i, b"old%d" % i)
        i = 0
        while ftl.capacity()["free_physical_pages"] > 12:
            ftl.write(20 + (i % 4), b"fill%d" % i)
            i += 1
            self.assertLess(i, 200)
        shadow = {l: ftl.read(l) for l in range(24)}
        ftl.transaction_put(TXN_ITEMS)
        for j in range(4):
            shadow[j] = pad(b"T%d" % j)
        self.assertEqual(image_of(ftl), shadow)
        assert_matches_reference(self, ftl, dev)

    def test_two_reclaims_budget_boundary_decided_before_data_pages(self):
        # 固定轨迹（6 好块）构造出“一笔 4 页事务需连续回收 2 个块”的状态。
        # 解析核对（依据动作日志，而非被测实现告知）：
        #   gc_budget=1 → 容量决策阶段在第 2 次回收处预算耗尽而拒绝，
        #                  物理日志中该事务不得出现任何 txn-data；
        #   gc_budget=2 → 恰好 2 次 gc-erase 后提交成功。
        seq = [
            12, 1, 7, 1, 17, 4, 9, 13, 4, 17, 3, 18, 9, 17, 21, 5, 3, 18,
            18, 20, 6, 11, 3, 17, 22, 2, 18, 1, 19, 6, 15, 21, 17, 13, 10,
            14, 18, 14,
        ]

        def build():
            dev = RawNand(max_erases=8)
            for b in range(10):
                dev.mark_factory_bad(b)
            ftl = FTL(dev)
            for i, l in enumerate(seq):
                ftl.write(l, b"v%-4d" % i)
            return dev, ftl

        dev1, ftl1 = build()
        self.assertLess(ftl1.capacity()["free_physical_pages"], 13)
        shadow1 = image_of(ftl1)
        n0 = len(dev1.action_log())
        with self.assertRaises(GCBudgetExhaustedError):
            ftl1.transaction_put(TXN_ITEMS, gc_budget=1)
        added = dev1.action_log()[n0:]
        self.assertFalse(
            any(a.purpose == "txn-data" for a in added),
            "容量决策必须先于任何事务数据页写入",
        )
        # 注意：第 1 次回收若搬迁了有效页会产生 gc-commit（序列号前进、
        # 逻辑值不变）；允许序列号前进，但映像必须逐字节不变。
        self.assertEqual(image_of(ftl1), shadow1)
        for j in range(4):  # 事务新值绝不能泄露
            self.assertNotEqual(ftl1.read(j), pad(b"T%d" % j))
        self.assertFalse(ftl1.in_transaction())

        dev2, ftl2 = build()
        shadow2 = image_of(ftl2)
        n0 = len(dev2.action_log())
        ftl2.transaction_put(TXN_ITEMS, gc_budget=2)
        added = dev2.action_log()[n0:]
        self.assertEqual(
            sum(1 for a in added if a.kind == "erase"), 2,
            "该状态应恰好需要回收 2 个块",
        )
        self.assertEqual(
            [a.purpose for a in added].count("txn-data"), 4
        )
        for j in range(4):
            shadow2[j] = pad(b"T%d" % j)
        self.assertEqual(image_of(ftl2), shadow2)
        assert_matches_reference(self, ftl2, dev2)


class TestTransactionStateMachineAndValidation(unittest.TestCase):
    def setUp(self):
        self.ftl = FTL(RawNand(max_erases=4))

    def test_ops_without_begin_are_state_errors(self):
        with self.assertRaises(TransactionError):
            self.ftl.txn_put(0, b"z")
        with self.assertRaises(TransactionError):
            self.ftl.commit_transaction()
        with self.assertRaises(TransactionError):
            self.ftl.abort_transaction()

    def test_nested_begin_rejected(self):
        self.ftl.begin_transaction()
        with self.assertRaises(TransactionError):
            self.ftl.begin_transaction()
        self.ftl.abort_transaction()

    def test_plain_write_and_gc_blocked_inside(self):
        self.ftl.begin_transaction()
        self.ftl.txn_put(0, b"z")
        with self.assertRaises(TransactionError):
            self.ftl.write(1, b"w")
        with self.assertRaises(TransactionError):
            self.ftl.gc_collect()
        self.ftl.abort_transaction()

    def test_invalid_put_inputs_do_not_open_transaction(self):
        # 无事务时：非法输入给 InvalidInputError，且不得隐式开启事务
        with self.assertRaises(InvalidInputError):
            self.ftl.txn_put(True, b"z")
        with self.assertRaises(InvalidInputError):
            self.ftl.txn_put(24, b"z")
        with self.assertRaises(InvalidInputError):
            self.ftl.txn_put(0, b"")
        with self.assertRaises(InvalidInputError):
            self.ftl.txn_put(0, b"z" * 33)
        with self.assertRaises(InvalidInputError):
            self.ftl.txn_put(0, "str")
        self.assertFalse(self.ftl.in_transaction())

    def test_invalid_put_inside_keeps_prior_staging(self):
        self.ftl.begin_transaction()
        self.ftl.txn_put(0, b"kept")
        with self.assertRaises(InvalidInputError):
            self.ftl.txn_put(99, b"bad")
        self.assertEqual(self.ftl.transaction_staging(), ((0, b"kept"),))
        self.ftl.abort_transaction()

    def test_convenience_input_validation(self):
        for bad_items in (
            123,
            None,
            [(0,)],
            [(0, b"a", 3)],
            [("0", b"a")],
            [(True, b"a")],
            [(0, b"")],
            [(0, "s")],
        ):
            with self.assertRaises(InvalidInputError, msg=f"应拒绝 {bad_items!r}"):
                self.ftl.transaction_put(bad_items)
            self.assertFalse(self.ftl.in_transaction(), f"{bad_items!r} 不得残留事务")
        self.assertEqual(len(self.ftl.physical_log()), 0)

    def test_convenience_nested_inside_explicit_txn(self):
        self.ftl.begin_transaction()
        with self.assertRaises(TransactionError):
            self.ftl.transaction_put([(0, b"z")])
        self.ftl.abort_transaction()


class TestCrashAfterTransactionCommitFresh(unittest.TestCase):
    """全新 lpn 的 4 页事务：5 个物理动作，逐点掉电，严格整旧/整新二分。"""

    def setUp(self):
        self.dev = RawNand(max_erases=8)
        self.snap0 = self.dev.snapshot()
        ftl = FTL(self.dev)
        self.items = [(i, b"new%d" % i) for i in range(4)]
        ftl.transaction_put(self.items)
        self.total = len(self.dev.action_log())  # 5
        self.assertEqual(self.total, 5)

    def test_every_crash_point(self):
        for k in range(1, self.total + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap0)
                self.dev.set_crash_plan([k])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                ftl.begin_transaction()
                for lpn, data in self.items:
                    ftl.txn_put(lpn, data)
                with self.assertRaises(PowerLossError):
                    ftl.commit_transaction()
                self.dev.reboot()
                rec = FTL(self.dev)
                # 手算：最后一个动作（txn-commit）完成才可见整笔新值
                if k == self.total:
                    for lpn, data in self.items:
                        self.assertEqual(rec.read(lpn), pad(data))
                else:
                    for lpn, _ in self.items:
                        self.assertIsNone(rec.read(lpn), f"k={k} 不得半新半旧")
                assert_matches_reference(self, rec, self.dev)
                # 恢复后重放同一事务成功
                rec.transaction_put(self.items)
                for lpn, data in self.items:
                    self.assertEqual(rec.read(lpn), pad(data))
                assert_matches_reference(self, rec, self.dev)

    def test_no_open_transaction_after_reboot(self):
        self.dev.restore(self.snap0)
        self.dev.set_crash_plan([2])
        self.dev.arm_crash()
        ftl = FTL(self.dev)
        ftl.begin_transaction()
        for lpn, data in self.items:
            ftl.txn_put(lpn, data)
        with self.assertRaises(PowerLossError):
            ftl.commit_transaction()
        self.dev.reboot()
        rec = FTL(self.dev)
        self.assertFalse(rec.in_transaction())  # 暂存在内存，断电即消失
        # 设备上恢复后仍可正常开启新事务
        rec.begin_transaction()
        rec.txn_put(7, b"after")
        rec.commit_transaction()
        self.assertEqual(rec.read(7), pad(b"after"))


class TestCrashOverwriteTransaction(unittest.TestCase):
    """覆盖已映射 4 页的事务：掉电只能得到整笔旧值或整笔新值（手算影子）。"""

    def setUp(self):
        self.dev = RawNand(max_erases=8)
        ftl = FTL(self.dev)
        for i in range(4):
            ftl.write(i, b"old%d" % i)
        self.snap_pre = self.dev.snapshot()
        self.n0 = len(self.dev.action_log())
        self.items = [(i, b"T%d" % i) for i in range(4)]
        ftl.transaction_put(self.items)
        self.n1 = len(self.dev.action_log())
        self.assertEqual(self.n1 - self.n0, 5)

    def test_every_crash_point(self):
        old_img = {i: pad(b"old%d" % i) for i in range(4)}
        new_img = {i: pad(b"T%d" % i) for i in range(4)}
        for k in range(self.n0 + 1, self.n1 + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap_pre)
                self.dev.set_crash_plan([k - self.n0])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                ftl.begin_transaction()
                for lpn, data in self.items:
                    ftl.txn_put(lpn, data)
                with self.assertRaises(PowerLossError):
                    ftl.commit_transaction()
                self.dev.reboot()
                rec = FTL(self.dev)
                got = image_of(rec, range(4))
                # 严格判定：提交动作（序列最后一个）之后才是整笔新值
                expected = new_img if k == self.n1 else old_img
                self.assertEqual(got, expected, f"k={k} 出现非原子映像")
                assert_matches_reference(self, rec, self.dev)


class TestCrashTransactionWithGC(unittest.TestCase):
    """事务提交触发含搬迁 GC：枚举 [gc-copy, gc-commit, gc-erase,
    txn-data*4, txn-commit] 全部 8 个动作边界。

    GC 阶段（前 3 个动作）崩溃：逻辑映像仍是整笔旧值（GC 只搬迁不改变值）；
    txn-data 阶段崩溃：无提交记录引用，整笔旧值；
    txn-commit 后崩溃：整笔新值。
    """

    def setUp(self):
        dev = RawNand(max_erases=8)
        for b in range(10):  # 仅 6 个好块，逼迫搬迁式 GC
            dev.mark_factory_bad(b)
        ftl = FTL(dev)
        for i in range(4):
            ftl.write(i, b"old%d" % i)
        i = 0
        while True:
            snap = dev.snapshot()
            n0 = len(dev.action_log())
            ftl.transaction_put(TXN_ITEMS)
            n1 = len(dev.action_log())
            acts = ftl.physical_log()[n0:n1]
            if any(a.purpose == "gc-copy" for a in acts):
                self.dev = dev
                self.snap_pre = snap
                self.n0 = n0
                self.n1 = n1
                self.acts = [(a.kind, a.purpose) for a in acts]
                break
            dev.restore(snap)
            ftl = FTL(dev)
            ftl.write(20 + (i % 4), b"fill%d" % i)
            i += 1
            self.assertLess(i, 200, "应在 200 次写内构造出搬迁式 GC 场景")
        # sanity：手算的动作结构
        self.assertEqual(self.acts[-1], ("program", "txn-commit"))
        self.assertIn(("program", "txn-data"), self.acts)
        self.assertIn(("erase", "gc-erase"), self.acts)

    def test_every_crash_point(self):
        old_img = {i: pad(b"old%d" % i) for i in range(4)}
        new_img = {i: pad(b"T%d" % i) for i in range(4)}
        total = self.n1 - self.n0
        for k in range(self.n0 + 1, self.n1 + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap_pre)
                self.dev.set_crash_plan([k - self.n0])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                ftl.begin_transaction()
                for lpn, data in TXN_ITEMS:
                    ftl.txn_put(lpn, data)
                with self.assertRaises(PowerLossError):
                    ftl.commit_transaction()
                self.dev.reboot()
                rec = FTL(self.dev)
                got = image_of(rec, range(4))
                expected = new_img if k - self.n0 == total else old_img
                self.assertEqual(got, expected, f"k={k} 事务原子性被破坏")
                assert_matches_reference(self, rec, self.dev)
                # 恢复后重放事务并成功
                rec.transaction_put(TXN_ITEMS)
                self.assertEqual(image_of(rec, range(4)), new_img)
                assert_matches_reference(self, rec, self.dev)


class TestAllFFPagesNotRecycled(unittest.TestCase):
    """全 0xFF 数据页与物理空白页不可区分的陷阱回归。

    若分配以物理 blank 位为准，已映射的全 0xFF 数据页会被误当空白页
    再次分配，产生重复 ppn 的非法提交记录，恢复时回退到更旧映像——
    直接破坏“恢复只能得整笔旧值或整笔新值”。元数据（映射/pin）必须
    优先于物理 blank 判定。
    """

    def test_plain_write_all_ff_survives_remount_and_realloc(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.write(3, b"\xff")          # 1 字节 0xFF，补齐后整页全 0xFF
        ftl.write(3, b"\xff" * 32)     # 整页全 0xFF 的覆盖写
        self.assertEqual(ftl.read(3), b"\xff" * PAGE_SIZE)
        ftl2 = FTL(dev)
        self.assertEqual(ftl2.read(3), b"\xff" * PAGE_SIZE)
        ppn3 = ftl2.mapping_snapshot()[3]
        ftl2.write(4, b"\x00\x01")     # 不得复用全 0xFF 的有效页 ppn3
        self.assertEqual(ftl2.read(3), b"\xff" * PAGE_SIZE)
        self.assertNotEqual(ftl2.mapping_snapshot()[3],
                            ftl2.mapping_snapshot()[4])
        self.assertEqual(ftl2.mapping_snapshot()[3], ppn3)
        assert_matches_reference(self, ftl2, dev)

    def test_all_ff_transaction_crash_enumeration(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        for i in range(4):
            ftl.write(i, bytes([i]) * 4)
        snap_pre = dev.snapshot()
        n0 = len(dev.action_log())
        items = [(i, b"\xff" * ((i % 32) + 1)) for i in range(4)]
        ftl.transaction_put(items)
        n1 = len(dev.action_log())
        self.assertEqual(n1 - n0, 5)
        old_img = {i: pad(bytes([i]) * 4) for i in range(4)}
        new_img = {i: b"\xff" * PAGE_SIZE for i in range(4)}
        for k in range(n0 + 1, n1 + 1):
            with self.subTest(crash_after_action=k):
                dev.restore(snap_pre)
                dev.set_crash_plan([k - n0])
                dev.arm_crash()
                r = FTL(dev)
                r.begin_transaction()
                for lpn, data in items:
                    r.txn_put(lpn, data)
                with self.assertRaises(PowerLossError):
                    r.commit_transaction()
                dev.reboot()
                rec = FTL(dev)
                got = image_of(rec, range(4))
                expected = new_img if k == n1 else old_img
                self.assertEqual(got, expected)
                assert_matches_reference(self, rec, dev)

    def test_gc_with_all_ff_valid_pages(self):
        # 含全 0xFF 有效页的工作负载在大量覆盖写触发 GC 后仍逐页一致
        dev = RawNand(max_erases=8)
        for b in range(10):
            dev.mark_factory_bad(b)
        ftl = FTL(dev)
        shadow = {}
        for i in range(20):
            data = b"\xff" * 32 if i % 2 == 0 else bytes([i]) * 32
            ftl.write(i, data)
            shadow[i] = data
        for i in range(40):
            lpn = i % 24
            data = bytes([i % 256]) * 32
            ftl.write(lpn, data)
            shadow[lpn] = data
        for lpn in range(24):
            want = None if lpn not in shadow else pad(shadow[lpn])
            self.assertEqual(ftl.read(lpn), want)
        assert_matches_reference(self, ftl, dev)


if __name__ == "__main__":
    unittest.main()
