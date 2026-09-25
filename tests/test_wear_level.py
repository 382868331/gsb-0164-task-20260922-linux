"""主动磨损均衡 wear_level：冷页搬迁、候选规则、预算/空间分别报告、
断电枚举（每个物理动作后掉电重挂载，逻辑映像不得丢失）。

场景用设备公开接口确定性构造：dev.erase 制造磨损差，dev.program 写入
冷数据页，提交记录用公开的 encode_commit_record 编码——不调用 FTL 核心
算法得到期望状态；逻辑映像用独立影子字典与 refimg 参考解析器双重核对。
"""
import unittest

from ftl import (
    FTL,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    InvalidInputError,
    PowerLossError,
    RawNand,
    encode_commit_record,
)
from refimg import assert_image_matches, assert_matches_reference


def payload(i, size=12):
    return bytes(((i * 29 + j * 11) % 256 for j in range(size)))


def craft_state(dev, *, worn=(), cold=(), commit_at, fill=()):
    """确定性构造设备状态，返回独立影子字典 {lpn: 原始写入字节串}。

    worn: [(块号, 预擦次数)]；cold: [(块号, [lpn, ...])]（列表序即块内页序）；
    commit_at: 提交记录落页 (块号, 页内偏移)；fill: 用垃圾页填满的块号。
    """
    mapping = [0xFF] * 24
    shadow = {}
    for b, times in worn:
        for _ in range(times):
            dev.erase(b, purpose="setup-wear")
    for b, lpns in cold:
        for i, lpn in enumerate(lpns):
            data = payload(lpn)
            dev.program(b, i, data, purpose="cold-data")
            mapping[lpn] = b * PAGES_PER_BLOCK + i
            shadow[lpn] = data
    for b in fill:
        for p in range(PAGES_PER_BLOCK):
            dev.program(b, p, b"\x5a", purpose="junk")
    dev.program(
        commit_at[0],
        commit_at[1],
        encode_commit_record(1, mapping),
        purpose="setup-commit",
    )
    return shadow


def make_main_scenario():
    """冷页占住低磨损块的确定场景：

    块 0/1 装满冷数据（擦除 0 次）；块 12/13 已擦 3 次、块 14 及其余空白块
    已擦 2 次，均为全空白空块；提交记录在块 2。threshold=2 时块 12/13 是
    最优目的地（擦除最多），腾空的块 0/1（擦除 1 次）成为全设备最低磨损
    空块，将重新优先进入分配。
    """
    dev = RawNand(max_erases=10)
    shadow = craft_state(
        dev,
        worn=[(12, 3), (13, 3)] + [(b, 2) for b in range(2, 16) if b not in (12, 13)],
        cold=[(0, list(range(0, 8))), (1, list(range(8, 16)))],
        commit_at=(2, 0),
    )
    return dev, shadow


