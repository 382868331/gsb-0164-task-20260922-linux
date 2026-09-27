"""只读快照：旧版本可读、GC 不提前擦除、删除后释放、断电恢复不混用。"""
import unittest
import zlib

from ftl import (
    FTL,
    PAGE_SIZE,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
    RawNand,
    encode_snapshot_record,
    encode_tombstone_record,
)

from refimg import parse_committed_image, parse_snapshots


def pad(data):
    return data.ljust(PAGE_SIZE, b"\xff")


class TestSnapshots(unittest.TestCase):
    def setUp(self):
        self.dev = RawNand(max_erases=8)
        self.ftl = FTL(self.dev)

    def test_snapshot_keeps_old_version_while_current_writes(self):
        self.ftl.write(0, b"v0")
        sid = self.ftl.create_snapshot()
        self.assertEqual(self.ftl.snapshots(), (0,))
        for i in range(1, 40):  # 反复覆盖，必然触发 GC
            self.ftl.write(0, b"v%02d" % i)
        self.assertEqual(self.ftl.read(0), pad(b"v39"))
        self.assertEqual(self.ftl.read_snapshot(sid, 0), pad(b"v0"))
        self.assertIsNone(self.ftl.read_snapshot(sid, 23))
        # 独立参考实现：快照旧映像与当前映像各自独立且不混用
        snaps = parse_snapshots(self.dev)
        self.assertEqual(snaps[sid][0], pad(b"v0"))
        self.assertEqual(parse_committed_image(self.dev)[0], pad(b"v39"))

    def test_snapshot_remount_and_independent_reference(self):
        self.ftl.write(1, b"alpha")
        sid = self.ftl.create_snapshot()
        self.ftl.write(1, b"beta")
        self.ftl.write(2, b"gamma")
        ftl2 = FTL(self.dev)
        self.assertEqual(ftl2.snapshots(), (sid,))
        self.assertEqual(ftl2.read_snapshot(sid, 1), pad(b"alpha"))
        self.assertIsNone(ftl2.read_snapshot(sid, 2))  # 快照点之后才写的页
        self.assertEqual(ftl2.read(1), pad(b"beta"))
        self.assertEqual(ftl2.read(2), pad(b"gamma"))
        snaps = parse_snapshots(self.dev)
        self.assertEqual(set(snaps), {sid})
        self.assertEqual(snaps[sid][1], pad(b"alpha"))
        self.assertNotIn(2, snaps[sid])

    def test_multiple_snapshots_each_immutable(self):
        versions = {}
        for i in range(6):
            self.ftl.write(5, b"gen-%d" % i)
            sid = self.ftl.create_snapshot()
            versions[sid] = pad(b"gen-%d" % i)
        self.assertEqual(self.ftl.snapshots(), tuple(versions))
        for sid, data in versions.items():
            self.assertEqual(self.ftl.read_snapshot(sid, 5), data)
        # 继续大量写入触发 GC，全部历史版本仍完整
        for i in range(60):
            self.ftl.write(i % 24, b"w%04d" % i)
        for sid, data in versions.items():
            self.assertEqual(self.ftl.read_snapshot(sid, 5), data)

    def test_gc_never_erases_live_snapshot_pages(self):
        self.ftl.write(0, b"keep")
        sid = self.ftl.create_snapshot()
        old_ppn = self.ftl.snapshot_mapping(sid)[0]
        for i in range(200):
            self.ftl.write(i % 24, b"x%04d" % i)
        self.ftl.gc_collect()
        # 快照映射指向的物理页始终未被擦除重写
        b, p = divmod(old_ppn, 8)
        self.assertEqual(self.dev.read_page(b, p), pad(b"keep"))
        self.assertEqual(self.ftl.read_snapshot(sid, 0), pad(b"keep"))

    def test_delete_releases_space_then_reclaim(self):
        self.ftl.write(0, b"old")
        sid = self.ftl.create_snapshot()
        pinned = self.ftl.capacity()["snapshot_pinned_pages"]
        self.assertGreaterEqual(pinned, 2)
        for i in range(200):
            self.ftl.write(i % 24, b"y%04d" % i)
        self.assertEqual(self.ftl.read_snapshot(sid, 0), pad(b"old"))
        self.ftl.delete_snapshot(sid)
        self.assertEqual(self.ftl.snapshots(), ())
        # 删除前被钉住的块在删除后变为可回收
        self.assertGreater(self.ftl.capacity()["reclaimable_dead_pages"], 0)
        reclaimed = self.ftl.gc_collect()
        self.assertGreater(reclaimed, 0)
        # 重新挂载：删除是持久的，墓碑与旧记录被擦后快照不复活
        ftl2 = FTL(self.dev)
        self.assertEqual(ftl2.snapshots(), ())
        self.assertEqual(parse_snapshots(self.dev), {})
        with self.assertRaises(InvalidInputError):
            ftl2.read_snapshot(sid, 0)

    def test_delete_persists_across_crash_before_reclaim(self):
        self.ftl.write(0, b"old")
        sid = self.ftl.create_snapshot()
        self.ftl.write(0, b"new")
        # 枚举删除操作自身的每个断电点：墓碑没写完快照仍在，写完则永久删除
        for k in range(1, 4):
            snap = self.dev.snapshot()
            self.dev.set_crash_plan([k])
            self.dev.arm_crash()
            try:
                self.ftl.delete_snapshot(sid)
                crashed = False
            except PowerLossError:
                crashed = True
            self.dev.reboot()
            ftl2 = FTL(self.dev)
            if crashed:
                # 墓碑页可能已写入（原子）；无论是否删除，当前映像不受影响
                if ftl2.snapshots():
                    self.assertEqual(ftl2.read_snapshot(sid, 0), pad(b"old"))
            else:
                self.assertEqual(ftl2.snapshots(), ())
            self.assertEqual(ftl2.read(0), pad(b"new"))
            self.dev.restore(snap)
            self.ftl = FTL(self.dev)

    def test_snapshot_create_crash_is_all_or_nothing(self):
        self.ftl.write(3, b"frozen")
        for k in range(1, 4):
            snap = self.dev.snapshot()
            self.dev.set_crash_plan([k])
            self.dev.arm_crash()
            try:
                sid = self.ftl.create_snapshot()
            except PowerLossError:
                sid = None
            self.dev.reboot()
            ftl2 = FTL(self.dev)
            live = ftl2.snapshots()
            self.assertIn(len(live), (0, 1))
            if live:
                self.assertEqual(ftl2.read_snapshot(live[0], 3), pad(b"frozen"))
            self.assertEqual(ftl2.read(3), pad(b"frozen"))
            self.dev.restore(snap)
            self.ftl = FTL(self.dev)

    def test_out_of_space_rejected_when_snapshots_pin_everything(self):
        for l in range(24):
            self.ftl.write(l, b"gen0")
        generation = 0
        with self.assertRaises(OutOfSpaceError):
            for generation in range(1, 64):
                for l in range(24):
                    self.ftl.write(l, b"g%02d" % generation)
                self.ftl.create_snapshot()
        # 拒绝写后已提交映像与所有快照仍完整
        for sid in self.ftl.snapshots():
            for l in range(24):
                self.assertIsNotNone(self.ftl.read_snapshot(sid, l))

    def test_invalid_snapshot_inputs(self):
        with self.assertRaises(InvalidInputError):
            self.ftl.read_snapshot(123, 0)
        with self.assertRaises(InvalidInputError):
            self.ftl.delete_snapshot(123)
        sid = self.ftl.create_snapshot()
        with self.assertRaises(InvalidInputError):
            self.ftl.read_snapshot(sid, True)
        with self.assertRaises(InvalidInputError):
            self.ftl.read_snapshot(True, 0)
        self.ftl.delete_snapshot(sid)
        with self.assertRaises(InvalidInputError):
            self.ftl.delete_snapshot(sid)  # 已删除，不静默

    def test_corrupt_and_tombstone_records_arbitration(self):
        # 残缺快照记录（CRC 错误）不得生效
        dev = RawNand(max_erases=8)
        rec = bytearray(encode_snapshot_record(7, [0xFF] * 24))
        rec[28] ^= 0xFF
        dev.program(0, 0, bytes(rec))
        ftl = FTL(dev)
        self.assertEqual(ftl.snapshots(), ())

        # 正常创建后，墓碑使快照永久删除，重新挂载也不复活
        ftl.write(0, b"x")
        sid = ftl.create_snapshot()
        ftl.delete_snapshot(sid)
        ftl2 = FTL(dev)
        self.assertEqual(ftl2.snapshots(), ())

        # 伪造墓碑（CRC 错误）不得删除任何快照
        dev2 = RawNand(max_erases=8)
        ftl3 = FTL(dev2)
        ftl3.write(0, b"y")
        sid2 = ftl3.create_snapshot()
        fake = bytearray(encode_tombstone_record(sid2))
        fake[4] ^= 0x01
        dev2.program(3, 0, bytes(fake))
        ftl4 = FTL(dev2)
        self.assertEqual(ftl4.snapshots(), (sid2,))
        self.assertEqual(ftl4.read_snapshot(sid2, 0), pad(b"y"))


if __name__ == "__main__":
    unittest.main()
