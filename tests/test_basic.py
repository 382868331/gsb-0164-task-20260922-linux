"""基本读写、日志、容量与持久性测试。"""
import unittest

from ftl import FTL, PAGE_SIZE, RawNand
from refimg import assert_image_matches, assert_matches_reference


def payload(i, size=10):
    return bytes(((i * 13 + j * 7) % 256 for j in range(size)))


class TestBasicReadWrite(unittest.TestCase):
    def test_unmapped_read_returns_none(self):
        ftl = FTL(RawNand(max_erases=4))
        for lpn in range(24):
            self.assertIsNone(ftl.read(lpn))

    def test_write_read_roundtrip(self):
        ftl = FTL(RawNand(max_erases=4))
        ftl.write(3, b"hello")
        self.assertEqual(ftl.read(3), b"hello".ljust(PAGE_SIZE, b"\xff"))
        self.assertIsNone(ftl.read(4))

    def test_overwrite_latest_wins(self):
        ftl = FTL(RawNand(max_erases=4))
        ftl.write(0, b"first")
        ftl.write(0, b"second")
        self.assertEqual(ftl.read(0), b"second".ljust(PAGE_SIZE, b"\xff"))

    def test_full_32byte_write(self):
        ftl = FTL(RawNand(max_erases=4))
        data = bytes(range(32))
        ftl.write(23, data)
        self.assertEqual(ftl.read(23), data)

    def test_capacity_accounting(self):
        dev = RawNand(max_erases=4)
        ftl = FTL(dev)
        cap0 = ftl.capacity()
        self.assertEqual(cap0["free_physical_pages"], 128)
        self.assertEqual(cap0["reclaimable_dead_pages"], 0)
        self.assertEqual(cap0["mapped_pages"], 0)
        for i in range(5):
            ftl.write(i, payload(i))
        cap = ftl.capacity()
        self.assertEqual(cap["free_physical_pages"], 128 - 10)  # 每次写占 2 页
        self.assertEqual(cap["mapped_pages"], 5)
        self.assertEqual(cap["reclaimable_dead_pages"], 4)  # 4 条过期提交记录
        ftl.write(0, payload(99))  # 覆盖写：+1 死数据页 +1 过期提交记录
        cap = ftl.capacity()
        self.assertEqual(cap["reclaimable_dead_pages"], 6)
        self.assertEqual(cap["mapped_pages"], 5)

    def test_logs(self):
        dev = RawNand(max_erases=4)
        ftl = FTL(dev)
        ftl.write(1, b"abc")
        ftl.read(1)
        ftl.read(2)
        phys = ftl.physical_log()
        self.assertEqual(len(phys), 2)
        self.assertEqual(phys[0].kind, "program")
        self.assertEqual(phys[0].purpose, "user-data")
        self.assertEqual(phys[1].kind, "program")
        self.assertEqual(phys[1].purpose, "commit")
        logical = ftl.logical_log()
        self.assertEqual(
            [e["op"] for e in logical], ["write", "read", "read"]
        )
        self.assertEqual(logical[1]["result"], "ok")
        self.assertEqual(logical[2]["result"], "unmapped")

    def test_determinism(self):
        logs = []
        for _ in range(2):
            dev = RawNand(max_erases=4)
            ftl = FTL(dev)
            for i in range(10):
                ftl.write(i % 24, payload(i))
            logs.append(dev.action_log())
        self.assertEqual(logs[0], logs[1])


class TestPersistence(unittest.TestCase):
    def test_remount_recovers_committed_image(self):
        dev = RawNand(max_erases=4)
        shadow = {}
        ftl = FTL(dev)
        for i in range(30):  # 60 页 < 128，不触发 GC
            lpn = i % 24
            ftl.write(lpn, payload(i))
            shadow[lpn] = payload(i)
        seq_before = ftl.commit_seq
        # 重新挂载（模拟重启，无掉电）
        ftl2 = FTL(dev)
        self.assertEqual(ftl2.commit_seq, seq_before)
        assert_image_matches(self, ftl2, shadow)
        assert_matches_reference(self, ftl2, dev)
        # 恢复后继续写，序列号延续
        ftl2.write(0, b"after-remount")
        self.assertEqual(ftl2.commit_seq, seq_before + 1)
        self.assertEqual(ftl2.read(0), b"after-remount".ljust(PAGE_SIZE, b"\xff"))

    def test_shadow_model_trace(self):
        # 往返一致补充检查：确定性轨迹与测试侧影子模型逐写比较
        dev = RawNand(max_erases=4)
        ftl = FTL(dev)
        shadow = {}
        for i in range(40):
            lpn = (i * 7) % 24
            data = payload(i, size=(i % 32) + 1)
            ftl.write(lpn, data)
            shadow[lpn] = data
            if i % 5 == 0:
                assert_image_matches(self, ftl, shadow)
        assert_image_matches(self, ftl, shadow)
        assert_matches_reference(self, ftl, dev)


if __name__ == "__main__":
    unittest.main()
