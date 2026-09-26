"""扩展 Hamming SECDED 与待清洗队列测试。

- 字级枚举：对若干小数据字枚举全部单错（13 个位置）与双错（78 对位置），
  分类期望值由测试内独立复现的校验方程给出（不调用 ftl.ecc 的判定逻辑）。
- 页级行为：纠正读入队、去重、清洗搬迁、断电恢复、不可纠正明确损坏、
  校验正确但映射世代过期的页不得被救活、旧无故障写读一致。
"""
import unittest

from ftl import FTL, PAGE_SIZE, RawNand
from ftl import ecc
from ftl.errors import InvalidInputError, PowerLossError

# ---- 独立参考：测试内自行复现校验方程（第二份实现，不调用被测核心） ----
REF_DATA_POS = (3, 5, 6, 7, 9, 10, 11, 12)
REF_PARITY_POS = (1, 2, 4, 8)
REF_OVERALL_POS = 13


def ref_encode(word):
    cw = 0
    for i, pos in enumerate(REF_DATA_POS):
        if (word >> i) & 1:
            cw |= 1 << (pos - 1)
    for p in REF_PARITY_POS:
        par = 0
        for pos in range(1, 13):
            if pos & p:
                par ^= (cw >> (pos - 1)) & 1
        cw |= par << (p - 1)
    overall = 0
    for pos in range(1, 13):
        overall ^= (cw >> (pos - 1)) & 1
    cw |= overall << (REF_OVERALL_POS - 1)
    return cw


def ref_classify(cw):
    s = 0
    for pos in range(1, 13):
        if (cw >> (pos - 1)) & 1:
            s ^= pos
    p = 0
    for pos in range(1, 14):
        p ^= (cw >> (pos - 1)) & 1
    if p == 0 and s == 0:
        return "clean"
    if p == 1:
        return "corrected"
    return "uncorrectable"


def payload(i, size=16):
    return bytes(((i * 31 + j * 7 + 1) % 256 for j in range(size)))


class TestWordEnumeration(unittest.TestCase):
    """小数据字：枚举全部单错/双错位置，独立按校验方程核对分类。"""

    def test_all_single_and_double_flips(self):
        for word in (0x00, 0x01, 0x5A, 0xA5, 0xFF):
            cw = ecc.encode_word(word)
            self.assertEqual(cw, ref_encode(word), "编码与独立参考不一致")
            self.assertEqual(ecc.decode_word(cw), ("clean", word))
            # 全部 13 个单比特位置（含校验位与整体校验位）
            for i in range(13):
                bad = cw ^ (1 << i)
                self.assertEqual(ref_classify(bad), "corrected")
                status, got = ecc.decode_word(bad)
                self.assertEqual(status, "corrected", f"word={word:#x} bit={i}")
                self.assertEqual(got, word, f"word={word:#x} bit={i} 纠正结果错误")
            # 全部 78 对双比特位置：必须判为不可纠正，不得改成合法数据
            for i in range(13):
                for j in range(i + 1, 13):
                    bad = cw ^ (1 << i) ^ (1 << j)
                    self.assertEqual(ref_classify(bad), "uncorrectable")
                    status, got = ecc.decode_word(bad)
                    self.assertEqual(status, "uncorrectable",
                                     f"word={word:#x} bits=({i},{j})")
                    self.assertIsNone(got, "双错不得被改成看似合法的数据")

    def test_parity_bit_flip_is_corrected(self):
        # 校验位（位 1/2/4/8）与整体校验位（位 13）自身翻转也要识别
        cw = ecc.encode_word(0x3C)
        for pos in (1, 2, 4, 8, 13):
            status, got = ecc.decode_word(cw ^ (1 << (pos - 1)))
            self.assertEqual((status, got), ("corrected", 0x3C))

    def test_reserved_bits_rejected(self):
        with self.assertRaises(InvalidInputError):
            ecc.decode_word(1 << 13)  # 高 3 位保留位必须为零


