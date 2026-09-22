"""提交记录的完整性与可见性：残缺/伪造/过期记录不得生效。"""
import unittest
import zlib

from ftl import FTL, PAGE_SIZE, RawNand

MAGIC = 0xA5


def make_record(seq, mapping_bytes, *, corrupt_crc=False):
    body = bytes([MAGIC]) + seq.to_bytes(3, "little") + mapping_bytes
    crc = 0xDEADBEEF if corrupt_crc else (zlib.crc32(body) & 0xFFFFFFFF)
    return body + crc.to_bytes(4, "little")


class TestCommitRecordIntegrity(unittest.TestCase):
    def test_torn_commit_record_invisible(self):
        # 模拟写了一半的提交页：magic 正确但 CRC 错误 → 不得生效
        dev = RawNand(max_erases=4)
        body = bytes([MAGIC]) + (1).to_bytes(3, "little") + bytes([0xFF] * 24)
        assert (zlib.crc32(body) & 0xFFFFFFFF) != 0xDEADBEEF  # 固定输入，必然成立
        dev.program(0, 0, body + b"\xde\xad\xbe\xef")
        ftl = FTL(dev)
        self.assertIsNone(ftl.read(0))
        self.assertEqual(ftl.commit_seq, 0)
        self.assertEqual(ftl.capacity()["free_physical_pages"], 127)

    def test_garbage_page_ignored(self):
        dev = RawNand(max_erases=4)
        dev.program(4, 3, b"\x00" * 32)  # 全零垃圾页，非空白也非合法记录
        ftl = FTL(dev)
        self.assertIsNone(ftl.read(0))
        self.assertEqual(ftl.capacity()["free_physical_pages"], 127)
        ftl.write(0, b"ok")
        self.assertEqual(ftl.read(0), b"ok".ljust(PAGE_SIZE, b"\xff"))

    def test_duplicate_ppn_record_rejected(self):
        dev = RawNand(max_erases=4)
        mapping = bytes([5, 5] + [0xFF] * 22)  # 重复物理页号 → 结构非法
        dev.program(1, 1, make_record(7, mapping))
        ftl = FTL(dev)
        self.assertEqual(ftl.commit_seq, 0)
        self.assertIsNone(ftl.read(0))

    def test_self_referencing_record_rejected(self):
        dev = RawNand(max_erases=4)
        self_ppn = 2 * 8 + 3
        mapping = bytes([self_ppn] + [0xFF] * 23)
        dev.program(2, 3, make_record(9, mapping))
        ftl = FTL(dev)
        self.assertEqual(ftl.commit_seq, 0)
        self.assertIsNone(ftl.read(0))

    def test_stale_commit_not_chosen(self):
        dev = RawNand(max_erases=4)
        ftl = FTL(dev)
        ftl.write(0, b"old")
        ftl.write(0, b"new")
        ftl2 = FTL(dev)
        self.assertEqual(ftl2.read(0), b"new".ljust(PAGE_SIZE, b"\xff"))
        self.assertEqual(ftl2.commit_seq, 2)


if __name__ == "__main__":
    unittest.main()
