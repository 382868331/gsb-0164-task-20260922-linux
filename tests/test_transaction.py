"""原子事务：正常/边界/失败用例 + 逐写入点掉电枚举（全有或全无）。"""
import unittest

from ftl import (
    FTL,
    MAX_TXN_PAGES,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    GCBudgetExhaustedError,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
    RawNand,
    decode_commit_record,
)
from refimg import assert_image_matches, assert_matches_reference


def payload(i, size=12):
    return bytes(((i * 29 + j * 11) % 256 for j in range(size)))


class TestTxnBasic(unittest.TestCase):
    def test_sizes_1_to_4_commit_as_one_record(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = {}
        for size in (1, 2, 3, 4):
            batch = [(l, payload(10 * size + l)) for l in range(size)]
            seq0 = ftl.commit_seq
            ftl.transaction(batch)
            self.assertEqual(
                ftl.commit_seq, seq0 + 1, "一笔事务恰好推进一次提交序列号"
            )
            for l, d in batch:
                shadow[l] = d
            assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_untouched_pages_keep_old_values(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.write(5, b"keep-me")
        ftl.write(6, b"keep-too")
        ftl.transaction([(0, b"n0"), (1, b"n1")])
        self.assertEqual(ftl.read(5), b"keep-me".ljust(PAGE_SIZE, b"\xff"))
        self.assertEqual(ftl.read(6), b"keep-too".ljust(PAGE_SIZE, b"\xff"))

    def test_dict_input_accepted(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.transaction({7: b"dx", 8: b"dy"})
        self.assertEqual(ftl.read(7), b"dx".ljust(PAGE_SIZE, b"\xff"))
        self.assertEqual(ftl.read(8), b"dy".ljust(PAGE_SIZE, b"\xff"))

    def test_remount_sees_committed_transaction(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        batch = [(i, payload(50 + i)) for i in range(4)]
        ftl.transaction(batch)
        ftl2 = FTL(dev)  # 重新挂载 = 恢复
        for l, d in batch:
            self.assertEqual(ftl2.read(l), d.ljust(PAGE_SIZE, b"\xff"))
        assert_matches_reference(self, ftl2, dev)

    def test_data_pages_separate_from_commit_record(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        batch = [(i, payload(70 + i)) for i in range(3)]
        n0 = len(dev.action_log())
        ftl.transaction(batch)
        actions = dev.action_log()[n0:]
        self.assertEqual(
            [a.purpose for a in actions], ["txn-data"] * 3 + ["txn-commit"]
        )
        data_ppns = [a.block * PAGES_PER_BLOCK + a.page for a in actions[:3]]
        commit_ppn = actions[-1].block * PAGES_PER_BLOCK + actions[-1].page
        self.assertEqual(len(set(data_ppns)), 3, "数据页互不重叠")
        self.assertNotIn(commit_ppn, data_ppns, "提交记录与数据页分开存放")
        mapping = ftl.mapping_snapshot()
        for (l, _), ppn in zip(batch, data_ppns):
            self.assertEqual(mapping[l], ppn)
        # 提交记录页可独立解码，其映射指向数据页而非自身
        rec = decode_commit_record(
            dev.read_page(actions[-1].block, actions[-1].page)
        )
        self.assertIsNotNone(rec)
        _, rec_mapping = rec
        for ppn in data_ppns:
            self.assertIn(ppn, rec_mapping)
        self.assertNotIn(commit_ppn, rec_mapping)


class TestTxnValidation(unittest.TestCase):
    def setUp(self):
        self.dev = RawNand(max_erases=4)
        self.ftl = FTL(self.dev)
        self.ftl.write(0, b"keep")

    def test_invalid_writes_rejected_without_side_effects(self):
        cases = [
            ("空事务", []),
            ("超过 4 页", [(i, b"x") for i in range(MAX_TXN_PAGES + 1)]),
            ("重复 lpn", [(0, b"a"), (0, b"b")]),
            ("lpn 越界", [(24, b"a")]),
            ("lpn 为负", [(-1, b"a")]),
            ("lpn 为 bool", [(True, b"a")]),
            ("data 为空", [(1, b"")]),
            ("data 超长", [(1, b"x" * 33)]),
            ("data 非字节串", [(1, "str")]),
            ("writes 非可迭代", 42),
            ("事务项不是对", ["ab"]),
            ("事务项长度不为 2", [(1, b"a", 3)]),
        ]
        for label, writes in cases:
            with self.subTest(label=label):
                n_actions = len(self.dev.action_log())
                seq = self.ftl.commit_seq
                mapping = self.ftl.mapping_snapshot()
                with self.assertRaises(InvalidInputError, msg=label):
                    self.ftl.transaction(writes)
                self.assertEqual(
                    len(self.dev.action_log()), n_actions, "非法输入不得产生物理动作"
                )
                self.assertEqual(self.ftl.commit_seq, seq)
                self.assertEqual(self.ftl.mapping_snapshot(), mapping)
        self.assertEqual(self.ftl.read(0), b"keep".ljust(PAGE_SIZE, b"\xff"))

    def test_invalid_gc_budget_rejected(self):
        n_actions = len(self.dev.action_log())
        with self.assertRaises(InvalidInputError):
            self.ftl.transaction([(1, b"a")], gc_budget=True)
        with self.assertRaises(InvalidInputError):
            self.ftl.transaction([(1, b"a")], gc_budget=-1)
        self.assertEqual(len(self.dev.action_log()), n_actions)


class TestTxnCrashPointEnumeration(unittest.TestCase):
    """空白充足（无 GC）时，枚举一笔 4 页事务内部的每个断电点。"""

    def setUp(self):
        self.dev = RawNand(max_erases=4)
        ftl = FTL(self.dev)
        self.shadow = {}
        for i in range(4):  # 事务要覆盖的 4 页先写入旧值
            ftl.write(i, payload(100 + i))
            self.shadow[i] = payload(100 + i)
        ftl.write(10, payload(300))  # 不参与事务的页，验证不受影响
        self.shadow[10] = payload(300)
        self.snap = self.dev.snapshot()
        self.n0 = len(self.dev.action_log())
        self.new = [(i, payload(200 + i)) for i in range(4)]
        ftl.transaction(self.new)
        self.n1 = len(self.dev.action_log())
        actions = self.dev.action_log()[self.n0 : self.n1]
        self.assertEqual(
            [a.purpose for a in actions],
            ["txn-data"] * 4 + ["txn-commit"],
            "空白充足时事务应恰好为 4 个数据页 + 1 条提交记录",
        )

    def test_every_crash_point(self):
        for k in range(self.n0 + 1, self.n1 + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap)
                self.dev.set_crash_plan([k - self.n0])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                with self.assertRaises(PowerLossError):
                    ftl.transaction(self.new)
                self.dev.reboot()
                ftl2 = FTL(self.dev)
                expected = dict(self.shadow)
                if k == self.n1:  # 提交动作已完成 → 整笔新值，否则整笔旧值
                    for lpn, data in self.new:
                        expected[lpn] = data
                assert_image_matches(self, ftl2, expected)
                assert_matches_reference(self, ftl2, self.dev)
                # 恢复后系统收敛：可继续写并重挂载仍一致
                ftl2.write(20, b"after")
                self.assertEqual(
                    ftl2.read(20), b"after".ljust(PAGE_SIZE, b"\xff")
                )
                ftl3 = FTL(self.dev)
                self.assertEqual(
                    ftl3.read(20), b"after".ljust(PAGE_SIZE, b"\xff")
                )


class TestTxnPreflightGCCrashEnumeration(unittest.TestCase):
    """空白不足时，枚举一笔触发预检垃圾整理的 4 页事务的每个断电点。"""

    def setUp(self):
        self.dev = RawNand(max_erases=8)
        ftl = FTL(self.dev)
        self.shadow = {}
        i = 0
        while ftl.capacity()["free_physical_pages"] > 8:  # 8 = GC 保留页
            ftl.write(i % 24, payload(i))
            self.shadow[i % 24] = payload(i)
            i += 1
            self.assertLess(i, 300)
        self.snap = self.dev.snapshot()
        self.n0 = len(self.dev.action_log())
        self.new = [(j, payload(200 + j)) for j in range(4)]
        ftl.transaction(self.new)
        self.n1 = len(self.dev.action_log())
        actions = self.dev.action_log()[self.n0 : self.n1]
        self.assertTrue(
            any(a.kind == "erase" for a in actions), "预检应发生垃圾整理擦除"
        )
        self.assertEqual(actions[-1].purpose, "txn-commit")

    def test_every_crash_point(self):
        for k in range(self.n0 + 1, self.n1 + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap)
                self.dev.set_crash_plan([k - self.n0])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                with self.assertRaises(PowerLossError):
                    ftl.transaction(self.new)
                self.dev.reboot()
                ftl2 = FTL(self.dev)
                expected = dict(self.shadow)
                if k == self.n1:  # 只有事务提交完成后才可见整笔新值
                    for lpn, data in self.new:
                        expected[lpn] = data
                assert_image_matches(self, ftl2, expected)
                assert_matches_reference(self, ftl2, self.dev)
                # 恢复后收敛：可继续写、可手动整理
                # （16 块全满的场景收敛需 30+ 次擦除，显式放宽本次预算）
                ftl2.write(23, b"converge")
                self.assertEqual(
                    ftl2.read(23), b"converge".ljust(PAGE_SIZE, b"\xff")
                )
                ftl2.gc_collect(gc_budget=4 * 16)
                self.assertEqual(
                    ftl2.read(23), b"converge".ljust(PAGE_SIZE, b"\xff")
                )


class TestTxnCapacityAndBudget(unittest.TestCase):
    """容量不足必须先拒绝；预算耗尽与证明无解分开。"""

    def fill_to_gc_boundary(self, dev, ftl, shadow, boundary=8):
        i = 0
        while ftl.capacity()["free_physical_pages"] > boundary:
            ftl.write(i % 24, payload(i))
            shadow[i % 24] = payload(i)
            i += 1
            self.assertLess(i, 300)

    def test_out_of_space_rejected_before_any_physical_action(self):
        dev = RawNand(max_erases=0)  # 禁止擦除：死页永不可回收
        ftl = FTL(dev)
        shadow = {}
        self.fill_to_gc_boundary(dev, ftl, shadow, boundary=10)
        self.assertEqual(ftl.capacity()["free_physical_pages"], 10)
        n_actions = len(dev.action_log())
        seq = ftl.commit_seq
        mapping = ftl.mapping_snapshot()
        with self.assertRaises(OutOfSpaceError):
            ftl.transaction([(0, b"a"), (1, b"b")])  # 需 3+8=11 空白页 > 10
        self.assertEqual(
            len(dev.action_log()), n_actions, "先拒绝：不得产生任何物理动作"
        )
        self.assertEqual(ftl.commit_seq, seq)
        self.assertEqual(ftl.mapping_snapshot(), mapping)
        assert_image_matches(self, ftl, shadow)
        # 边界对照：1 页事务需 2+8=10 <= 10，同一状态下应成功
        ftl.transaction([(0, b"a")])
        shadow[0] = b"a"
        assert_image_matches(self, ftl, shadow)

    def test_budget_exhausted_then_success_with_budget(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev, gc_budget=0)  # 预算为 0：任何 GC 擦除都超预算
        shadow = {}
        self.fill_to_gc_boundary(dev, ftl, shadow)
        self.assertGreater(ftl.capacity()["reclaimable_dead_pages"], 0)
        n_actions = len(dev.action_log())
        batch = [(j, payload(200 + j)) for j in range(4)]
        with self.assertRaises(GCBudgetExhaustedError):
            ftl.transaction(batch)
        self.assertEqual(
            len(dev.action_log()), n_actions, "预算为 0：预检即失败，无任何物理动作"
        )
        assert_image_matches(self, ftl, shadow)
        # 放宽预算后同一事务成功：证明刚才是预算问题而非无解
        ftl.transaction(batch, gc_budget=8)
        for j in range(4):
            shadow[j] = payload(200 + j)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_failed_transaction_leaves_no_logical_partial_change(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev, gc_budget=0)
        shadow = {}
        self.fill_to_gc_boundary(dev, ftl, shadow)
        seq = ftl.commit_seq
        mapping = ftl.mapping_snapshot()
        with self.assertRaises(GCBudgetExhaustedError):
            ftl.transaction([(5, b"x"), (6, b"y")])
        self.assertEqual(ftl.commit_seq, seq, "失败不得推进提交序列号")
        self.assertEqual(ftl.mapping_snapshot(), mapping, "失败不得改变映射")
        assert_image_matches(self, ftl, shadow)


if __name__ == "__main__":
    unittest.main()