class TestEccPageReadWrite(unittest.TestCase):
    def setUp(self):
        self.dev = RawNand(max_erases=8)
        self.ftl = FTL(self.dev, ecc=True)

    def _ppn(self, lpn):
        return self.ftl.mapping_snapshot()[lpn]

    def test_clean_roundtrip_matches_old_semantics(self):
        # 旧无故障写读结果一致（ECC 模式槽长 16 字节，0xFF 填充）
        data = payload(1, size=10)
        self.ftl.write(3, data)
        res = self.ftl.read(3)
        self.assertEqual(res.status, "clean")
        self.assertEqual(res.data, data.ljust(16, b"\xff"))
        self.assertEqual(self.ftl.scrub_queue(), ())
        self.assertIsNone(self.ftl.read(4))

    def test_single_bit_flip_corrected_and_queued(self):
        data = payload(2)
        self.ftl.write(5, data)
        ppn = self._ppn(5)
        self.dev.inject_bit_flip(ppn // 8, ppn % 8, 3)  # 第 0 码字某数据位
        res = self.ftl.read(5)
        self.assertEqual(res.status, "corrected")
        self.assertEqual(res.data, data)
        self.assertEqual(self.ftl.scrub_queue(), (ppn,))
        # 重复读同一待清洗页不重复排队
        self.ftl.read(5)
        self.ftl.read(5)
        self.assertEqual(self.ftl.scrub_queue(), (ppn,))

    def test_parity_bit_flip_on_page_corrected(self):
        data = payload(3)
        self.ftl.write(6, data)
        ppn = self._ppn(6)
        # 第 1 个码字的整体校验位：码字位 13 -> 页位 2*8 + 12 = 28
        self.dev.inject_bit_flip(ppn // 8, ppn % 8, 28)
        res = self.ftl.read(6)
        self.assertEqual((res.status, res.data), ("corrected", data))

    def test_double_bit_flip_uncorrectable(self):
        self.ftl.write(7, payload(4))
        ppn = self._ppn(7)
        self.dev.inject_bit_flip(ppn // 8, ppn % 8, 0)
        self.dev.inject_bit_flip(ppn // 8, ppn % 8, 1)
        res = self.ftl.read(7)
        self.assertEqual(res.status, "uncorrectable")
        self.assertIsNone(res.data)  # 明确损坏状态，不给看似合法的数据
        self.assertEqual(self.ftl.scrub_queue(), ())  # 不可纠正不入清洗队列

    def test_reserved_bit_flip_uncorrectable(self):
        self.ftl.write(8, payload(5))
        ppn = self._ppn(8)
        self.dev.inject_bit_flip(ppn // 8, ppn % 8, 13)  # 码字高 3 位保留位
        self.assertEqual(self.ftl.read(8).status, "uncorrectable")

    def test_scrub_relocates_via_safe_commit(self):
        data = payload(6)
        self.ftl.write(9, data)
        old_ppn = self._ppn(9)
        self.dev.inject_bit_flip(old_ppn // 8, old_ppn % 8, 5)
        self.ftl.read(9)
        result = self.ftl.scrub()
        self.assertEqual(result, {"scrubbed": 1, "unmapped": 0, "uncorrectable": 0})
        self.assertEqual(self.ftl.scrub_queue(), ())
        new_ppn = self._ppn(9)
        self.assertNotEqual(new_ppn, old_ppn)  # 写入新页
        res = self.ftl.read(9)
        self.assertEqual((res.status, res.data), ("clean", data))
        purposes = [a.purpose for a in self.ftl.physical_log()]
        self.assertIn("scrub-copy", purposes)
        self.assertIn("scrub-commit", purposes)

    def test_scrub_crash_mapping_recovers(self):
        data = payload(7)
        self.ftl.write(10, data)
        ppn = self._ppn(10)
        self.dev.inject_bit_flip(ppn // 8, ppn % 8, 2)
        self.ftl.read(10)
        # 清洗的第 1 个物理动作（scrub-copy）完成后掉电：提交尚未发生
        self.dev.set_crash_plan([1])
        self.dev.arm_crash()
        with self.assertRaises(PowerLossError):
            self.ftl.scrub()
        self.dev.reboot()
        ftl2 = FTL(self.dev, ecc=True)  # 重启后映射仍能恢复
        self.assertEqual(ftl2.mapping_snapshot()[10], ppn)
        res = ftl2.read(10)  # 旧页仍可纠正读出，并重新入队
        self.assertEqual((res.status, res.data), ("corrected", data))
        self.assertEqual(ftl2.scrub_queue(), (ppn,))
        result = ftl2.scrub()  # 再次清洗成功
        self.assertEqual(result["scrubbed"], 1)
        self.assertEqual(ftl2.read(10).status, "clean")

    def test_stale_generation_page_not_revived(self):
        # 校验正确但映射世代过期的页不能被救活
        self.ftl.write(11, payload(8))
        stale_ppn = self._ppn(11)
        stale_raw = self.dev.read_page(stale_ppn // 8, stale_ppn % 8)
        self.assertEqual(ecc.decode_page(stale_raw)[0], "clean")  # 旧页校验正确
        self.ftl.write(11, payload(9))  # 覆盖写：旧页成为世代过期死页
        cur_ppn = self._ppn(11)
        self.assertNotEqual(cur_ppn, stale_ppn)
        # 当前页双错不可纠正：不得回退到世代过期的旧页
        self.dev.inject_bit_flip(cur_ppn // 8, cur_ppn % 8, 0)
        self.dev.inject_bit_flip(cur_ppn // 8, cur_ppn % 8, 1)
        self.assertEqual(self.ftl.read(11).status, "uncorrectable")
        # 重启恢复后依然如此：旧提交记录/旧数据页均不得生效
        self.dev.reboot()
        ftl2 = FTL(self.dev, ecc=True)
        self.assertEqual(ftl2.mapping_snapshot()[11], cur_ppn)
        res = ftl2.read(11)
        self.assertEqual(res.status, "uncorrectable")
        self.assertIsNone(res.data)

    def test_validation(self):
        with self.assertRaises(InvalidInputError):
            self.ftl.write(0, b"x" * 17)  # ECC 模式数据上限 16 字节
        with self.assertRaises(InvalidInputError):
            FTL(RawNand(max_erases=1), ecc=1)  # ecc 必须是 bool
        with self.assertRaises(InvalidInputError):
            self.dev.inject_bit_flip(0, 0, 256)  # 位号越界


if __name__ == "__main__":
    unittest.main()

