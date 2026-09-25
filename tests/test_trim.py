"""逻辑页释放（trim）：幂等释放、原子提交、释放后重写、混块 GC、
坏块、零 GC 预算，以及 trim 与其触发 GC 的逐断电点枚举。

期望值全部由测试侧独立维护的影子字典给出，并用 tests/refimg.py 的
独立解析器（不调用被测核心）核对设备上最后可见的已提交映像。
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
from refimg import assert_image_matches, assert_matches_reference, parse_committed_image


def payload(i, size=12):
    return bytes(((i * 17 + j * 11 + 3) % 256 for j in range(size)))


def slot(data):
    return data.ljust(PAGE_SIZE, b"\xff")


class Shadow:
    """测试侧独立维护的逻辑页字典：只记录调用序列，不调用被测核心。"""

    def __init__(self):
        self.d = {}

    def write(self, lpn, data):
        self.d[lpn] = data

    def trim(self, lpns):
        for lpn in lpns:
            self.d.pop(lpn, None)

    def check(self, case, ftl):
        assert_image_matches(case, ftl, self.d)


class TestTrimBasic(unittest.TestCase):
    def setUp(self):
        self.dev = RawNand(max_erases=8)
        self.ftl = FTL(self.dev)
        self.shadow = Shadow()
        for i in range(8):
            self.ftl.write(i, payload(i))
            self.shadow.write(i, payload(i))

    def test_trim_unmaps_and_returns_count(self):
        released = self.ftl.trim([1, 3, 5])
        self.assertEqual(released, 3)
        self.shadow.trim([1, 3, 5])
        for lpn in (1, 3, 5):
            self.assertIsNone(self.ftl.read(lpn), f"lpn={lpn} 释放后应读 None")
        for lpn in (0, 2, 4, 6, 7):
            self.assertEqual(self.ftl.read(lpn), slot(self.shadow.d[lpn]))
        self.shadow.check(self, self.ftl)

    def test_trim_is_single_commit_record(self):
        # 整组释放只允许一条新提交记录（一个物理动作）
        n = len(self.dev.action_log())
        seq = self.ftl.commit_seq
        self.ftl.trim([0, 2, 4, 6])
        actions = self.dev.action_log()[n:]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].kind, "program")
        self.assertEqual(actions[0].purpose, "trim-commit")
        self.assertEqual(self.ftl.commit_seq, seq + 1)

    def test_trim_does_not_erase_block(self):
        # 释放只是撤映射：绝不立即擦除任何物理块
        self.ftl.trim([0, 1, 2, 3])
        self.assertFalse(
            any(a.kind == "erase" for a in self.dev.action_log()),
            "trim 不得立即擦除物理块",
        )

    def test_trimmed_pages_become_dead_in_capacity(self):
        cap0 = self.ftl.capacity()
        self.ftl.trim([0, 1, 2])
        cap = self.ftl.capacity()
        self.assertEqual(cap["mapped_pages"], cap0["mapped_pages"] - 3)
        # 死页 +4：3 个被释放的数据页 + 1 条被取代的旧提交记录
        self.assertEqual(
            cap["reclaimable_dead_pages"],
            cap0["reclaimable_dead_pages"] + 4,
        )
        self.assertEqual(cap["free_physical_pages"], cap0["free_physical_pages"] - 1)

    def test_rewrite_after_trim_uses_allocation(self):
        old_ppn = self.ftl.mapping_snapshot()[1]
        self.ftl.trim([1, 3])
        self.shadow.trim([1, 3])
        self.ftl.write(1, b"rewritten")
        self.shadow.write(1, b"rewritten")
        self.assertEqual(self.ftl.read(1), slot(b"rewritten"))
        self.assertNotEqual(self.ftl.mapping_snapshot()[1], old_ppn, "重写必须走新页分配")
        self.assertIsNone(self.ftl.read(3))
        self.shadow.check(self, self.ftl)

    def test_trim_persists_across_remount(self):
        self.ftl.trim([1, 2, 3, 7])
        self.shadow.trim([1, 2, 3, 7])
        seq = self.ftl.commit_seq
        ftl2 = FTL(self.dev)
        self.assertEqual(ftl2.commit_seq, seq)
        self.shadow.check(self, ftl2)
        assert_matches_reference(self, ftl2, self.dev)


class TestTrimIdempotent(unittest.TestCase):
    def setUp(self):
        self.dev = RawNand(max_erases=8)
        self.ftl = FTL(self.dev)
        for i in range(6):
            self.ftl.write(i, payload(i))

    def test_empty_list_is_noop(self):
        seq = self.ftl.commit_seq
        n = len(self.dev.action_log())
        self.assertEqual(self.ftl.trim([]), 0)
        self.assertEqual(self.ftl.commit_seq, seq)
        self.assertEqual(len(self.dev.action_log()), n)

    def test_all_unmapped_is_noop(self):
        seq = self.ftl.commit_seq
        n = len(self.dev.action_log())
        self.assertEqual(self.ftl.trim([20, 21, 22, 23]), 0)
        self.assertEqual(self.ftl.commit_seq, seq)
        self.assertEqual(len(self.dev.action_log()), n)

    def test_duplicate_lpns_count_once(self):
        seq = self.ftl.commit_seq
        n = len(self.dev.action_log())
        released = self.ftl.trim([1, 1, 1, 2, 2])
        self.assertEqual(released, 2)
        self.assertEqual(self.ftl.commit_seq, seq + 1)
        # 只有一条提交记录
        self.assertEqual(len(self.dev.action_log()) - n, 1)

    def test_repeated_trim_is_idempotent(self):
        self.assertEqual(self.ftl.trim([0, 1]), 2)
        seq = self.ftl.commit_seq
        n = len(self.dev.action_log())
        # 再次释放：部分已释放、混入从未映射的页、重复页号
        self.assertEqual(self.ftl.trim([0, 1, 1, 20]), 0)
        self.assertEqual(self.ftl.commit_seq, seq)
        self.assertEqual(len(self.dev.action_log()), n)
        self.assertIsNone(self.ftl.read(0))
        self.assertIsNone(self.ftl.read(1))
        # 未受影响的页保持
        self.assertEqual(self.ftl.read(2), slot(payload(2)))

    def test_mixed_mapped_and_unmapped_releases_only_mapped(self):
        released = self.ftl.trim([0, 20, 2, 21, 2])
        self.assertEqual(released, 2)
        self.assertIsNone(self.ftl.read(0))
        self.assertIsNone(self.ftl.read(2))
        self.assertEqual(self.ftl.read(1), slot(payload(1)))


class TestTrimValidation(unittest.TestCase):
    def setUp(self):
        self.dev = RawNand(max_erases=8)
        self.ftl = FTL(self.dev)
        self.ftl.write(0, b"x")

    def test_invalid_lpn_rejects_whole_batch_before_any_action(self):
        n = len(self.dev.action_log())
        seq = self.ftl.commit_seq
        mapping = self.ftl.mapping_snapshot()
        bad_inputs = [
            [0, 24],
            [-1, 0],
            [0, True],
            [False],
            [1.0],
            ["0"],
            [None],
            [0, [1]],
        ]
        for bad in bad_inputs:
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    self.ftl.trim(bad)
        # 任何物理动作之前拒绝：动作日志、序列号、映射全部不变
        self.assertEqual(len(self.dev.action_log()), n)
        self.assertEqual(self.ftl.commit_seq, seq)
        self.assertEqual(self.ftl.mapping_snapshot(), mapping)
        self.assertEqual(self.ftl.read(0), slot(b"x"))

    def test_non_iterable_and_string_rejected(self):
        for bad in (None, 7, "012", b"\x00\x01", {0: 1}):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    self.ftl.trim(bad)

    def test_gc_budget_validation(self):
        for bad in (True, -1, 1.5, "4"):
            with self.assertRaises(InvalidInputError):
                self.ftl.trim([0], gc_budget=bad)

    def test_invalid_among_unmapped_still_rejected(self):
        # 即使其余页号全部已未映射，单个非法页号仍整组拒绝、零物理动作
        n = len(self.dev.action_log())
        seq = self.ftl.commit_seq
        for bad in ([23, 24], [100], [True, 20], [0, -1]):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidInputError):
                    self.ftl.trim(bad)
        self.assertEqual(len(self.dev.action_log()), n)
        self.assertEqual(self.ftl.commit_seq, seq)
        self.assertEqual(self.ftl.read(0), slot(b"x"))


class TestTrimWithBadBlocks(unittest.TestCase):
    def test_trim_works_with_factory_bad_blocks(self):
        dev = RawNand(max_erases=8)
        dev.mark_factory_bad(2)
        dev.mark_factory_bad(9)
        dev.mark_factory_bad(14)
        ftl = FTL(dev)
        shadow = Shadow()
        for i in range(24):
            ftl.write(i, payload(i))
            shadow.write(i, payload(i))
        released = ftl.trim([0, 5, 10, 23])
        self.assertEqual(released, 4)
        shadow.trim([0, 5, 10, 23])
        shadow.check(self, ftl)
        # 任何物理动作都不得触碰坏块
        for a in dev.action_log():
            self.assertNotIn(a.block, (2, 9, 14))
        # 重挂载与独立参考一致
        ftl2 = FTL(dev)
        shadow.check(self, ftl2)
        assert_matches_reference(self, ftl2, dev)
        # 释放后在坏块几何下重写并 GC，活页保持
        ftl2.write(0, b"after-trim")
        shadow.write(0, b"after-trim")
        ftl2.gc_collect()
        shadow.check(self, ftl2)
        for a in dev.action_log():
            self.assertNotIn(a.block, (2, 9, 14))


class TestMixedBlockGC(unittest.TestCase):
    """同一块内混着活页与已释放死页：GC 只搬活页、死页随块擦除。"""

    def test_gc_preserves_live_pages_in_block_with_trimmed_pages(self):
        dev = RawNand(max_erases=8)
        for b in range(10):
            dev.mark_factory_bad(b)  # 仅 6 块可用，放大 GC 压力
        ftl = FTL(dev)
        shadow = Shadow()
        # 全部 24 个逻辑页各写一次（会触发 GC）
        for i in range(24):
            ftl.write(i, payload(i))
            shadow.write(i, payload(i))
        # 释放部分页；找到持有这些死页、同时仍含其他活页的块
        ftl.trim([0, 1, 2, 3])
        shadow.trim([0, 1, 2, 3])
        cap = ftl.capacity()
        self.assertGreaterEqual(cap["reclaimable_dead_pages"], 4)
        # 大量覆盖写其余活页，驱动 GC 擦除含 trim 死页的混块
        for i in range(40):
            lpn = 4 + (i % 20)
            ftl.write(lpn, payload(100 + i))
            shadow.write(lpn, payload(100 + i))
            shadow.check(self, ftl)
        self.assertTrue(any(a.kind == "erase" for a in dev.action_log()))
        # 释放页仍未映射；活页数据完整；独立参考映像一致
        for lpn in (0, 1, 2, 3):
            self.assertIsNone(ftl.read(lpn))
        shadow.check(self, ftl)
        assert_matches_reference(self, ftl, dev)
        # 手动 GC 收敛后再核对
        self.assertGreaterEqual(ftl.gc_collect(), 0)
        shadow.check(self, ftl)
        assert_matches_reference(self, ftl, dev)

    def test_dead_trimmed_pages_reclaimed_by_gc(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        for i in range(10):
            ftl.write(i, payload(i))
        ftl.trim([0, 1, 2, 3, 4])
        dead_before = ftl.capacity()["reclaimable_dead_pages"]
        free_before = ftl.capacity()["free_physical_pages"]
        self.assertGreater(dead_before, 0)
        reclaimed = ftl.gc_collect()
        self.assertGreater(reclaimed, 0)
        cap = ftl.capacity()
        self.assertEqual(cap["reclaimable_dead_pages"], 0)
        self.assertGreater(cap["free_physical_pages"], free_before)
        for lpn in range(10):
            if lpn < 5:
                self.assertIsNone(ftl.read(lpn))
            else:
                self.assertEqual(ftl.read(lpn), slot(payload(lpn)))


class TestTrimBudgetAndSpace(unittest.TestCase):
    def fill_to_boundary(self, dev, ftl, shadow):
        i = 0
        while ftl.capacity()["free_physical_pages"] > 8:
            lpn = i % 24
            ftl.write(lpn, payload(i))
            shadow.write(lpn, payload(i))
            i += 1
            self.assertLess(i, 300)

    def test_zero_budget_gives_budget_exhausted(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev, gc_budget=0)
        shadow = Shadow()
        self.fill_to_boundary(dev, ftl, shadow)
        # 先写几个待释放页（写在到达边界之前）；重排：直接释放已写页
        targets = [lpn for lpn in range(24) if lpn in shadow.d][:4]
        self.assertGreater(len(targets), 0)
        seq = ftl.commit_seq
        mapping = ftl.mapping_snapshot()
        with self.assertRaises(GCBudgetExhaustedError):
            ftl.trim(targets)
        # 预算耗尽不改变已提交映像、不推进序列号
        self.assertEqual(ftl.commit_seq, seq)
        self.assertEqual(ftl.mapping_snapshot(), mapping)
        shadow.check(self, ftl)
        # 放宽预算后同一释放成功：证明是预算问题而非无解
        released = ftl.trim(targets, gc_budget=8)
        self.assertEqual(released, len(targets))
        shadow.trim(targets)
        shadow.check(self, ftl)

    def test_provable_out_of_space_on_worn_device(self):
        dev = RawNand(max_erases=0)  # 禁止任何擦除：死页永不可回收
        ftl = FTL(dev)
        shadow = Shadow()
        self.fill_to_boundary(dev, ftl, shadow)
        targets = sorted(shadow.d)[:4]
        with self.assertRaises(OutOfSpaceError):
            ftl.trim(targets)
        shadow.check(self, ftl)


class TestTrimCrashEnumeration(unittest.TestCase):
    """枚举 trim 及其触发 GC 的每一个物理动作后的掉电点。

    恢复后只允许两种映像：释放前映像，或整组全部释放后的映像。
    """

    def setUp(self):
        # 10 坏块 → 6 块 48 页。先写满 24 个逻辑页，再覆盖写到保留页边界：
        # 此时每个含死页的块都同时持有有效页，下一次 trim 的提交页分配
        # 必然触发带搬迁（gc-copy）的垃圾整理。
        dev = RawNand(max_erases=8)
        for b in range(10):
            dev.mark_factory_bad(b)
        ftl = FTL(dev)
        shadow = Shadow()
        i = 0
        while len(shadow.d) < 24 or ftl.capacity()["free_physical_pages"] > 8:
            lpn = i % 24
            data = payload(i)
            ftl.write(lpn, data)
            shadow.write(lpn, data)
            i += 1
            self.assertLess(i, 200, "应在 200 次写内到达保留页边界")
        self.snap_before = dev.snapshot()
        mapping0 = ftl.mapping_snapshot()
        # 干跑探测：实际受害块的活页 lpn，确保待释放页位于受害块中。
        # 这覆盖关键顺序：目标页先被 gc-copy 搬迁、由 gc-commit 保住，
        # 再由最后一条 trim-commit 撤映射；任何中间断电点都不产生
        # 指向已擦除页的映射。受害块选择只取决于边界时的设备状态
        # （提交页分配先于 trim 更新），故与具体 trim 目标无关。
        n_probe = len(dev.action_log())
        ftl.trim(sorted(shadow.d))
        probe_actions = dev.action_log()[n_probe:]
        erased_blocks = [a.block for a in probe_actions if a.purpose == "gc-erase"]
        self.assertEqual(len(erased_blocks), 1, "干跑应恰好回收一个受害块")
        victim_block = erased_blocks[0]
        victim_lpns = [
            l for l, p in enumerate(mapping0) if p is not None and p // 8 == victim_block
        ]
        self.assertTrue(victim_lpns, "受害块在边界时必须持有活页（带搬迁 GC）")
        victim_lpn = victim_lpns[0]
        # 恢复到边界状态，正式目标 = 受害块中的活页 + 另两个其他块上的活页
        dev.restore(self.snap_before)
        others = [
            l
            for l in shadow.d
            if l != victim_lpn and mapping0[l] // 8 != victim_block
        ]
        self.targets = [victim_lpn] + others[:2]
        self.assertEqual(len(set(self.targets)), 3)
        self.assertTrue(all(t in shadow.d for t in self.targets))
        self.n0 = len(dev.action_log())
        ftl = FTL(dev)
        ftl.trim(self.targets)
        self.n1 = len(dev.action_log())
        actions = dev.action_log()[self.n0 : self.n1]
        self.trim_commit_idx = self.n1 - 1
        self.assertEqual(actions[-1].purpose, "trim-commit")
        kinds = {(a.kind, a.purpose) for a in actions}
        self.assertIn(("program", "gc-copy"), kinds, "trim 应触发带搬迁的 GC")
        self.assertIn(("erase", "gc-erase"), kinds)
        self.assertIn(("program", "trim-commit"), kinds)
        # 正式运行回收的仍是同一受害块：选定的目标活页必然先被搬迁
        real_erased = [a.block for a in actions if a.purpose == "gc-erase"]
        self.assertEqual(real_erased, [victim_block])
        self.dev = dev
        self.shadow = shadow

    def test_every_trim_gc_crash_point(self):
        for k in range(self.n0 + 1, self.n1 + 1):
            with self.subTest(crash_after_action=k):
                self.dev.restore(self.snap_before)
                self.dev.set_crash_plan([k - self.n0])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                with self.assertRaises(PowerLossError):
                    ftl.trim(self.targets)
                self.dev.reboot()
                ftl2 = FTL(self.dev)
                # 期望映像：trim 提交动作完成前 = 释放前；完成后 = 全部释放
                expected = dict(self.shadow.d)
                if self.trim_commit_idx < k:
                    for lpn in self.targets:
                        expected.pop(lpn, None)
                for lpn in range(24):
                    got = ftl2.read(lpn)
                    exp = expected.get(lpn)
                    if exp is None:
                        self.assertIsNone(got, f"k={k} lpn={lpn} 应为未映射")
                    else:
                        self.assertEqual(got, slot(exp), f"k={k} lpn={lpn} 不符")
                assert_matches_reference(self, ftl2, self.dev)
                # 原子性：不存在“释放一半”的映像
                image = parse_committed_image(self.dev)
                mapped_targets = [t for t in self.targets if t in image]
                self.assertIn(
                    len(mapped_targets),
                    (0, len(self.targets)),
                    f"k={k} 出现半释放映像：{mapped_targets}",
                )
                # 恢复后收敛：重写与 GC 均正常（6 块高占用下默认预算 32
                # 可能不足——基线固有特性，故显式放宽本次预算）
                first = self.targets[0]
                ftl2.write(first, b"after-crash")
                self.assertEqual(ftl2.read(first), slot(b"after-crash"))
                for t in self.targets[1:]:
                    if self.trim_commit_idx < k:
                        self.assertIsNone(ftl2.read(t))
                ftl2.gc_collect(gc_budget=64)
                self.assertEqual(ftl2.read(first), slot(b"after-crash"))

class TestTrimCrashNoGC(unittest.TestCase):
    def test_trim_without_gc_crash_point(self):
        # 无 GC 的 trim 只有一个物理动作：枚举“动作前/动作后”两种状态
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = Shadow()
        for i in range(6):
            ftl.write(i, payload(i))
            shadow.write(i, payload(i))
        snap = dev.snapshot()
        n0 = len(dev.action_log())
        targets = [1, 3, 5]

        dev.restore(snap)
        dev.set_crash_plan([1])
        dev.arm_crash()
        ftl = FTL(dev)
        with self.assertRaises(PowerLossError):
            ftl.trim(targets)
        dev.reboot()
        ftl2 = FTL(dev)
        # 唯一动作就是 trim-commit，掉电发生在它完成之后 = 全部释放
        shadow.trim(targets)
        shadow.check(self, ftl2)
        assert_matches_reference(self, ftl2, dev)
        self.assertEqual(len(dev.action_log()), n0 + 1)
        self.assertEqual(dev.action_log()[n0].purpose, "trim-commit")


class TestTrimThenWriteMixedBlockCrash(unittest.TestCase):
    """释放完成后，后续写触发混块 GC：枚举每个断电点，活页不丢。"""

    def test_post_trim_write_gc_crash_points(self):
        dev = RawNand(max_erases=8)
        for b in range(10):
            dev.mark_factory_bad(b)
        ftl = FTL(dev)
        shadow = Shadow()
        for i in range(24):
            ftl.write(i, payload(i))
            shadow.write(i, payload(i))
        # 释放一批页：其数据页与其余活页共存于各块
        targets = list(range(8))
        ftl.trim(targets)
        shadow.trim(targets)
        # 推进到下一次带搬迁 GC 的写
        i = 0
        while True:
            snap = dev.snapshot()
            before = len(dev.action_log())
            lpn = 8 + (i % 16)
            data = payload(500 + i)
            ftl.write(lpn, data)
            after = len(dev.action_log())
            if any(a.purpose == "gc-copy" for a in dev.action_log()[before:after]):
                snap_trigger = snap
                n0, n1 = before, after
                trigger = (lpn, data)
                break
            shadow.write(lpn, data)
            i += 1
            self.assertLess(i, 200)
        for k in range(n0 + 1, n1 + 1):
            with self.subTest(crash_after_action=k):
                dev.restore(snap_trigger)
                dev.set_crash_plan([k - n0])
                dev.arm_crash()
                ftl = FTL(dev)
                with self.assertRaises(PowerLossError):
                    ftl.write(*trigger)
                dev.reboot()
                ftl2 = FTL(dev)
                expected = dict(shadow.d)
                if n1 - 1 < k:  # 最后一个动作是该写的 commit
                    expected[trigger[0]] = trigger[1]
                for t in targets:
                    expected.pop(t, None)  # trim 已提交，必须保持释放
                for lpn in range(24):
                    got = ftl2.read(lpn)
                    exp = expected.get(lpn)
                    if exp is None:
                        self.assertIsNone(got, f"k={k} lpn={lpn} 应未映射")
                    else:
                        self.assertEqual(got, slot(exp), f"k={k} lpn={lpn} 不符")
                assert_matches_reference(self, ftl2, dev)


if __name__ == "__main__":
    unittest.main()
