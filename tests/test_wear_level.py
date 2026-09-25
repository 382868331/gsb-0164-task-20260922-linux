"""显式磨损均衡 wear_level：确定性场景、断电枚举、边界与预算语义。

场景构造约定：
- 主场景用真实写负载（冷页写一次后不再触碰，热页反复覆盖）自然形成
  低磨损块持有有效页、高磨损块持有空白页的分化布局；
- 紧张空间场景（仅剩一个可用空块）用 14 个工厂坏块 + 真实写 +
  直接页编程构造，布局完全确定；
- 维护前后用独立影子字典与独立参考解析器（refimg）双重核对逻辑映像。
"""
import unittest

from ftl import (
    BLOCKS,
    FTL,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    InvalidInputError,
    PowerLossError,
    RawNand,
    encode_commit_record,
)
from refimg import (
    assert_image_matches,
    assert_matches_reference,
)


def payload(i, size=16):
    return bytes(((i * 31 + j * 7 + 1) % 256 for j in range(size)))


def build_hot_cold(case):
    """真实负载构造冷热分化布局（确定性，无随机）：

    4 个工厂坏块；先写满 24 个逻辑页（冷数据），再反复覆盖 3 个热页。
    结果：擦除次数 0 的块持有长期不动的有效页，擦除次数 6~7 的块持有
    空白页——正是磨损均衡要处理的形态。
    """
    dev = RawNand(max_erases=32)
    for b in range(4):
        dev.mark_factory_bad(b)
    ftl = FTL(dev)
    shadow = {}
    for i in range(24):
        d = payload(0x10 + i)
        ftl.write(i, d)
        shadow[i] = d
    for r in range(60):
        for h in (0, 1, 2):
            d = payload(0x100 + r * 3 + h)
            ftl.write(h, d)
            shadow[h] = d
    # 布局 sanity：必须存在“低磨损+有效页”与“高磨损+空白页”两类块
    counts = ftl.capacity()["erase_counts"]
    cold = [b for b in range(BLOCKS) if counts[b] == 0 and ftl._view[b].valid > 0]
    hot = [b for b in range(BLOCKS) if counts[b] >= 6 and ftl._view[b].blanks > 0]
    case.assertTrue(cold, "应存在持有有效页的低磨损块")
    case.assertTrue(hot, "应存在持有空白页的高磨损块")
    return dev, ftl, shadow


