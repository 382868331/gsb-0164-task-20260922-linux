"""块退役（retire_block）与故障注入的独立对照测试。

真值来源：测试侧自行维护的 shadow 字典（{lpn: 原始写入字节串}），
不调用被测核心得到期望值；tests/refimg.py 的独立解析器作补充对照。
"""
import unittest

from ftl import (
    BLOCKS,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    BadBlockError,
    DataLossError,
    FTL,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
    RawNand,
)
from ftl.errors import EraseFailureError, PageUnreadableError

from refimg import assert_matches_reference, parse_committed_image


def payload(i, size=24):
    return bytes(((i * 31 + j * 7 + 1) % 256 for j in range(size)))


def slot(data):
    return data.ljust(PAGE_SIZE, b"\xff")


def write_batch(ftl, indices):
    """按 indices 写入确定性数据，返回 shadow 真值字典。"""
    shadow = {}
    for i in indices:
        ftl.write(i % 24, payload(i))
        shadow[i % 24] = payload(i)
    return shadow


def assert_shadow(case, ftl, shadow, damaged=()):
    """除 damaged 中的逻辑页外，全部按 shadow 逐页比对；damaged 必须报 DataLossError。"""
    for lpn, data in shadow.items():
        if lpn in damaged:
            with case.assertRaises(DataLossError, msg=f"lpn={lpn} 应报数据丢失"):
                ftl.read(lpn)
        else:
            case.assertEqual(ftl.read(lpn), slot(data), f"lpn={lpn} 数据不符")


