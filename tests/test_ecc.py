"""扩展 Hamming SECDED：独立校验方程对照、页级纠正/清洗、世代过期不可救活。

独立参考：ref_encode / ref_classify 在本文件内直接按校验方程计算，
不调用 ftl.ecc 的任何函数得到期望值。
"""
import itertools
import unittest

from ftl import FTL, PAGE_SIZE, PowerLossError, RawNand, UncorrectableError
from ftl.ecc import decode_page, decode_word, encode_page, encode_word


# ----------------------------------------------------------------------
# 测试侧独立参考：直接按校验方程构造码字并分类
# ----------------------------------------------------------------------
def ref_encode_bits(nibble):
    """返回 {位置1..8: 比特}。位置 1/2/4 校验位，3/5/6/7 数据位，8 全局校验。"""
    bits = {3: (nibble >> 0) & 1, 5: (nibble >> 1) & 1,
            6: (nibble >> 2) & 1, 7: (nibble >> 3) & 1}
    bits[1] = bits[3] ^ bits[5] ^ bits[7]
    bits[2] = bits[3] ^ bits[6] ^ bits[7]
    bits[4] = bits[5] ^ bits[6] ^ bits[7]
    bits[8] = bits[1] ^ bits[2] ^ bits[3] ^ bits[4] ^ bits[5] ^ bits[6] ^ bits[7]
    return bits


def ref_classify(nibble, flips):
    """按校验方程独立分类：返回 ("clean"/"corrected"/"uncorrectable", 数据字)。"""
    bits = ref_encode_bits(nibble)
    for pos in flips:
        bits[pos] ^= 1
    syndrome = (
        (bits[1] ^ bits[3] ^ bits[5] ^ bits[7])
        | ((bits[2] ^ bits[3] ^ bits[6] ^ bits[7]) << 1)
        | ((bits[4] ^ bits[5] ^ bits[6] ^ bits[7]) << 2)
    )
    overall = 0
    for pos in range(1, 9):
        overall ^= bits[pos]
    if syndrome == 0 and overall == 0:
        return "clean", nibble
    if overall == 1:
        if syndrome:
            bits[syndrome] ^= 1
        recovered = bits[3] | (bits[5] << 1) | (bits[6] << 2) | (bits[7] << 3)
        return "corrected", recovered
    return "uncorrectable", None


def flip_codeword(codeword, flips):
    for pos in flips:
        codeword ^= 1 << (pos - 1)  # 位置 p 对应字节内 bit (p-1)
    return codeword


class TestWordExhaustive(unittest.TestCase):
    """对一个小数据字枚举全部单错与双错位置，独立按校验方程核对分类。"""

    def test_all_nibbles_all_single_and_double_flips(self):
        positions = range(1, 9)
        cases = 0
        for nibble in range(16):
            flip_sets = [()] + [(p,) for p in positions] + list(
                itertools.combinations(positions, 2)
            )
            for flips in flip_sets:
                expected = ref_classify(nibble, flips)
                got = decode_word(flip_codeword(encode_word(nibble), flips))
                self.assertEqual(
                    got, expected, f"nibble={nibble:#x} flips={flips}"
                )
                cases += 1
        self.assertEqual(cases, 16 * (1 + 8 + 28))

    def test_double_error_never_misdecoded_as_valid(self):
        # 双错必须全部判为 uncorrectable，绝不产出看似合法的数据字
        for nibble in range(16):
            for flips in itertools.combinations(range(1, 9), 2):
                status, value = decode_word(
                    flip_codeword(encode_word(nibble), flips)
                )
                self.assertEqual(status, "uncorrectable")
                self.assertIsNone(value)

    def test_parity_bit_only_flip_corrected_with_data_unchanged(self):
        # 校验位（位置 1/2/4/8）自身翻转：判为 corrected 且数据字不变
        for nibble in range(16):
            for pos in (1, 2, 4, 8):
                status, value = decode_word(
                    flip_codeword(encode_word(nibble), (pos,))
                )
                self.assertEqual((status, value), ("corrected", nibble))


def payload(i, size=10):
    return bytes(((i * 13 + j * 7) % 256 for j in range(size)))


def locate(ftl, lpn):
    ppn = ftl.mapping_snapshot()[lpn]
    assert ppn is not None
    return ppn // 8, ppn % 8


