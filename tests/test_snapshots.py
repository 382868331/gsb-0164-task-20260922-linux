"""只读快照：旧版本保留、GC 不擦有效快照页、删除释放、断电恢复一致性。"""
import unittest

from ftl import (
    FTL,
    PAGE_SIZE,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
    RawNand,
)
from refimg import parse_snapshots


def payload(i, size=12):
    return bytes(((i * 17 + j * 5 + 3) % 256 for j in range(size)))


def slot(data):
    return data.ljust(PAGE_SIZE, b"\xff")


class TestSnapshotBasics(unittest.TestCase):
    def test_snapshot_keeps_old_version_while_current_advances(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        for i in range(10):
            ftl.write(i % 6, payload(i))
        v1 = {l: ftl.read(l) for l in range(6)}
        ftl.create_snapshot(7)
        for i in range(10, 30):
            ftl.write(i % 6, payload(i))
        v2 = {l: ftl.read(l) for l in range(6)}
        self.assertEqual(ftl.list_snapshots(), (7,))
        for l in range(6):
            self.assertEqual(ftl.read_snapshot(7, l), v1[l])
            self.assertEqual(ftl.read(l), v2[l])
            self.assertNotEqual(v1[l], v2[l])
        # 未映射页：快照与当前都返回 None
        self.assertIsNone(ftl.read_snapshot(7, 20))
        self.assertIsNone(ftl.read(20))
        info = ftl.snapshot_info(7)
        self.assertEqual(info["snap_id"], 7)
        self.assertEqual(info["mapped_pages"], 6)

    def test_empty_image_snapshot(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.create_snapshot(0)
        self.assertIsNone(ftl.read_snapshot(0, 3))
        ftl.write(3, b"later")
        self.assertIsNone(FTL(dev).read_snapshot(0, 3))
        self.assertEqual(FTL(dev).read(3), slot(b"later"))

    def test_tombstone_pages_are_eventually_reclaimed(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.write(0, payload(0))
        for _ in range(3):
            ftl.create_snapshot(4)
            ftl.delete_snapshot(4)
        self.assertEqual(ftl.list_snapshots(), ())
        dead_before = ftl.capacity()["reclaimable_dead_pages"]
        self.assertGreater(dead_before, 0)
        reclaimed = ftl.gc_collect()
        self.assertGreater(reclaimed, 0)
        # 重挂载后墓碑全部消失也不会让任何旧快照复活
        self.assertEqual(FTL(dev).list_snapshots(), ())

    def test_duplicate_and_missing_snapshot_errors(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        ftl.create_snapshot(1)
        with self.assertRaises(InvalidInputError):
            ftl.create_snapshot(1)
        with self.assertRaises(InvalidInputError):
            ftl.delete_snapshot(9)
        with self.assertRaises(InvalidInputError):
            ftl.read_snapshot(9, 0)
        ftl.delete_snapshot(1)
        with self.assertRaises(InvalidInputError):
            ftl.delete_snapshot(1)
        # 删除后同一 ID 可重新创建
        ftl.write(0, b"x")
        ftl.create_snapshot(1)
        self.assertEqual(ftl.read_snapshot(1, 0), slot(b"x"))
        for bad in (-1, 65536, True, 1.0):
            with self.assertRaises(InvalidInputError):
                ftl.create_snapshot(bad)


class TestSnapshotGC(unittest.TestCase):
    def test_gc_never_erases_snapshot_blocks_and_frees_after_delete(self):
        dev = RawNand(max_erases=8)
        ftl = FTL(dev)
        for i in range(8):
            ftl.write(i, payload(100 + i))
        ftl.create_snapshot(3)
        v1 = {l: ftl.read_snapshot(3, l) for l in range(8)}
        # 大量覆盖写 + 手动 GC：快照旧版本始终可读
        for i in range(120):
            ftl.write(i % 8, payload(i))
        ftl.gc_collect()
        for l in range(8):
            self.assertEqual(FTL(dev).read_snapshot(3, l), v1[l])
        retained = ftl.capacity()["snapshot_retained_pages"]
        self.assertGreaterEqual(retained, 1)  # 至少描述符页保留
        ftl.delete_snapshot(3)
        self.assertEqual(ftl.list_snapshots(), ())
        self.assertEqual(FTL(dev).list_snapshots(), ())

    def test_write_rejected_when_snapshot_pins_all_space(self):
        dev = RawNand(max_erases=1)  # 极低磨损上限：很快无可回收块
        ftl = FTL(dev)
        for i in range(24):
            ftl.write(i, payload(i))
        ftl.create_snapshot(5)
        v1 = {l: ftl.read_snapshot(5, l) for l in range(24)}
        count = 0
        with self.assertRaises(OutOfSpaceError):
            while True:
                ftl.write(count % 24, payload(200 + count))
                count += 1
        # 拒绝写入后快照与当前版本都保持完整
        for l in range(24):
            self.assertEqual(FTL(dev).read_snapshot(5, l), v1[l])
        # 删除快照（墓碑可落保留页）后空间可被回收并继续写
        ftl.delete_snapshot(5)
        ftl.write(0, b"after-delete")
        self.assertEqual(ftl.read(0), slot(b"after-delete"))


class TestSnapshotCrashPoints(unittest.TestCase):
    def _build_base(self):
        dev = RawNand(max_erases=4)
        ftl = FTL(dev)
        for i in range(6):
            ftl.write(i, payload(i))
        return dev, ftl

    def test_crash_around_create_and_overwrites(self):
        dev, ftl = self._build_base()
        snap0 = dev.snapshot()
        n_before = len(dev.action_log())
        ftl.create_snapshot(2)
        for i in range(6, 12):
            ftl.write(i - 6, payload(i))
        total = len(dev.action_log())
        v1 = {l: slot(payload(l)) for l in range(6)}
        # 不创建快照，仅让一次未提交写在数据页写后掉电：重启后快照不存在，
        # 当前映像仍完全是基础映像（未提交写丢失，无新旧混用）
        dev.restore(snap0)
        dev.set_crash_plan([1])
        dev.arm_crash()
        ftl = FTL(dev)
        with self.assertRaises(PowerLossError):
            ftl.write(0, b"crash-before-snapshot")
        dev.reboot()
        r = FTL(dev)
        self.assertEqual(r.list_snapshots(), ())
        for l in range(6):
            self.assertEqual(r.read(l), v1[l])
        # 崩溃点 k = 描述符动作之后快照才存在
        desc_idx = n_before  # snapshot-create 动作下标
        for k in range(1, total - n_before + 1):
            with self.subTest(k=k):
                dev.restore(snap0)
                dev.set_crash_plan([n_before + k])
                dev.arm_crash()
                ftl = FTL(dev)
                try:
                    ftl.create_snapshot(2)
                    for i in range(6, 12):
                        ftl.write(i - 6, payload(i))
                except PowerLossError:
                    pass
                dev.reboot()
                r = FTL(dev)
                global_k = n_before + k
                if global_k <= desc_idx:
                    self.assertEqual(r.list_snapshots(), ())
                else:
                    self.assertEqual(r.list_snapshots(), (2,))
                    for l in range(6):
                        self.assertEqual(r.read_snapshot(2, l), v1[l])
                # 任何情况下快照应与独立参考解析器一致，且无新旧混用
                ref = parse_snapshots(dev)
                self.assertEqual(sorted(ref), list(r.list_snapshots()))
                for sid, image in ref.items():
                    for l, data in image.items():
                        self.assertEqual(r.read_snapshot(sid, l), data)

    def test_crash_around_delete(self):
        dev, ftl = self._build_base()
        ftl.create_snapshot(2)
        snap0 = dev.snapshot()
        v1 = {l: slot(payload(l)) for l in range(6)}
        # 场景 1：墓碑动作之前掉电（计划点晚于删除所需动作 => 删除成功）
        # 删除只需 1 个物理动作：在第 1 个动作后掉电，墓碑已完整，快照应消失
        dev.restore(snap0)
        dev.set_crash_plan([1])
        dev.arm_crash()
        ftl = FTL(dev)
        with self.assertRaises(PowerLossError):
            ftl.delete_snapshot(2)
        dev.reboot()
        self.assertEqual(FTL(dev).list_snapshots(), ())
        # 场景 2：不执行删除，直接重挂载，快照仍完整
        dev.restore(snap0)
        dev.reboot()
        r = FTL(dev)
        self.assertEqual(r.list_snapshots(), (2,))
        for l in range(6):
            self.assertEqual(r.read_snapshot(2, l), v1[l])
            self.assertEqual(r.read(l), v1[l])


if __name__ == "__main__":
    unittest.main()
