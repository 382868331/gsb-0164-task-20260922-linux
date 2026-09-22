"""垃圾整理：数据完整性、提交先于擦除的顺序、整理中断（断电）枚举。"""
import unittest

from ftl import FTL, PAGE_SIZE, PowerLossError, RawNand
from refimg import assert_image_matches, assert_matches_reference


def payload(i, size=12):
    return bytes(((i * 29 + j * 11) % 256 for j in range(size)))


class TestGCIntegrity(unittest.TestCase):
    def test_gc_preserves_data_and_reclaims(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = {}
        gc_seen = False
        for i in range(80):
            lpn = i % 24
            data = payload(i)
            ftl.write(lpn, data)
            shadow[lpn] = data
            if any(a.kind == "erase" for a in dev.action_log()):
                gc_seen = True
            assert_image_matches(self, ftl, shadow)  # 每次写后全量比对
        self.assertTrue(gc_seen, "80 次写应触发至少一次垃圾整理")
        cap = ftl.capacity()
        self.assertGreater(cap["free_physical_pages"], 0)
        self.assertGreater(max(cap["erase_counts"]), 0)
        assert_matches_reference(self, ftl, dev)

    def test_commit_precedes_erase(self):
        # 顺序契约：含有效页的受害块，其 gc-erase 之前必须有 gc-commit
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        for i in range(80):
            ftl.write(i % 24, payload(i))
        log = dev.action_log()
        last_commit = -1
        for idx, a in enumerate(log):
            if a.kind == "program" and a.purpose in ("commit", "gc-commit"):
                last_commit = idx
            if a.kind == "erase":
                self.assertGreater(
                    last_commit, -1, "任何擦除之前必须存在已完成的提交"
                )
        self.assertTrue(any(a.kind == "erase" for a in log))

    def test_manual_gc_collect(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = {}
        for i in range(30):
            ftl.write(i % 24, payload(i))
            shadow[i % 24] = payload(i)
        before = ftl.capacity()
        self.assertGreater(before["reclaimable_dead_pages"], 0)
        reclaimed = ftl.gc_collect()
        self.assertGreater(reclaimed, 0)
        after = ftl.capacity()
        self.assertEqual(after["reclaimable_dead_pages"], 0)
        self.assertGreater(after["free_physical_pages"], before["free_physical_pages"])
        assert_image_matches(self, ftl, shadow)


class TestGCCrashEnumeration(unittest.TestCase):
    """枚举触发 GC 的那一次写内部的每个断电点。"""

    def setUp(self):
        # 10 个工厂坏块 → 仅 6 块（48 页）可用：空白页耗尽时每个块都
        # 同时持有有效页与死页，GC 必须搬迁（gc-copy）才能回收。
        dev = RawNand(max_erases=8)
        for b in range(10):
            dev.mark_factory_bad(b)
        ftl = FTL(dev)
        self.shadow = {}
        i = 0
        while True:  # 驱动到第一次“带搬迁”的 GC：记录触发写之前的快照
            snap = dev.snapshot()
            before = len(dev.action_log())
            lpn = i % 24
            data = payload(i)
            ftl.write(lpn, data)
            after = len(dev.action_log())
            actions = dev.action_log()[before:after]
            if any(a.purpose == "gc-copy" for a in actions):
                self.dev = dev
                self.snap_before = snap
                self.n0 = before
                self.n1 = after
                self.trigger = (lpn, data)
                self.trigger_commit_idx = after - 1
                self.assertEqual(actions[-1].purpose, "commit")
                break
            self.shadow[lpn] = data
            i += 1
            self.assertLess(i, 200, "应在 200 次写内触发带搬迁的 GC")
        #  sanity：触发写确实包含搬迁、提交与擦除
        kinds = {(a.kind, a.purpose) for a in dev.action_log()[self.n0 : self.n1]}
        self.assertIn(("program", "gc-copy"), kinds)
        self.assertIn(("erase", "gc-erase"), kinds)

    def test_every_gc_crash_point(self):
        # 崩溃计划按“重放后完成的动作数”计数：绝对动作下标 k 对应计划值 k - n0
        for k in range(self.n0 + 1, self.n1 + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap_before)
                self.dev.set_crash_plan([k - self.n0])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                lpn, data = self.trigger
                with self.assertRaises(PowerLossError):
                    ftl.write(lpn, data)
                self.dev.reboot()
                ftl2 = FTL(self.dev)
                # 期望望映像：触发写之前的已提交影子 +（仅当 k 覆盖其提交动作）触发写
                expected = dict(self.shadow)
                if self.trigger_commit_idx < k:
                    expected[lpn] = data
                for l in range(24):
                    got = ftl2.read(l)
                    exp = expected.get(l)
                    if exp is None:
                        self.assertIsNone(got, f"k={k} lpn={l} 应为未映射")
                    else:
                        self.assertEqual(
                            got, exp.ljust(PAGE_SIZE, b"\xff"), f"k={k} lpn={l} 不符"
                        )
                assert_matches_reference(self, ftl2, self.dev)
                # 恢复后系统收敛：可继续写、可手动整理
                ftl2.write(23, b"converge")
                self.assertEqual(ftl2.read(23), b"converge".ljust(PAGE_SIZE, b"\xff"))
                ftl2.gc_collect()
                self.assertEqual(ftl2.read(23), b"converge".ljust(PAGE_SIZE, b"\xff"))


if __name__ == "__main__":
    unittest.main()
