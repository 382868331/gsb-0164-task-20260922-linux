"""块退役（擦写失败后永久停用）与页不可读故障的独立对照用例。

测试侧用独立影子字典保存逻辑真值（expected_image），不调用 FTL 的
映射算法；期望映像仅由注入的故障与退役结果自行推导。
"""
import unittest

from ftl import (
    FTL,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    EraseFailureError,
    GCBudgetExhaustedError,
    LogicalDataLossError,
    OutOfSpaceError,
    PowerLossError,
    RawNand,
    UnreadablePageError,
)


def payload(i, size=12):
    return bytes(((i * 37 + j * 13 + 5) % 256 for j in range(size)))


def slot(data):
    return data.ljust(PAGE_SIZE, b"\xff")


def assert_truth(case, ftl, image, lost):
    """独立逻辑真值对照：image={lpn: bytes}，lost=不可读逻辑页集合。"""
    for lpn in range(24):
        if lpn in lost:
            with case.assertRaises(LogicalDataLossError):
                ftl.read(lpn)
        elif lpn in image:
            case.assertEqual(ftl.read(lpn), slot(image[lpn]))
        else:
            case.assertIsNone(ftl.read(lpn))


def build_filled_device(num_good_blocks=6, max_erases=8, writes=30):
    """用少量好块填充数据，返回 (dev, ftl, image)；其余块登记为工厂坏块。"""
    dev = RawNand(max_erases=max_erases)
    for b in range(num_good_blocks, 16):
        dev.mark_factory_bad(b)
    ftl = FTL(dev)
    image = {}
    for i in range(writes):
        lpn = i % 24
        data = payload(i)
        ftl.write(lpn, data)
        image[lpn] = data
    return dev, ftl, image


