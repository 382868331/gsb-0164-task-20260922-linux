"""坏块、磨损极限、空间不足与预算耗尽的区分。"""
import unittest

from ftl import (
    FTL,
    PAGE_SIZE,
    BadBlockError,
    BlockWornError,
    GCBudgetExhaustedError,
    InvalidInputError,
    OutOfSpaceError,
    RawNand,
)
from refimg import assert_image_matches


def payload(i, size=12):
    return bytes(((i * 29 + j * 11) % 256 for j in range(size)))


class TestBadBlocks(unittest.TestCase):
    def test_factory_bad_blocks_never_allocated(self):
        dev = RawNand(max_erases=8)
        dev.mark_factory_bad(3)
        dev.mark_factory_bad(11)
        ftl = FTL(dev)
        shadow = {}
        for i in range(70):  # 足以触发 GC
            lpn = i % 24
            ftl.write(lpn, payload(i))
            shadow[lpn] = payload(i)
        for a in dev.action_log():
            self.assertNotIn(a.block, (3, 11), "坏块不得再分配")
        self.assertEqual(ftl.capacity()["bad_blocks"], (3, 11))
        assert_image_matches(self, ftl, shadow)

    def test_bad_block_access_rejected(self):
        dev = RawNand(max_erases=4)
        dev.mark_factory_bad(5)
        with self.assertRaises(BadBlockError):
            dev.program(5, 0, b"x")
        with self.assertRaises(BadBlockError):
            dev.erase(5)
        with self.assertRaises(BadBlockError):
            dev.read_page(5, 0)
        with self.assertRaises(BadBlockError):
            dev.is_blank(5, 0)

    def test_factory_bad_requires_unused_block(self):
        dev = RawNand(max_erases=4)
        dev.program(2, 0, b"x")
        with self.assertRaises(InvalidInputError):
            dev.mark_factory_bad(2)
        with self.assertRaises(InvalidInputError):
            dev.mark_factory_bad(2)  # 幂等性不适用于已用块
        dev.mark_factory_bad(6)
        with self.assertRaises(InvalidInputError):
            dev.mark_factory_bad(6)  # 重复登记

    def test_retired_block_never_allocated(self):
        dev = RawNand(max_erases=8)
        dev.retire_block(0)
        ftl = FTL(dev)
        for i in range(20):
            ftl.write(i % 24, payload(i))
        self.assertTrue(all(a.block != 0 for a in dev.action_log()))
        self.assertIn(0, ftl.capacity()["bad_blocks"])


class TestWearLimit(unittest.TestCase):
    def test_worn_blocks_stop_being_reclaimed_then_out_of_space(self):
        dev = RawNand(max_erases=1)  # 每块仅允许擦除一次
        ftl = FTL(dev)
        shadow = {}
        count = 0
        with self.assertRaises(OutOfSpaceError):
            for i in range(400):
                lpn = i % 24
                ftl.write(lpn, payload(i))
                shadow[lpn] = payload(i)
                count += 1
        self.assertGreater(count, 0)
        cap = ftl.capacity()
        self.assertTrue(all(c <= 1 for c in cap["erase_counts"]), "不得超磨损上限")
        self.assertGreater(len(cap["worn_blocks"]), 0)
        # 已提交数据在失败后仍完整可读（无未说明的部分变更）
        assert_image_matches(self, ftl, shadow)

    def test_device_erase_on_worn_block_rejected(self):
        dev = RawNand(max_erases=1)
        dev.erase(0)
        with self.assertRaises(BlockWornError):
            dev.erase(0)
        self.assertEqual(dev.erase_count(0), 1)

    def test_zero_max_erases_means_no_erase_at_all(self):
        dev = RawNand(max_erases=0)
        with self.assertRaises(BlockWornError):
            dev.erase(0)


class TestSpaceAndBudgetSeparation(unittest.TestCase):
    """预算耗尽（GCBudgetExhaustedError）与证明无解（OutOfSpaceError）必须分开。"""

    def fill_to_gc_boundary(self, dev, ftl, shadow):
        """写用户数据直到触及 GC 保留页边界：下一次写必须先做垃圾整理。"""
        i = 0
        while ftl.capacity()["free_physical_pages"] > 8:  # 8 = GC_RESERVE_PAGES
            lpn = i % 24
            ftl.write(lpn, payload(i))
            shadow[lpn] = payload(i)
            i += 1
            self.assertLess(i, 300)

    def test_budget_exhausted_is_not_out_of_space(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev, gc_budget=0)  # 预算为 0：任何 GC 擦除都超预算
        shadow = {}
        self.fill_to_gc_boundary(dev, ftl, shadow)
        cap = ftl.capacity()
        self.assertGreater(cap["reclaimable_dead_pages"], 0, "应存在可回收页")
        before_log = len(dev.action_log())
        with self.assertRaises(GCBudgetExhaustedError):
            ftl.write(0, b"need-gc")
        # 失败不改变已提交逻辑映像
        assert_image_matches(self, ftl, shadow)
        # 放宽预算后同一写成功：证明刚才是预算问题而非无解
        ftl.write(0, b"need-gc", gc_budget=8)
        shadow[0] = b"need-gc"
        assert_image_matches(self, ftl, shadow)
        self.assertGreater(len(dev.action_log()), before_log)

    def test_out_of_space_is_provable(self):
        dev = RawNand(max_erases=0)  # 禁止任何擦除：死页永不可回收
        ftl = FTL(dev)
        shadow = {}
        self.fill_to_gc_boundary(dev, ftl, shadow)
        self.assertEqual(ftl.capacity()["free_physical_pages"], 8)  # 仅剩保留页
        with self.assertRaises(OutOfSpaceError):
            ftl.write(0, b"no-space")
        # 失败不改变已提交逻辑映像
        assert_image_matches(self, ftl, shadow)

    def test_failed_write_leaves_no_logical_partial_change(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev, gc_budget=0)
        shadow = {}
        self.fill_to_gc_boundary(dev, ftl, shadow)
        seq = ftl.commit_seq
        mapping = ftl.mapping_snapshot()
        with self.assertRaises(GCBudgetExhaustedError):
            ftl.write(5, b"atomicity-check")
        self.assertEqual(ftl.commit_seq, seq, "失败不得推进提交序列号")
        self.assertEqual(ftl.mapping_snapshot(), mapping, "失败不得改变映射")
        assert_image_matches(self, ftl, shadow)


if __name__ == "__main__":
    unittest.main()
