"""验收核心：枚举短写入轨迹的每个断电点，与最后已提交逻辑映像比较。"""
import unittest

from ftl import FTL, PAGE_SIZE, PowerLossError, RawNand
from refimg import assert_matches_reference, parse_committed_image


def payload(i, size=10):
    return bytes(((i * 13 + j * 7) % 256 for j in range(size)))


TRACE = [(i, payload(i)) for i in range(8)]  # 8 次写，每次 2 个物理动作，共 16 个


def run_trace(ftl):
    for lpn, data in TRACE:
        ftl.write(lpn, data)


class TestCrashPointEnumeration(unittest.TestCase):
    def setUp(self):
        # 基线运行（不注入崩溃），记录每次写的提交动作下标与完整轨迹快照
        self.dev = RawNand(max_erases=4)
        self.snap0 = self.dev.snapshot()
        ftl = FTL(self.dev)
        self.commit_idx = []
        for lpn, data in TRACE:
            ftl.write(lpn, data)
            last = self.dev.action_log()[-1]
            self.assertEqual(last.purpose, "commit")
            self.commit_idx.append(len(self.dev.action_log()) - 1)
        self.total_actions = len(self.dev.action_log())
        self.assertEqual(self.total_actions, 2 * len(TRACE))

    def expected_shadow(self, k):
        """崩溃发生在第 k 个动作（1 起始）之后：提交动作下标 < k 的写已提交。"""
        shadow = {}
        for (lpn, data), cidx in zip(TRACE, self.commit_idx):
            if cidx < k:
                shadow[lpn] = data
        return shadow

    def check_recovered(self, k):
        dev = self.dev
        dev.reboot()
        ftl2 = FTL(dev)
        # 与测试侧独立维护的“已提交写”影子比较
        shadow = self.expected_shadow(k)
        for lpn in range(24):
            got = ftl2.read(lpn)
            exp = shadow.get(lpn)
            if exp is None:
                self.assertIsNone(got, f"k={k} lpn={lpn} 应为未映射")
            else:
                self.assertEqual(
                    got, exp.ljust(PAGE_SIZE, b"\xff"), f"k={k} lpn={lpn} 数据不符"
                )
        # 与独立参考解析器重建的映像交叉验证
        assert_matches_reference(self, ftl2, dev)
        # 恢复后可继续写并收敛
        ftl2.write(20, b"post-crash")
        self.assertEqual(ftl2.read(20), b"post-crash".ljust(PAGE_SIZE, b"\xff"))
        dev2 = ftl2.device
        ftl3 = FTL(dev2)
        self.assertEqual(ftl3.read(20), b"post-crash".ljust(PAGE_SIZE, b"\xff"))

    def test_every_crash_point(self):
        for k in range(1, self.total_actions + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap0)
                self.dev.set_crash_plan([k])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                crashed = False
                try:
                    run_trace(ftl)
                except PowerLossError:
                    crashed = True
                self.assertTrue(crashed, f"k={k} 应发生掉电")
                self.assertTrue(self.dev.crashed)
                self.check_recovered(k)

    def test_no_crash_control(self):
        # 对照组：计划耗尽后不再掉电，完整轨迹全部提交
        self.dev.restore(self.snap0)
        self.dev.set_crash_plan([self.total_actions + 5])
        self.dev.arm_crash()
        ftl = FTL(self.dev)
        run_trace(ftl)
        shadow = {lpn: data for lpn, data in TRACE}
        for lpn, data in shadow.items():
            self.assertEqual(ftl.read(lpn), data.ljust(PAGE_SIZE, b"\xff"))
        assert_matches_reference(self, ftl, self.dev)

    def test_reference_parser_sees_exactly_committed(self):
        # 参考解析器自身 sanity：崩溃点 k 之后，映像大小等于已提交写数
        for k in (1, 2, 3, self.total_actions):
            self.dev.restore(self.snap0)
            self.dev.set_crash_plan([k])
            self.dev.arm_crash()
            ftl = FTL(self.dev)
            try:
                run_trace(ftl)
            except PowerLossError:
                pass
            self.dev.reboot()
            image = parse_committed_image(self.dev)
            self.assertEqual(len(image), len(self.expected_shadow(k)))


if __name__ == "__main__":
    unittest.main()