class TestWearLevelMain(unittest.TestCase):
    """主场景：冷页占住低磨损块，维护后低磨损块重新进入分配。"""

    def test_cold_block_reenters_allocation(self):
        dev, ftl, shadow = build_hot_cold(self)
        counts = ftl.capacity()["erase_counts"]
        coldest = min(
            b
            for b in range(BLOCKS)
            if counts[b] == 0 and not dev.is_bad_block(b)
        )
        self.assertGreater(ftl._view[coldest].valid, 0)
        image_before = {l: ftl.read(l) for l in range(24)}

        report = ftl.wear_level(3)  # 预算 3 页：恰好腾空最冷块（3 个有效页）

        self.assertEqual(report["moved_pages"], 3)
        self.assertEqual(report["leveled_blocks"], (coldest,))
        # 预算按实际物理动作计：3 次搬迁编程 + 1 次提交 + 1 次擦除
        self.assertEqual(report["program_actions"], 4)
        self.assertEqual(report["erase_actions"], 1)
        self.assertEqual(report["stop_reason"], "budget-exhausted")
        # 源块被擦除一次、变为全空白，重新进入分配
        self.assertEqual(dev.erase_count(coldest), 1)
        self.assertEqual(ftl._view[coldest].blanks, PAGES_PER_BLOCK)
        # 逻辑映像逐页不变（独立影子字典 + 独立参考解析器双重核对）
        for l in range(24):
            self.assertEqual(ftl.read(l), image_before[l], f"lpn={l} 维护后内容改变")
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)
        # 低磨损块重新进入分配：新写入落在刚腾空的块上
        ftl.write(5, b"post-level")
        self.assertEqual(ftl.mapping_snapshot()[5] // PAGES_PER_BLOCK, coldest)
        self.assertEqual(ftl.read(5), b"post-level".ljust(PAGE_SIZE, b"\xff"))

    def test_full_maintenance_then_converges(self):
        dev, ftl, shadow = build_hot_cold(self)
        image_before = {l: ftl.read(l) for l in range(24)}
        # 预算 24（=逻辑页数）：逐块腾空，最后一块（7 个有效页）剩余预算
        # 不足 → 整块不搬，预算耗尽即停（只做一次有限维护）
        report = ftl.wear_level(24)
        self.assertEqual(report["moved_pages"], 21)
        self.assertEqual(len(report["leveled_blocks"]), 6)
        self.assertEqual(report["stop_reason"], "budget-exhausted")
        for l in range(24):
            self.assertEqual(ftl.read(l), image_before[l])
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)
        # 放宽预算后重跑：已无预算约束，最终因高磨损块空白页不足以再腾空
        # 任一块而停——空间不足，与预算耗尽严格分开，且不再有动作
        report2 = ftl.wear_level(32)
        self.assertEqual(report2["moved_pages"], 0)
        self.assertEqual(report2["stop_reason"], "insufficient-space")
        self.assertEqual(report2["program_actions"], 0)
        self.assertEqual(report2["erase_actions"], 0)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_no_candidate_below_threshold_no_action(self):
        # 全部块等磨损（全新设备）：没有任何擦写更多的可用块，阈值未达 → 无动作
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = {}
        for i in range(6):
            ftl.write(i, payload(i))
            shadow[i] = payload(i)
        log0 = len(dev.action_log())
        seq0 = ftl.commit_seq
        report = ftl.wear_level(4)
        self.assertEqual(report["moved_pages"], 0)
        self.assertEqual(report["leveled_blocks"], ())
        self.assertEqual(report["program_actions"], 0)
        self.assertEqual(report["erase_actions"], 0)
        self.assertEqual(report["stop_reason"], "no-candidate")
        self.assertEqual(len(dev.action_log()), log0, "无候选不得有任何物理动作")
        self.assertEqual(ftl.commit_seq, seq0)
        assert_image_matches(self, ftl, shadow)

    def test_no_candidate_when_no_valid_pages(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        report = ftl.wear_level(4)
        self.assertEqual(report["moved_pages"], 0)
        self.assertEqual(report["stop_reason"], "no-candidate")

    def test_invalid_max_moves_rejected(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        for bad in (True, -1, 1.5, "3", None):
            with self.assertRaises(InvalidInputError, msg=f"max_moves={bad!r}"):
                ftl.wear_level(bad)


class TestWearLevelCrashEnumeration(unittest.TestCase):
    """枚举 wear_level 内部每个物理动作之后的断电点，重启核对逻辑映像。"""

    def setUp(self):
        dev, ftl, shadow = build_hot_cold(self)
        self.shadow = shadow
        self.image_before = {l: ftl.read(l) for l in range(24)}
        self.snap = dev.snapshot()
        self.n0 = len(dev.action_log())
        self.report = ftl.wear_level(3)  # 基线运行：记录动作区间
        self.n1 = len(dev.action_log())
        self.assertGreater(self.n1, self.n0)
        kinds = {a.purpose for a in dev.action_log()[self.n0 : self.n1]}
        self.assertEqual(kinds, {"wear-copy", "wear-commit", "wear-erase"})
        self.dev = dev

    def test_every_wear_level_crash_point(self):
        for k in range(self.n0 + 1, self.n1 + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap)
                self.dev.set_crash_plan([k - self.n0])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                with self.assertRaises(PowerLossError):
                    ftl.wear_level(3)
                self.dev.reboot()
                ftl2 = FTL(self.dev)
                # 维护不改逻辑页内容：任何断电点恢复后映像 = 维护前映像
                for l in range(24):
                    self.assertEqual(
                        ftl2.read(l),
                        self.image_before[l],
                        f"k={k} lpn={l} 恢复后数据不符",
                    )
                assert_image_matches(self, ftl2, self.shadow)
                assert_matches_reference(self, ftl2, self.dev)
                # 恢复后系统收敛：可继续写、可再做一次有限维护
                ftl2.write(23, b"converge")
                self.assertEqual(
                    ftl2.read(23), b"converge".ljust(PAGE_SIZE, b"\xff")
                )
                ftl2.wear_level(2)
                self.assertEqual(
                    ftl2.read(23), b"converge".ljust(PAGE_SIZE, b"\xff")
                )
                assert_matches_reference(self, ftl2, self.dev)

    def test_no_crash_control_matches_baseline(self):
        self.dev.restore(self.snap)
        ftl = FTL(self.dev)
        report = ftl.wear_level(3)
        self.assertEqual(report, self.report)
        for l in range(24):
            self.assertEqual(ftl.read(l), self.image_before[l])


def hand_built(dev, *, data_pages, commit_block, commit_page, seq=1):
    """手工铺设一份已提交映像：data_pages 为 {lpn: (block, page)}，
    提交记录写在 (commit_block, commit_page)。返回影子字典。"""
    mapping = [0xFF] * 24
    shadow = {}
    for lpn, (b, p) in data_pages.items():
        d = payload(0x80 + lpn)
        dev.program(b, p, d, purpose="history")
        mapping[lpn] = b * PAGES_PER_BLOCK + p
        shadow[lpn] = d
    dev.program(commit_block, commit_page, encode_commit_record(seq, mapping),
                purpose="history")
    return shadow


class TestWearLevelTightSpace(unittest.TestCase):
    """仅剩一个可用空块（块级）与更紧张的页级空间边界。

    构造：14 个工厂坏块，仅块 14、15 可用。块 14（ec 0）手工铺设
    2 个有效页 + 提交记录；块 15 经一次擦除（ec 1）后用直接页编程
    填充历史占用，控制其剩余空白页数。
    """

    def _build(self, history_pages):
        dev = RawNand(max_erases=8)
        for b in range(14):
            dev.mark_factory_bad(b)
        shadow = hand_built(
            dev,
            data_pages={0: (14, 0), 1: (14, 1)},
            commit_block=14,
            commit_page=2,
        )
        dev.erase(15)  # 块 15：ec 1，全空白
        for p in range(history_pages):
            dev.program(15, p, payload(0x40 + p), purpose="history")
        ftl = FTL(dev)
        self.assertEqual(ftl._view[14].valid, 2)
        self.assertEqual(ftl._view[15].blanks, PAGES_PER_BLOCK - history_pages)
        self.assertEqual(dev.erase_count(14), 0)
        self.assertEqual(dev.erase_count(15), 1)
        return dev, ftl, shadow

    def test_only_one_free_block_insufficient_space(self):
        # 仅剩一个可用空块且其中只有 1 个空白页：
        # 腾空源块需 2 个目标页 + 1 个提交页 = 3 页 → 空间不足，无动作
        dev, ftl, shadow = self._build(history_pages=7)
        self.assertEqual(ftl.capacity()["free_physical_pages"], 1 + 5)
        log0 = len(dev.action_log())
        seq0 = ftl.commit_seq
        report = ftl.wear_level(4)
        self.assertEqual(report["moved_pages"], 0)
        self.assertEqual(report["stop_reason"], "insufficient-space")
        self.assertEqual(report["program_actions"], 0)
        self.assertEqual(report["erase_actions"], 0)
        self.assertEqual(len(dev.action_log()), log0, "空间不足不得有任何物理动作")
        self.assertEqual(ftl.commit_seq, seq0)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)
        # 与预算耗尽分开：预算是足够的（4 > 0），失败原因是空间
        self.assertNotEqual(report["stop_reason"], "budget-exhausted")

    def test_exactly_enough_space_succeeds(self):
        # 对照：唯一的可用空块留出 3 个空白页（2 目标 + 1 提交），维护成功
        dev, ftl, shadow = self._build(history_pages=5)
        report = ftl.wear_level(4)
        self.assertEqual(report["moved_pages"], 2)
        self.assertEqual(report["leveled_blocks"], (14,))
        self.assertEqual(report["program_actions"], 3)  # 2 搬迁 + 1 提交
        self.assertEqual(report["erase_actions"], 1)
        self.assertEqual(dev.erase_count(14), 1)
        self.assertEqual(ftl._view[14].blanks, PAGES_PER_BLOCK)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)
        # 块 14 重新进入分配
        ftl.write(2, b"reused")
        self.assertEqual(ftl.mapping_snapshot()[2] // PAGES_PER_BLOCK, 14)


class TestWearLevelTieAndLimits(unittest.TestCase):
    """等磨损平局、坏块与磨损上限不得绕过。"""

    def test_tie_broken_by_block_number(self):
        # 两个源块等磨损（ec 0）→ 选块号小者；两个目标块等磨损 → 同样块号小者
        dev = RawNand(max_erases=8)
        for b in list(range(12)) + [13]:
            dev.mark_factory_bad(b)
        # 块 12：2 个有效页 + 提交记录（全在源块）；块 13 保持空白（同样 ec 0）
        shadow = hand_built(
            dev,
            data_pages={0: (12, 0), 1: (12, 1)},
            commit_block=12,
            commit_page=2,
        )
        dev.erase(14)  # 块 14、15 等磨损（ec 1）且全空白
        dev.erase(15)
        ftl = FTL(dev)
        report = ftl.wear_level(2)
        self.assertEqual(report["leveled_blocks"], (12,))
        # 搬迁目标应为块号较小的等磨损块 14（等磨损平局按块号定序）
        copy_targets = {
            a.block for a in dev.action_log() if a.purpose == "wear-copy"
        }
        self.assertEqual(copy_targets, {14})
        # 提交记录页落在源块之外的可用块
        commit_blocks = {
            a.block for a in dev.action_log() if a.purpose == "wear-commit"
        }
        self.assertTrue(commit_blocks <= {14, 15} and 12 not in commit_blocks)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_worn_blocks_never_used(self):
        # 高磨损块已达上限：不得作为目标（不可再擦）也不得绕过上限
        dev = RawNand(max_erases=2)
        for b in list(range(12)) + [13]:
            dev.mark_factory_bad(b)
        shadow = hand_built(
            dev,
            data_pages={0: (12, 0), 1: (12, 1)},
            commit_block=12,
            commit_page=2,
        )
        dev.erase(14)
        dev.erase(14)  # 块 14：ec 2 = max_erases，已磨损
        dev.erase(15)  # 块 15：ec 1，可用
        ftl = FTL(dev)
        report = ftl.wear_level(2)
        self.assertEqual(report["moved_pages"], 2)
        copy_targets = {
            a.block for a in dev.action_log() if a.purpose == "wear-copy"
        }
        self.assertEqual(copy_targets, {15}, "磨损块不得接收搬迁页")
        commit_blocks = {
            a.block for a in dev.action_log() if a.purpose == "wear-commit"
        }
        self.assertEqual(commit_blocks, {15}, "磨损块不得接收提交页")
        self.assertEqual(dev.erase_count(14), 2, "磨损块不得被擦除")
        self.assertLessEqual(dev.erase_count(15), 2)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_bad_blocks_never_used(self):
        dev, ftl, shadow = build_hot_cold(self)
        ftl.wear_level(24)
        for a in dev.action_log():
            self.assertNotIn(a.block, (0, 1, 2, 3), "坏块不得参与任何物理动作")

    def test_budget_zero_is_budget_exhausted_not_no_candidate(self):
        dev, ftl, shadow = build_hot_cold(self)
        log0 = len(dev.action_log())
        report = ftl.wear_level(0)
        self.assertEqual(report["moved_pages"], 0)
        self.assertEqual(report["stop_reason"], "budget-exhausted")
        self.assertEqual(len(dev.action_log()), log0)
        assert_image_matches(self, ftl, shadow)

    def test_partial_budget_stops_between_blocks(self):
        dev, ftl, shadow = build_hot_cold(self)
        # 最冷块有 3 个有效页：预算 2 不够腾空它 → 无动作
        counts = ftl.capacity()["erase_counts"]
        coldest = min(
            b
            for b in range(BLOCKS)
            if counts[b] == 0 and not dev.is_bad_block(b)
        )
        n = ftl._view[coldest].valid
        self.assertGreater(n, 2)
        report = ftl.wear_level(2)
        self.assertEqual(report["moved_pages"], 0)
        self.assertEqual(report["stop_reason"], "budget-exhausted")
        self.assertEqual(report["program_actions"], 0)
        assert_image_matches(self, ftl, shadow)


class TestWearLevelHandBuiltImage(unittest.TestCase):
    """手工构造的提交映像（直接页编程 + encode_commit_record）：

    验证维护只依赖已提交映像与物理状态，不依赖 FTL 内存历史。
    """

    def test_hand_built_image(self):
        dev = RawNand(max_erases=8)
        for b in range(14):
            dev.mark_factory_bad(b)
        # 块 14：页 0、1 为有效数据（lpn 0、1），页 2 为提交记录
        dev.program(14, 0, payload(0), purpose="history")
        dev.program(14, 1, payload(1), purpose="history")
        mapping = [0xFF] * 24
        mapping[0] = 14 * PAGES_PER_BLOCK + 0
        mapping[1] = 14 * PAGES_PER_BLOCK + 1
        dev.program(14, 2, encode_commit_record(1, mapping), purpose="history")
        dev.erase(15)  # 块 15：ec 1，全空白
        ftl = FTL(dev)
        self.assertEqual(ftl.read(0), payload(0).ljust(PAGE_SIZE, b"\xff"))
        self.assertEqual(ftl.read(1), payload(1).ljust(PAGE_SIZE, b"\xff"))
        report = ftl.wear_level(2)
        self.assertEqual(report["moved_pages"], 2)
        self.assertEqual(report["leveled_blocks"], (14,))
        self.assertEqual(ftl.read(0), payload(0).ljust(PAGE_SIZE, b"\xff"))
        self.assertEqual(ftl.read(1), payload(1).ljust(PAGE_SIZE, b"\xff"))
        assert_matches_reference(self, ftl, dev)
        self.assertEqual(dev.erase_count(14), 1)


if __name__ == "__main__":
    unittest.main()
