"""逻辑页释放（trim）：整组原子提交、幂等、断电恢复、GC 与容量账目。

契约要点（与被测核心无关的测试侧影子字典 + 独立参考解析器双重核对）：
- trim(lpn_list) 整组通过一条新映像提交记录生效：断电重挂载只能看到
  全部释放前或全部释放后的映射，不能丢一半；
- 已未映射页与重复页号幂等；空列表/全未映射不产生物理写入、不消耗提交序号；
- 混入任一非法页号 → 任何物理动作之前整次拒绝；
- 释放只撤销逻辑映射，不立即擦除物理块；死页纳入既有 GC 与容量统计。
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
)
from refimg import assert_image_matches, assert_matches_reference


def payload(i, size=12):
    return bytes(((i * 17 + j * 5 + 3) % 256 for j in range(size)))


def fill_to_gc_boundary(case, ftl, shadow):
    """写用户数据直到触及 GC 保留页边界：下一次操作必须先做垃圾整理。"""
    i = 0
    while ftl.capacity()["free_physical_pages"] > 8:  # 8 = GC_RESERVE_PAGES
        lpn = i % 24
        ftl.write(lpn, payload(i))
        shadow[lpn] = payload(i)
        i += 1
        case.assertLess(i, 300)


class TestTrimBasic(unittest.TestCase):
    def test_trim_releases_group_with_single_commit(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = {}
        for i in range(6):
            ftl.write(i, payload(i))
            shadow[i] = payload(i)
        seq0 = ftl.commit_seq
        n0 = len(dev.action_log())
        self.assertEqual(ftl.trim([1, 3, 5]), 3)
        actions = dev.action_log()[n0:]
        self.assertEqual(len(actions), 1, "无需 GC 时整组释放应只有一条提交记录")
        self.assertEqual(actions[0].kind, "program")
        self.assertEqual(actions[0].purpose, "trim-commit")
        self.assertEqual(ftl.commit_seq, seq0 + 1, "整组释放只消耗一个提交序号")
        for lpn in (1, 3, 5):
            shadow.pop(lpn)
            self.assertIsNone(ftl.read(lpn))
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_trim_does_not_erase_and_dead_pages_counted(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        for i in range(5):
            ftl.write(i, payload(i))
        cap0 = ftl.capacity()
        ftl.trim([0, 1])
        self.assertFalse(
            any(a.kind == "erase" for a in dev.action_log()),
            "释放不得立即擦除任何物理块",
        )
        cap = ftl.capacity()
        self.assertEqual(cap["mapped_pages"], 3)
        # 死页增加：2 个被释放的数据页 + 1 条过期提交记录
        self.assertEqual(cap["reclaimable_dead_pages"], cap0["reclaimable_dead_pages"] + 3)
        self.assertEqual(cap["free_physical_pages"], cap0["free_physical_pages"] - 1)

    def test_rewrite_after_trim_uses_normal_allocation(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.write(3, b"old-data")
        ftl.trim([3])
        self.assertIsNone(ftl.read(3))
        ftl.write(3, b"new-data")
        self.assertEqual(ftl.read(3), b"new-data".ljust(PAGE_SIZE, b"\xff"))
        ftl2 = FTL(dev)  # 重挂载后仍为新数据
        self.assertEqual(ftl2.read(3), b"new-data".ljust(PAGE_SIZE, b"\xff"))
        assert_matches_reference(self, ftl2, dev)


class TestTrimIdempotent(unittest.TestCase):
    def setUp(self):
        self.dev = RawNand(max_erases=8)
        self.ftl = FTL(self.dev)
        for i in range(4):
            self.ftl.write(i, payload(i))

    def assert_no_physical_action(self, n0, seq0):
        self.assertEqual(len(self.dev.action_log()), n0, "不应产生任何物理动作")
        self.assertEqual(self.ftl.commit_seq, seq0, "不应消耗提交序号")

    def test_empty_list_is_noop(self):
        n0, seq0 = len(self.dev.action_log()), self.ftl.commit_seq
        self.assertEqual(self.ftl.trim([]), 0)
        self.assert_no_physical_action(n0, seq0)

    def test_unmapped_pages_are_noop(self):
        n0, seq0 = len(self.dev.action_log()), self.ftl.commit_seq
        self.assertEqual(self.ftl.trim([7, 8, 9]), 0)  # 全部未映射
        self.assert_no_physical_action(n0, seq0)

    def test_duplicate_lpns_deduped(self):
        n0 = len(self.dev.action_log())
        self.assertEqual(self.ftl.trim([2, 2, 2]), 1)
        self.assertEqual(len(self.dev.action_log()), n0 + 1, "重复页号只释放一次")
        self.assertIsNone(self.ftl.read(2))

    def test_repeated_trim_is_noop(self):
        self.assertEqual(self.ftl.trim([0, 1]), 2)
        n0, seq0 = len(self.dev.action_log()), self.ftl.commit_seq
        self.assertEqual(self.ftl.trim([0, 1]), 0)  # 重复释放：幂等
        self.assert_no_physical_action(n0, seq0)

    def test_mixed_mapped_and_unmapped(self):
        self.assertEqual(self.ftl.trim([1, 9]), 1)  # 9 未映射：幂等跳过
        self.assertIsNone(self.ftl.read(1))
        self.assertIsNone(self.ftl.read(9))


class TestTrimValidation(unittest.TestCase):
    def test_invalid_lpn_rejects_whole_request_before_any_action(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.write(0, b"keep-me")
        n0, seq0 = len(dev.action_log()), ftl.commit_seq
        mapping0 = ftl.mapping_snapshot()
        bad_lists = [
            [0, 24],
            [0, -1],
            [0, True],
            [0, 1.5],
            [0, "x"],
            [0, None],
            [0, float("nan")],
            [0, float("inf")],
        ]
        for bad in bad_lists:
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    ftl.trim(bad)
                self.assertEqual(len(dev.action_log()), n0, "拒绝前不得有物理动作")
                self.assertEqual(ftl.commit_seq, seq0)
                self.assertEqual(ftl.mapping_snapshot(), mapping0)
                self.assertEqual(
                    ftl.read(0),
                    b"keep-me".ljust(PAGE_SIZE, b"\xff"),
                    "同组合法页号也不得被释放",
                )

    def test_non_sequence_containers_rejected(self):
        ftl = FTL(RawNand(max_erases=4))
        for bad in (5, None, 1.5, "abc", b"\x01\x02"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    ftl.trim(bad)

    def test_iterables_accepted(self):
        ftl = FTL(RawNand(max_erases=4))
        ftl.write(0, b"a")
        ftl.write(1, b"b")
        self.assertEqual(ftl.trim(iter([0, 1])), 2)  # 迭代器可用
        self.assertEqual(ftl.trim((x for x in [0, 1])), 0)  # 已释放：幂等

    def test_invalid_gc_budget_rejected(self):
        ftl = FTL(RawNand(max_erases=4))
        ftl.write(0, b"a")
        for bad in (-1, True, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    ftl.trim([0], gc_budget=bad)
        self.assertEqual(ftl.read(0), b"a".ljust(PAGE_SIZE, b"\xff"))


class TestTrimMixedLiveAndReleasedPages(unittest.TestCase):
    """同一块内混着活页与待释放页：释放后活页必须保住，块被回收后亦然。"""

    def test_live_pages_survive_trim_and_block_reclaim(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = {}
        for lpn in range(24):  # 24 页全映射：鸽巢原理必有块含 ≥2 个有效页
            ftl.write(lpn, payload(lpn))
            shadow[lpn] = payload(lpn)
        blocks = {}
        for lpn, ppn in enumerate(ftl.mapping_snapshot()):
            blocks.setdefault(ppn // 8, []).append(lpn)
        block, lpns_in_block = next((b, ls) for b, ls in blocks.items() if len(ls) >= 2)
        survivor = lpns_in_block[0]
        victims = lpns_in_block[1:]
        erases0 = dev.erase_count(block)
        self.assertEqual(ftl.trim(victims), len(victims))
        for lpn in victims:
            shadow.pop(lpn)
        # 同块活页在释放后立即可读
        self.assertEqual(ftl.read(survivor), shadow[survivor].ljust(PAGE_SIZE, b"\xff"))
        assert_image_matches(self, ftl, shadow)
        # 手动 GC 回收该块（内含被释放的死页）：活页先搬迁，块才被擦除
        ftl.gc_collect()
        self.assertEqual(dev.erase_count(block), erases0 + 1, "含死页的块应被回收")
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)


class TestTrimGCAndCapacity(unittest.TestCase):
    def test_trimmed_dead_pages_reclaimed_by_gc(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = {}
        for i in range(10):
            ftl.write(i, payload(i))
            shadow[i] = payload(i)
        ftl.trim([0, 2, 4, 6, 8])
        for lpn in (0, 2, 4, 6, 8):
            shadow.pop(lpn)
        self.assertGreaterEqual(ftl.capacity()["reclaimable_dead_pages"], 5)
        reclaimed = ftl.gc_collect()
        self.assertGreater(reclaimed, 0)
        cap = ftl.capacity()
        self.assertEqual(cap["reclaimable_dead_pages"], 0)
        self.assertEqual(cap["mapped_pages"], 5)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_trim_triggers_gc_when_commit_page_unavailable(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = {}
        fill_to_gc_boundary(self, ftl, shadow)
        self.assertLessEqual(ftl.capacity()["free_physical_pages"], 8)
        self.assertEqual(ftl.trim([0, 1, 2]), 3)  # 提交页不足：必须先 GC 再提交
        self.assertTrue(any(a.kind == "erase" for a in dev.action_log()))
        for lpn in (0, 1, 2):
            shadow.pop(lpn)
            self.assertIsNone(ftl.read(lpn))
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_zero_budget_trim_distinguishes_budget_from_space(self):
        # 预算耗尽：存在可回收块但 gc_budget=0 → GCBudgetExhaustedError
        dev = RawNand(max_erases=8)
        ftl = FTL(dev, gc_budget=0)
        shadow = {}
        fill_to_gc_boundary(self, ftl, shadow)
        self.assertGreater(ftl.capacity()["reclaimable_dead_pages"], 0)
        seq0 = ftl.commit_seq
        mapping0 = ftl.mapping_snapshot()
        with self.assertRaises(GCBudgetExhaustedError):
            ftl.trim([0, 1])
        self.assertEqual(ftl.commit_seq, seq0, "失败不得推进提交序列号")
        self.assertEqual(ftl.mapping_snapshot(), mapping0, "失败不得改变映射")
        assert_image_matches(self, ftl, shadow)
        # 放宽预算后同一释放成功：证明刚才是预算问题而非无解
        self.assertEqual(ftl.trim([0, 1], gc_budget=8), 2)
        shadow.pop(0)
        shadow.pop(1)
        assert_image_matches(self, ftl, shadow)

    def test_zero_budget_trim_without_gc_need_succeeds(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev, gc_budget=0)  # 空白充足：释放不需要任何擦除
        ftl.write(0, b"x")
        self.assertEqual(ftl.trim([0]), 1)
        self.assertFalse(any(a.kind == "erase" for a in dev.action_log()))
        self.assertIsNone(ftl.read(0))

    def test_trim_out_of_space_is_provable(self):
        dev = RawNand(max_erases=0)  # 禁止任何擦除：死页永不可回收
        ftl = FTL(dev)
        shadow = {}
        fill_to_gc_boundary(self, ftl, shadow)
        self.assertEqual(ftl.capacity()["free_physical_pages"], 8)  # 仅剩保留页
        seq0 = ftl.commit_seq
        with self.assertRaises(OutOfSpaceError):
            ftl.trim([0, 1])
        self.assertEqual(ftl.commit_seq, seq0)
        assert_image_matches(self, ftl, shadow)


class TestTrimBadBlocks(unittest.TestCase):
    def test_trim_and_reclaim_never_touch_bad_blocks(self):
        dev = RawNand(max_erases=8)
        dev.mark_factory_bad(4)
        dev.mark_factory_bad(9)
        ftl = FTL(dev)
        shadow = {}
        for i in range(30):  # 足以触发 GC
            lpn = i % 24
            ftl.write(lpn, payload(i))
            shadow[lpn] = payload(i)
        self.assertEqual(ftl.trim([0, 5, 10, 15, 20]), 5)
        for lpn in (0, 5, 10, 15, 20):
            shadow.pop(lpn)
        ftl.gc_collect()
        for a in dev.action_log():
            self.assertNotIn(a.block, (4, 9), "坏块不得再分配")
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)


class TestTrimCrashAtomicity(unittest.TestCase):
    def test_crash_after_trim_commit_sees_released_image(self):
        # 无需 GC 时 trim 只有 1 个物理动作：掉电发生在提交记录写完之后
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        for i in range(4):
            ftl.write(i, payload(i))
        dev.set_crash_plan([1])
        dev.arm_crash()
        with self.assertRaises(PowerLossError):
            ftl.trim([1, 2])
        dev.reboot()
        ftl2 = FTL(dev)
        self.assertIsNone(ftl2.read(1))  # 提交已落盘：整组释放生效
        self.assertIsNone(ftl2.read(2))
        self.assertEqual(ftl2.read(0), payload(0).ljust(PAGE_SIZE, b"\xff"))
        self.assertEqual(ftl2.read(3), payload(3).ljust(PAGE_SIZE, b"\xff"))
        assert_matches_reference(self, ftl2, dev)


class TestTrimCrashEnumeration(unittest.TestCase):
    """枚举 trim（含其触发的带搬迁 GC）内部每个断电点。

    任何断电点恢复后，可见映像只能是“全部释放前”或“全部释放后”。
    """

    def setUp(self):
        dev = RawNand(max_erases=8)
        for b in range(9):
            dev.mark_factory_bad(b)  # 仅剩 7 块：trim 的提交页必须先做带搬迁的 GC
        ftl = FTL(dev)
        self.shadow = {}
        fill_to_gc_boundary(self, ftl, self.shadow)
        self.trim_lpns = (0, 1, 2, 3)
        for lpn in self.trim_lpns:
            self.assertIn(lpn, self.shadow)  # sanity：待释放页当前已映射
        self.snap_before = dev.snapshot()
        self.n0 = len(dev.action_log())
        ftl.trim(self.trim_lpns)
        self.n1 = len(dev.action_log())
        actions = dev.action_log()[self.n0 : self.n1]
        kinds = {(a.kind, a.purpose) for a in actions}
        self.assertIn(("program", "gc-copy"), kinds, "trim 触发的 GC 应含搬迁")
        self.assertIn(("erase", "gc-erase"), kinds)
        self.assertEqual(actions[-1].purpose, "trim-commit")
        self.trim_commit_idx = self.n1 - 1
        self.dev = dev

    def test_every_trim_crash_point(self):
        # 崩溃计划按“重放后完成的动作数”计数：绝对动作下标 k 对应计划值 k - n0
        for k in range(self.n0 + 1, self.n1 + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap_before)
                self.dev.set_crash_plan([k - self.n0])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                with self.assertRaises(PowerLossError):
                    ftl.trim(self.trim_lpns)
                self.dev.reboot()
                ftl2 = FTL(self.dev)
                # 期望映像：释放前的影子 +（仅当 k 覆盖 trim 提交动作）整组释放
                expected = dict(self.shadow)
                if self.trim_commit_idx < k:
                    for lpn in self.trim_lpns:
                        expected.pop(lpn)
                # 独立维护的逻辑页字典逐页核对最后可见映像
                for lpn in range(24):
                    got = ftl2.read(lpn)
                    exp = expected.get(lpn)
                    if exp is None:
                        self.assertIsNone(got, f"k={k} lpn={lpn} 应为未映射")
                    else:
                        self.assertEqual(
                            got, exp.ljust(PAGE_SIZE, b"\xff"), f"k={k} lpn={lpn} 不符"
                        )
                # 与独立参考解析器重建的已提交映像交叉验证
                assert_matches_reference(self, ftl2, self.dev)
                # 恢复后系统收敛：可继续写与释放
                ftl2.write(23, b"converge")
                self.assertEqual(ftl2.read(23), b"converge".ljust(PAGE_SIZE, b"\xff"))
                self.assertEqual(ftl2.trim([23]), 1)
                self.assertIsNone(ftl2.read(23))


if __name__ == "__main__":
    unittest.main()