class TestWearLevelMoves(unittest.TestCase):
    def test_cold_pages_moved_to_worn_empty_blocks(self):
        dev, shadow = make_main_scenario()
        ftl = FTL(dev)
        assert_image_matches(self, ftl, shadow)  # 维护前镜像核对
        before_log = len(dev.action_log())
        report = ftl.wear_level(64, threshold=2)
        # 候选规则：源块按（擦除次数, 块号）升序 → 块 0 先于块 1；
        # 目的块按（擦除次数降序, 块号）→ 块 12（与 13 等磨损取小号），再块 13
        self.assertEqual(report["vacated"], (0, 1))
        self.assertEqual(report["moved"], 16)
        self.assertEqual(report["stop"], "done")
        self.assertEqual(report["actions"], 20)  # 每块 8 搬迁 + 1 提交 + 1 擦除
        self.assertEqual(len(dev.action_log()) - before_log, report["actions"])
        kinds = {(a.kind, a.purpose) for a in dev.action_log()[before_log:]}
        self.assertIn(("program", "wl-copy"), kinds)
        self.assertIn(("program", "wl-commit"), kinds)
        self.assertIn(("erase", "wl-erase"), kinds)
        # 低磨损块腾空（擦除次数 +1），冷页落到擦写较多的空块
        self.assertEqual(dev.erase_count(0), 1)
        self.assertEqual(dev.erase_count(1), 1)
        self.assertEqual(dev.erase_count(12), 3)
        self.assertEqual(dev.erase_count(13), 3)
        mapping = ftl.mapping_snapshot()
        for lpn in range(0, 8):
            self.assertEqual(mapping[lpn] // PAGES_PER_BLOCK, 12)
        for lpn in range(8, 16):
            self.assertEqual(mapping[lpn] // PAGES_PER_BLOCK, 13)
        # 维护后逻辑映像逐页不变：独立影子字典 + 独立参考解析器
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)
        # 腾空的低磨损块重新进入分配：后续写落到块 0/1
        ftl.write(16, b"reused")
        self.assertIn(ftl.mapping_snapshot()[16] // PAGES_PER_BLOCK, (0, 1))

    def test_remount_after_maintenance(self):
        dev, shadow = make_main_scenario()
        ftl = FTL(dev)
        ftl.wear_level(64, threshold=2)
        ftl2 = FTL(dev)  # 重新挂载：映像与搬迁结果持久
        assert_image_matches(self, ftl2, shadow)
        assert_matches_reference(self, ftl2, dev)
        mapping = ftl2.mapping_snapshot()
        for lpn in range(0, 8):
            self.assertEqual(mapping[lpn] // PAGES_PER_BLOCK, 12)


class TestWearLevelCrashEnumeration(unittest.TestCase):
    """枚举 wear_level 内部每一个物理动作后的断电点。"""

    def setUp(self):
        self.dev, self.shadow = make_main_scenario()
        self.snap = self.dev.snapshot()
        ftl = FTL(self.dev)
        report = ftl.wear_level(64, threshold=2)
        self.assertEqual(report["stop"], "done")
        self.total_actions = report["actions"]
        self.assertEqual(self.total_actions, 20)

    def test_every_wear_level_crash_point(self):
        for k in range(1, self.total_actions + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap)
                self.dev.set_crash_plan([k])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                with self.assertRaises(PowerLossError):
                    ftl.wear_level(64, threshold=2)
                self.dev.reboot()
                ftl2 = FTL(self.dev)
                # 维护不改逻辑页内容：任何断电点恢复后映像都与维护前一致
                assert_image_matches(self, ftl2, self.shadow)
                assert_matches_reference(self, ftl2, self.dev)
                # 恢复后系统收敛：可继续写、可再次维护
                ftl2.write(16, b"converge")
                self.assertEqual(
                    ftl2.read(16), b"converge".ljust(PAGE_SIZE, b"\xff")
                )
                report = ftl2.wear_level(64, threshold=2)
                assert_image_matches(self, ftl2, {**self.shadow, 16: b"converge"})
                self.assertIn(report["stop"], ("done", "no-candidate"))


class TestWearLevelNoCandidate(unittest.TestCase):
    def test_uniform_wear_no_action(self):
        # 全设备磨损一致（全 0）：无任何满足阈值的对 → 无动作
        dev = RawNand(max_erases=10)
        ftl = FTL(dev)
        shadow = {}
        for i in range(6):
            ftl.write(i, payload(i))
            shadow[i] = payload(i)
        before_log = len(dev.action_log())
        report = ftl.wear_level(50, threshold=1)
        self.assertEqual(
            report, {"moved": 0, "actions": 0, "vacated": (), "stop": "no-candidate"}
        )
        self.assertEqual(len(dev.action_log()), before_log, "无候选不得有物理动作")
        assert_image_matches(self, ftl, shadow)

    def test_threshold_not_met_no_action(self):
        # 磨损差仅 1，threshold=2 → 阈值未达，无动作
        dev = RawNand(max_erases=10)
        shadow = craft_state(
            dev,
            worn=[(12, 1)],
            cold=[(0, list(range(0, 8)))],
            commit_at=(2, 0),
        )
        ftl = FTL(dev)
        before_log = len(dev.action_log())
        report = ftl.wear_level(50, threshold=2)
        self.assertEqual(report["stop"], "no-candidate")
        self.assertEqual(report["moved"], 0)
        self.assertEqual(len(dev.action_log()), before_log)
        self.assertEqual(dev.erase_count(0), 0, "未达阈值不得擦除源块")
        assert_image_matches(self, ftl, shadow)


class TestWearLevelSingleFreeBlock(unittest.TestCase):
    def test_only_one_usable_empty_block(self):
        # 仅块 12 一个可用空块（其余块要么装满、要么填满垃圾）：
        # 只能腾空一个源块，之后无满足阈值的目的地，单次维护有界结束
        dev = RawNand(max_erases=10)
        shadow = craft_state(
            dev,
            worn=[(12, 3)],
            cold=[(0, list(range(0, 7))), (1, list(range(7, 14)))],
            commit_at=(1, 7),
            fill=[2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15],
        )
        ftl = FTL(dev)
        report = ftl.wear_level(64, threshold=2)
        self.assertEqual(report["vacated"], (0,))
        self.assertEqual(report["moved"], 7)
        self.assertEqual(report["actions"], 9)  # 7 搬迁 + 1 提交 + 1 擦除
        self.assertEqual(report["stop"], "done")
        self.assertEqual(dev.erase_count(0), 1)
        self.assertEqual(dev.erase_count(1), 0, "块 1 未被处理")
        mapping = ftl.mapping_snapshot()
        for lpn in range(0, 7):
            self.assertEqual(mapping[lpn] // PAGES_PER_BLOCK, 12)
        for lpn in range(7, 14):
            self.assertEqual(mapping[lpn] // PAGES_PER_BLOCK, 1)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)


class TestWearLevelTie(unittest.TestCase):
    def test_equal_wear_tie_broken_by_block_number(self):
        # 等磨损平局：源块 5/9 同擦 2 次 → 先 5 后 9；
        # 目的块 13/14 同擦 4 次 → 先 13 后 14
        dev = RawNand(max_erases=10)
        shadow = craft_state(
            dev,
            worn=[(5, 2), (9, 2), (13, 4), (14, 4)],
            cold=[(5, [0, 1, 2, 3]), (9, [4, 5, 6, 7])],
            commit_at=(2, 0),
        )
        ftl = FTL(dev)
        report = ftl.wear_level(64, threshold=2)
        self.assertEqual(report["vacated"], (5, 9))
        self.assertEqual(report["moved"], 8)
        self.assertEqual(report["stop"], "done")
        mapping = ftl.mapping_snapshot()
        for lpn in range(0, 4):
            self.assertEqual(mapping[lpn] // PAGES_PER_BLOCK, 13)
        for lpn in range(4, 8):
            self.assertEqual(mapping[lpn] // PAGES_PER_BLOCK, 14)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)


class TestWearLevelBudgetAndSpace(unittest.TestCase):
    """预算耗尽与空间不足分开报告（stop="budget" vs stop="space"）。"""

    def test_budget_too_small_for_first_block(self):
        dev, shadow = make_main_scenario()
        ftl = FTL(dev)
        before_log = len(dev.action_log())
        report = ftl.wear_level(9, threshold=2)  # 块 0 代价 10 > 9
        self.assertEqual(
            report, {"moved": 0, "actions": 0, "vacated": (), "stop": "budget"}
        )
        self.assertEqual(len(dev.action_log()), before_log, "超预算不得有物理动作")
        assert_image_matches(self, ftl, shadow)

    def test_budget_allows_exactly_one_block(self):
        dev, shadow = make_main_scenario()
        ftl = FTL(dev)
        report = ftl.wear_level(10, threshold=2)  # 恰好够块 0（10 个动作）
        self.assertEqual(report["vacated"], (0,))
        self.assertEqual(report["moved"], 8)
        self.assertEqual(report["actions"], 10)
        self.assertEqual(report["stop"], "budget")  # 块 1 代价 10 > 剩余 0
        self.assertEqual(dev.erase_count(0), 1)
        self.assertEqual(dev.erase_count(1), 0)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_space_insufficient_reported_separately(self):
        # 块 0 有 8 个有效页：搬迁需 8 页 + 提交 1 页，但源块之外全设备
        # 仅剩块 12 的 8 个空白页 → 空间不足（与预算耗尽分开报告）
        dev = RawNand(max_erases=10)
        shadow = craft_state(
            dev,
            worn=[(12, 3)],
            cold=[(0, list(range(0, 8))), (1, list(range(8, 15)))],
            commit_at=(1, 7),
            fill=[2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15],
        )
        ftl = FTL(dev)
        before_log = len(dev.action_log())
        report = ftl.wear_level(64, threshold=2)
        self.assertEqual(
            report, {"moved": 0, "actions": 0, "vacated": (), "stop": "space"}
        )
        self.assertEqual(len(dev.action_log()), before_log, "空间不足不得有物理动作")
        self.assertEqual(dev.erase_count(0), 0)
        assert_image_matches(self, ftl, shadow)


class TestWearLevelValidation(unittest.TestCase):
    def test_invalid_arguments_rejected(self):
        dev = RawNand(max_erases=10)
        ftl = FTL(dev)
        for bad in (True, -1, 1.5, "3"):
            with self.subTest(max_moves=bad):
                with self.assertRaises(InvalidInputError):
                    ftl.wear_level(bad, threshold=1)
        for bad in (True, 0, -1, 2.5):
            with self.subTest(threshold=bad):
                with self.assertRaises(InvalidInputError):
                    ftl.wear_level(10, threshold=bad)


if __name__ == "__main__":
    unittest.main()