class TestPageLevelECC(unittest.TestCase):
    def setUp(self):
        self.dev = RawNand(max_erases=8)
        self.ftl = FTL(self.dev)

    def test_clean_read_never_enqueues(self):
        self.ftl.write(0, payload(1))
        self.assertEqual(self.ftl.read_status(0), "ok")
        self.assertEqual(self.ftl.read(0), payload(1).ljust(PAGE_SIZE, b"\xff"))
        self.assertEqual(self.ftl.scrub_pending(), ())

    def test_single_data_bit_flip_corrected_and_scrubbed(self):
        data = payload(2)
        self.ftl.write(1, data)
        b, p = locate(self.ftl, 1)
        self.dev.inject_bit_flip(b, p, 5, area="data")  # 数据区单比特翻转
        self.assertEqual(self.ftl.read_status(1), "corrected")
        self.assertEqual(self.ftl.read(1), data.ljust(PAGE_SIZE, b"\xff"))
        # 重复读同一待清洗页不重复排队
        self.ftl.read(1)
        self.ftl.read(1)
        self.assertEqual(self.ftl.scrub_pending(), (1,))
        # 清洗：通过原安全搬迁写入新页
        self.assertEqual(self.ftl.scrub(), 1)
        self.assertEqual(self.ftl.scrub_pending(), ())
        self.assertEqual(self.ftl.read_status(1), "ok")
        self.assertEqual(self.ftl.read(1), data.ljust(PAGE_SIZE, b"\xff"))
        # 重新挂载后数据仍在
        ftl2 = FTL(self.dev)
        self.assertEqual(ftl2.read(1), data.ljust(PAGE_SIZE, b"\xff"))

    def test_parity_area_flip_corrected_data_unchanged(self):
        data = payload(3)
        self.ftl.write(2, data)
        b, p = locate(self.ftl, 2)
        self.dev.inject_bit_flip(b, p, 3, area="spare")  # 校验区单比特翻转
        self.assertEqual(self.ftl.read_status(2), "corrected")
        self.assertEqual(self.ftl.read(2), data.ljust(PAGE_SIZE, b"\xff"))
        self.assertEqual(self.ftl.scrub_pending(), (2,))

    def test_double_bit_flip_reports_corrupted(self):
        self.ftl.write(3, payload(4))
        b, p = locate(self.ftl, 3)
        self.dev.inject_bit_flip(b, p, 0, area="data")
        self.dev.inject_bit_flip(b, p, 1, area="data")  # 同一数据字双比特错
        self.assertEqual(self.ftl.read_status(3), "corrupted")
        with self.assertRaises(UncorrectableError):
            self.ftl.read(3)
        self.assertEqual(self.ftl.scrub_pending(), ())  # 不可纠正页不入清洗队列

    def test_scrub_survives_power_loss_at_every_action(self):
        data = payload(5)
        self.ftl.write(4, data)
        b, p = locate(self.ftl, 4)
        self.dev.inject_bit_flip(b, p, 9, area="data")
        self.assertEqual(self.ftl.read(4), data.ljust(PAGE_SIZE, b"\xff"))
        self.assertEqual(self.ftl.scrub_pending(), (4,))
        snap = self.dev.snapshot()
        # 清洗写 = 2 个物理动作（数据页 + 提交记录），逐断电点枚举
        for k in (1, 2):
            with self.subTest(crash_after_action=k):
                self.dev.restore(snap)
                self.dev.set_crash_plan([k])
                self.dev.arm_crash()
                ftl = FTL(self.dev)
                ftl.read(4)  # 重新入队（restore 后内存队列为空）
                with self.assertRaises(PowerLossError):
                    ftl.scrub()
                self.dev.reboot()
                ftl2 = FTL(self.dev)
                # 映射仍可恢复，数据读回一致（旧页可纠正或新页已提交）
                self.assertEqual(
                    ftl2.read(4), data.ljust(PAGE_SIZE, b"\xff")
                )
                ftl2.scrub()  # 收尾清洗，确认可收敛
                self.assertEqual(ftl2.read_status(4), "ok")


class TestStaleGenerationNotRevived(unittest.TestCase):
    """校验正确但映射世代过期的页不能被救活。"""

    def test_stale_page_with_valid_ecc_not_revived(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.write(0, b"old-version")
        stale_block, stale_page = locate(ftl, 0)
        ftl.write(0, b"new-version")
        # 过期页 ECC 仍然有效（独立页级译码确认），但世代已过期
        status, _ = decode_page(
            dev.read_page(stale_block, stale_page),
            dev.read_spare(stale_block, stale_page),
        )
        self.assertEqual(status, "clean")
        # 重新挂载：映射仲裁只认最新提交记录，过期页不得复活
        ftl2 = FTL(dev)
        self.assertEqual(ftl2.read(0), b"new-version".ljust(PAGE_SIZE, b"\xff"))

    def test_uncorrectable_current_page_no_fallback_to_stale(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.write(0, b"old-version")
        ftl.write(0, b"new-version")
        b, p = locate(ftl, 0)  # 当前世代页
        dev.inject_bit_flip(b, p, 0, area="data")
        dev.inject_bit_flip(b, p, 1, area="data")  # 双比特错 → 不可纠正
        # 必须明确报告损坏，不得回退到世代过期的旧页数据
        self.assertEqual(ftl.read_status(0), "corrupted")
        with self.assertRaises(UncorrectableError):
            ftl.read(0)
        ftl2 = FTL(dev)  # 重新挂载后同样不得救活旧世代
        self.assertEqual(ftl2.read_status(0), "corrupted")


if __name__ == "__main__":
    unittest.main()