class TestRetireSuccess(unittest.TestCase):
    def test_retire_moves_valid_pages_and_bans_block(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        image = {}
        for i in range(40):
            lpn = i % 24
            ftl.write(lpn, payload(i))
            image[lpn] = payload(i)
        # 选一个持有有效数据的好块作为“擦写失败”块
        mapping = ftl.mapping_snapshot()
        victim = next(
            b
            for b in range(16)
            if not dev.is_bad_block(b)
            and any(
                p is not None and p // PAGES_PER_BLOCK == b for p in mapping
            )
        )
        lpns_before = [
            l for l, p in enumerate(mapping)
            if p is not None and p // PAGES_PER_BLOCK == victim
        ]
        self.assertTrue(lpns_before)
        dev.inject_block_erase_failure(victim)
        with self.assertRaises(EraseFailureError):
            dev.erase(victim)
        with self.assertRaises(EraseFailureError):
            dev.program(victim, 7, b"x")
        # 注入后块仍可读
        for p in range(PAGES_PER_BLOCK):
            dev.read_page(victim, p)

        report = ftl.retire_block(victim)
        self.assertEqual(report["retired_block"], victim)
        self.assertEqual(report["damaged_lpns"], ())
        moved_lpns = {e[0] for e in report["migrated"]}
        self.assertEqual(moved_lpns, set(lpns_before))
        for _, old_ppn, new_ppn in report["migrated"]:
            self.assertEqual(old_ppn // PAGES_PER_BLOCK, victim)
            self.assertNotEqual(new_ppn // PAGES_PER_BLOCK, victim)

        self.assertTrue(dev.is_bad_block(victim))
        self.assertIn(victim, ftl.capacity()["bad_blocks"])
        # 坏块今后不再进入分配和 GC 候选：再写一批，动作日志不碰它
        n0 = len(dev.action_log())
        for i in range(40, 70):
            ftl.write(i % 24, payload(i))
            image[i % 24] = payload(i)
        self.assertTrue(
            all(a.block != victim for a in dev.action_log()[n0:])
        )
        assert_truth(self, ftl, image, set())
        # 幂等：对已退役块再调一次，映像不变
        seq = ftl.commit_seq
        report2 = ftl.retire_block(victim)
        self.assertEqual(report2["migrated"], ())
        self.assertEqual(ftl.commit_seq, seq)
        assert_truth(self, ftl, image, set())

    def test_pending_retire_finishes_after_reboot(self):
        # 提交已落稳、块尚未正式退役时断电：重开后自动完成退役
        dev, ftl, image = build_filled_device()
        mapping = ftl.mapping_snapshot()
        victim = next(
            p // PAGES_PER_BLOCK
            for p in mapping
            if p is not None and not dev.is_bad_block(p // PAGES_PER_BLOCK)
        )
        lpns = [
            l for l, p in enumerate(mapping)
            if p is not None and p // PAGES_PER_BLOCK == victim
        ]
        dev.inject_block_erase_failure(victim)
        snap = dev.snapshot()
        report = ftl.retire_block(victim)
        # 退役动作序列：retire-copy* + retire-commit（块退役是管理动作）。
        # 在最后一个物理动作（commit）后断电：此时重启应自动补做退役。
        n0 = len(snap["log"])
        actions = dev.action_log()[n0:]
        self.assertEqual(actions[-1].purpose, "retire-commit")
        dev.restore(snap)
        dev.set_crash_plan([len(actions)])
        dev.arm_crash()
        ftl = FTL(dev)
        with self.assertRaises(PowerLossError):
            ftl.retire_block(victim)
        dev.reboot()
        ftl2 = FTL(dev)
        self.assertTrue(dev.is_bad_block(victim), "重启挂载后应补完成块退役")
        assert_truth(self, ftl2, image, set())
        # 所有搬出页确实不在坏块上
        for lpn in lpns:
            ppn = ftl2.mapping_snapshot()[lpn]
            self.assertNotEqual(ppn // PAGES_PER_BLOCK, victim)


class TestRetireCrashPoints(unittest.TestCase):
    def test_every_physical_action_crash_point(self):
        # 逐个物理动作后断电重开，核对映射、坏块集合与数据；最终收敛
        dev0 = RawNand(max_erases=8)
        ftl0 = FTL(dev0)
        image0 = {}
        for i in range(40):
            lpn = i % 24
            ftl0.write(lpn, payload(i))
            image0[lpn] = payload(i)
        victim = next(
            p // PAGES_PER_BLOCK for p in ftl0.mapping_snapshot() if p is not None
        )
        lpns = [
            l for l, p in enumerate(ftl0.mapping_snapshot())
            if p is not None and p // PAGES_PER_BLOCK == victim
        ]
        snap0 = dev0.snapshot()
        n0 = len(snap0["log"])
        dev0.inject_block_erase_failure(victim)
        ftl0.retire_block(victim)  # 仅用于得知动作总数
        n1 = len(dev0.action_log())
        total = n1 - n0
        self.assertGreaterEqual(total, len(lpns) + 1)
        for k in range(1, total + 1):
            with self.subTest(crash_after=k):
                dev0.restore(snap0)
                dev0.inject_block_erase_failure(victim)
                dev0.set_crash_plan([k])
                dev0.arm_crash()
                ftl = FTL(dev0)
                with self.assertRaises(PowerLossError):
                    ftl.retire_block(victim)
                dev0.reboot()
                ftl2 = FTL(dev0)
                if k < total:
                    # 提交未完成：旧映射继续有效，块尚未退役
                    self.assertNotIn(victim, dev0.bad_blocks())
                    for lpn in lpns:
                        self.assertEqual(
                            ftl2.mapping_snapshot()[lpn] // PAGES_PER_BLOCK,
                            victim,
                        )
                else:
                    # 最后动作后断电：挂载续做，块已退役、映射搬出
                    self.assertIn(victim, dev0.bad_blocks())
                    for lpn in lpns:
                        self.assertNotEqual(
                            ftl2.mapping_snapshot()[lpn] // PAGES_PER_BLOCK,
                            victim,
                        )
                assert_truth(self, ftl2, image0, set())
                # 无论断在何处，再次执行退役后系统收敛且映像不变
                ftl2.retire_block(victim)
                self.assertTrue(dev0.is_bad_block(victim))
                assert_truth(self, ftl2, image0, set())


class TestUnreadablePage(unittest.TestCase):
    def test_damaged_page_reported_and_never_fabricated(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        image = {}
        for i in range(10):
            ftl.write(i, payload(i))
            image[i] = payload(i)
        # 找到 lpn 5 当前所在物理页，直接注入不可读
        ppn = ftl.mapping_snapshot()[5]
        block, page = divmod(ppn, PAGES_PER_BLOCK)
        dev.inject_unreadable_page(block, page)
        with self.assertRaises(UnreadablePageError):
            dev.read_page(block, page)
        with self.assertRaises(UnreadablePageError):
            ftl.read(5)
        # 其余逻辑页不受影响
        for lpn in range(10):
            if lpn != 5:
                self.assertEqual(ftl.read(lpn), slot(image[lpn]))

        dev.inject_block_erase_failure(block)
        report = ftl.retire_block(block)
        self.assertEqual(report["damaged_lpns"], (5,))
        self.assertEqual(ftl.mapping_snapshot()[5], None)
        self.assertIn(5, dev.lost_logical_pages())
        self.assertTrue(dev.is_bad_block(block))
        # 明确阻止读取伪造内容：重启后仍然报错
        dev.reboot()
        ftl2 = FTL(dev)
        with self.assertRaises(LogicalDataLossError):
            ftl2.read(5)
        image.pop(5)
        for lpn, data in image.items():
            self.assertEqual(ftl2.read(lpn), slot(data))
        # 重新写入后损失解除
        ftl2.write(5, b"recovered-by-host")
        self.assertEqual(
            ftl2.read(5), slot(b"recovered-by-host")
        )
        self.assertNotIn(5, dev.lost_logical_pages())

    def test_unreadable_page_not_allocated_as_blank(self):
        dev = RawNand(max_erases=8)
        dev.program(0, 3, b"x")
        dev.inject_unreadable_page(0, 3)
        ftl = FTL(dev)
        ftl.write(0, b"a")  # 分配器必须跳过该页
        used = {
            a.page
            for a in dev.action_log()
            if a.block == 0 and a.purpose != ""
        }
        self.assertNotIn(3, used)


class TestRetireSpaceShortage(unittest.TestCase):
    def test_shortage_keeps_recoverable_original_state(self):
        # 4 个好块写满到 GC 保留边界，再占用 7 页保留余量：
        # 含 6 个有效页的故障块此时无法完整搬迁（预算 0）。
        dev = RawNand(max_erases=8)
        for b in range(4, 16):
            dev.mark_factory_bad(b)
        ftl = FTL(dev)
        image = {}
        i = 0
        while ftl.capacity()["free_physical_pages"] > 8:
            lpn = i % 24
            ftl.write(lpn, payload(i))
            image[lpn] = payload(i)
            i += 1
            self.assertLess(i, 200)
        victim = next(
            p // PAGES_PER_BLOCK for p in ftl.mapping_snapshot() if p is not None
        )
        lpns_in_victim = {
            l for l, p in enumerate(ftl.mapping_snapshot())
            if p is not None and p // PAGES_PER_BLOCK == victim
        }
        # 占用其他好块上的 7 个空白页（设备层直接编程，对 FTL 是孤儿页）
        taken = 0
        for b in range(4):
            if b == victim:
                continue
            for p in range(PAGES_PER_BLOCK):
                if dev.is_blank(b, p):
                    dev.program(b, p, b"\x02" * 4)
                    taken += 1
                if taken >= 7:
                    break
            if taken >= 7:
                break
        dev.inject_block_erase_failure(victim)
        seq_before = ftl.commit_seq
        mapping_before = ftl.mapping_snapshot()
        self.assertGreaterEqual(len(lpns_in_victim), 2)
        with self.assertRaises(GCBudgetExhaustedError):
            ftl.retire_block(victim, gc_budget=0)
        # 原状态保留：块未标成已迁完/未退役、映射不变、数据仍可读
        self.assertFalse(dev.is_bad_block(victim))
        self.assertTrue(dev.is_no_erase_block(victim))
        self.assertEqual(ftl.commit_seq, seq_before)
        self.assertEqual(ftl.mapping_snapshot(), mapping_before)
        self.assertEqual(dev.lost_logical_pages(), ())
        assert_truth(self, ftl, image, set())
        # 放宽预算后同一退役成功：GC 级联腾出空间，全部有效页搬出
        report = ftl.retire_block(victim, gc_budget=8)
        self.assertTrue(dev.is_bad_block(victim))
        self.assertEqual(
            {e[0] for e in report["migrated"]}, lpns_in_victim
        )
        assert_truth(self, ftl, image, set())


if __name__ == "__main__":
    unittest.main()