def block_of_valid_pages(ftl, shadow):
    """找出持有最多有效页的块号（用公开映射快照推导）。"""
    counts = {}
    for ppn in ftl.mapping_snapshot():
        if ppn is not None:
            counts[ppn // PAGES_PER_BLOCK] = counts.get(ppn // PAGES_PER_BLOCK, 0) + 1
    return max(counts, key=lambda b: (counts[b], -b))


def valid_pages_in(ftl, block):
    """当前映射中落在 block 内的 (lpn, ppn) 列表（公开快照推导）。"""
    return [
        (lpn, ppn)
        for lpn, ppn in enumerate(ftl.mapping_snapshot())
        if ppn is not None and ppn // PAGES_PER_BLOCK == block
    ]


class RetireSuccessTest(unittest.TestCase):
    def test_retire_moves_valid_pages_and_preserves_data(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = write_batch(ftl, range(12))  # 12 个不同逻辑页，填满块 0/1/2
        block = block_of_valid_pages(ftl, shadow)
        expected_moved = dict(valid_pages_in(ftl, block))
        seq_before = ftl.commit_seq

        result = ftl.retire_block(block)

        self.assertEqual(result["block"], block)
        self.assertEqual(result["damaged"], ())
        self.assertEqual(result["moved"].keys(), expected_moved.keys())
        self.assertIn(block, ftl.capacity()["bad_blocks"])
        self.assertGreater(ftl.commit_seq, seq_before)  # 新提交记录已落稳
        self.assertEqual(ftl.damaged_pages(), ())
        assert_shadow(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)

    def test_retired_block_excluded_from_alloc_and_gc(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = write_batch(ftl, range(12))
        block = block_of_valid_pages(ftl, shadow)
        ftl.retire_block(block)

        more = write_batch(ftl, range(12, 60))  # 触发 GC
        shadow.update(more)
        ftl.gc_collect(gc_budget=4 * BLOCKS)  # 放宽预算，确保全部可回收块整理完

        used_blocks = {
            ppn // PAGES_PER_BLOCK
            for ppn in ftl.mapping_snapshot()
            if ppn is not None
        }
        self.assertNotIn(block, used_blocks)  # 不再进入分配
        for action in ftl.physical_log():
            if action.kind == "erase":
                self.assertNotEqual(action.block, block)  # 不再是 GC 候选
        self.assertEqual(ftl.capacity()["bad_blocks"].count(block), 1)
        assert_shadow(self, ftl, shadow)

    def test_retire_block_without_valid_pages(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.retire_block(15)  # 全空白块：无需搬迁、无需提交
        self.assertIn(15, ftl.capacity()["bad_blocks"])
        self.assertEqual(ftl.commit_seq, 0)

    def test_retire_validation(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        for bad in (True, -1, BLOCKS, 1.5, "3"):
            with self.assertRaises(InvalidInputError, msg=f"block={bad!r}"):
                ftl.retire_block(bad)
        ftl.retire_block(3)
        with self.assertRaises(BadBlockError):
            ftl.retire_block(3)  # 已是坏块


class RetireCrashPointTest(unittest.TestCase):
    """逐个物理动作后断电重开：任何断点都不丢已提交数据。"""

    def test_every_power_cut_point(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = write_batch(ftl, range(12))
        block = block_of_valid_pages(ftl, shadow)
        snap = dev.snapshot()

        probe = FTL(dev)
        before = len(dev.action_log())
        probe.retire_block(block)  # 干净跑一遍，数出物理动作总数
        n_actions = len(dev.action_log()) - before
        self.assertGreaterEqual(n_actions, 3)  # 至少 2 次搬迁 + 1 次提交

        for k in range(1, n_actions + 1):
            with self.subTest(crash_after_actions=k):
                dev.restore(snap)
                dev.set_crash_plan([k])
                dev.arm_crash()
                f = FTL(dev)
                with self.assertRaises(PowerLossError):
                    f.retire_block(block)
                dev.reboot()
                f2 = FTL(dev)  # 重开：核对映射、坏块集合与数据
                assert_shadow(self, f2, shadow)
                assert_matches_reference(self, f2, dev)
                # 断电后要么尚未标坏（可重试），要么已完整退役
                f2.retire_block(block)
                self.assertIn(block, f2.capacity()["bad_blocks"])
                assert_shadow(self, f2, shadow)
                assert_matches_reference(self, f2, dev)


class RetireDataLossTest(unittest.TestCase):
    def test_unreadable_page_reported_not_fabricated(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = write_batch(ftl, range(12))
        block = block_of_valid_pages(ftl, shadow)
        # 找到块内一个有效页对应的逻辑页，注入“页不可读”故障
        victim_lpn = next(
            lpn
            for lpn, ppn in enumerate(ftl.mapping_snapshot())
            if ppn is not None and ppn // PAGES_PER_BLOCK == block
        )
        ppn = ftl.mapping_snapshot()[victim_lpn]
        dev.inject_unreadable_page(ppn // PAGES_PER_BLOCK, ppn % PAGES_PER_BLOCK)

        result = ftl.retire_block(block)

        self.assertEqual(result["damaged"], (victim_lpn,))
        self.assertEqual(ftl.damaged_pages(), (victim_lpn,))
        self.assertIn(block, ftl.capacity()["bad_blocks"])
        assert_shadow(self, ftl, shadow, damaged={victim_lpn})

        # 断电重开后受损逻辑页依然明确报告，绝不恢复出伪造内容
        dev.reboot()
        f2 = FTL(dev)
        self.assertEqual(f2.damaged_pages(), (victim_lpn,))
        with self.assertRaises(DataLossError):
            f2.read(victim_lpn)
        image = parse_committed_image(dev)
        self.assertIsNone(image[victim_lpn])  # 独立参考同样判定不可恢复
        assert_shadow(self, f2, shadow, damaged={victim_lpn})

    def test_unreadable_fault_is_permanent_and_validated(self):
        dev = RawNand(max_erases=8)
        FTL(dev)
        dev.inject_unreadable_page(0, 0)
        dev.reboot()  # 故障为硬件属性，重启不消失
        with self.assertRaises(PageUnreadableError):
            dev.read_page(0, 0)
        with self.assertRaises(InvalidInputError):
            dev.inject_unreadable_page(True, 0)
        with self.assertRaises(InvalidInputError):
            dev.inject_unreadable_page(0, 8)


class RetireSpaceTest(unittest.TestCase):
    def test_insufficient_space_keeps_recoverable_state(self):
        dev = RawNand(max_erases=0)  # 禁止一切擦除：死页永不可回收
        ftl = FTL(dev)
        shadow = write_batch(ftl, range(60))  # 用掉 120 页，仅剩 8 页保留余量
        with self.assertRaises(OutOfSpaceError):
            ftl.write(0, payload(999))  # 用户写已触顶（保留页不可耗尽）

        failing = None
        for block in range(BLOCKS):  # 逐个退役，余量终将被耗尽
            if ftl.device.is_bad_block(block):
                continue
            try:
                ftl.retire_block(block)
            except OutOfSpaceError:
                failing = block
                break
        self.assertIsNotNone(failing, "余量有限，退役终将因空间不足被拒绝")

        # 原状态可恢复：块未标坏、提交序列号不变、数据全部可读
        seq_before = ftl.commit_seq
        with self.assertRaises(OutOfSpaceError):
            ftl.retire_block(failing)  # 重试同样失败，且仍不产生部分迁移假象
        self.assertNotIn(failing, ftl.capacity()["bad_blocks"])
        self.assertEqual(ftl.commit_seq, seq_before)
        assert_shadow(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)


class EraseFailureFaultTest(unittest.TestCase):
    def test_device_rejects_erase_after_injection(self):
        dev = RawNand(max_erases=8)
        dev.inject_erase_failure(2)
        with self.assertRaises(EraseFailureError):
            dev.erase(2)
        dev.reboot()  # 故障永久有效
        with self.assertRaises(EraseFailureError):
            dev.erase(2)
        self.assertTrue(dev.is_erase_failing(2))
        with self.assertRaises(InvalidInputError):
            dev.inject_erase_failure(2.5)

    def test_gc_auto_retires_erase_failing_block(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        shadow = {}
        for i in range(8):  # 8 次覆盖写同一逻辑页：块 0 全为死页
            ftl.write(0, payload(i))
            shadow[0] = payload(i)
        # 块 0 持有 8 个死页（7 个被覆盖的数据页 + 7 条过期提交记录），
        # 是 GC 受害块的首选；注入“不可再擦写”故障。
        dev.inject_erase_failure(0)

        ftl.gc_collect()

        self.assertIn(0, ftl.capacity()["bad_blocks"])  # 擦写失败 → 永久退役
        self.assertEqual(ftl.read(0), slot(shadow[0]))  # 有效页完好
        assert_matches_reference(self, ftl, dev)
        self.assertFalse(
            any(a.kind == "erase" and a.block == 0 for a in ftl.physical_log()),
            "坏块不得再被擦除",
        )


if __name__ == "__main__":
    unittest.main()
