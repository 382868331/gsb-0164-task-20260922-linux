"""Garbage collection, wear levelling, bad blocks, and exhaustion."""

from __future__ import annotations

import unittest

from ftl_core import (
    FTL,
    RawFlash,
    StorageFullError,
    WearLimitError,
)

from reference import final_image, rewrite_sequence
from util import drive_workload, map_is_self_consistent


class TestGarbageCollection(unittest.TestCase):
    def test_rewrites_trigger_erases_and_preserve_data(self):
        writes = rewrite_sequence(120, 6)
        expect = final_image(writes)
        flash = RawFlash()
        ftl = drive_workload(flash, 10_000, writes)
        self.assertGreater(sum(flash.erase_counts), 0)
        for lpn in range(6):
            self.assertEqual(ftl.read(lpn), expect[lpn])
        map_is_self_consistent(ftl)

    def test_live_page_relocated_before_erase(self):
        # Property: at no point recorded in the action log is a block
        # erased while the committed map points into it.  We rebuild the
        # map by replaying the trace up to each erase using fresh fault
        # injection-free runs is complex; instead check the invariant on
        # a running device around each erase via a wrapping device.
        flash = RawFlash()
        ftl = FTL(flash, 10_000)
        violations = []
        original_erase = flash.erase

        def checked_erase(block):
            base = block * 8
            mapped_into = [
                lpn for lpn, ppn in ftl.mapping.items()
                if base <= ppn < base + 8
            ]
            if mapped_into:
                violations.append((block, mapped_into))
            return original_erase(block)

        flash.erase = checked_erase  # type: ignore[assignment]
        for lpn, data in rewrite_sequence(120, 6):
            ftl.write(lpn, data)
        self.assertEqual(violations, [])

    def test_force_gc_returns_block_and_reclaims(self):
        flash = RawFlash()
        ftl = FTL(flash, 10_000)
        for i in range(10):
            ftl.write(0, bytes([i]) * 4)
        before = ftl.capacity()["free_physical_pages"]
        block = ftl.force_gc()
        after = ftl.capacity()["free_physical_pages"]
        self.assertIsInstance(block, int)
        self.assertGreater(after, before)
        # newest value survives
        self.assertEqual(ftl.read(0), bytes([9]) * 4)

    def test_force_gc_empty_device(self):
        ftl = FTL(RawFlash(), 10)
        with self.assertRaises(StorageFullError):
            ftl.force_gc()


class TestWearLevelling(unittest.TestCase):
    def test_erase_counts_respect_limit(self):
        writes = rewrite_sequence(300, 6)
        flash = RawFlash()
        ftl = drive_workload(flash, 50, writes)
        self.assertTrue(all(c <= 50 for c in flash.erase_counts))

    def test_wear_limit_reached_is_distinct_error(self):
        flash = RawFlash()
        ftl = FTL(flash, max_erases_per_block=1)
        with self.assertRaises(WearLimitError):
            for i in range(400):
                ftl.write(i % 6, bytes([i & 0xFF]) * 3)
        # No block exceeded the ceiling even though the limit stopped us.
        self.assertTrue(all(c <= 1 for c in flash.erase_counts))

    def test_wear_is_distributed_across_blocks(self):
        writes = rewrite_sequence(400, 8)
        flash = RawFlash()
        ftl = drive_workload(flash, 100, writes)
        counts = [c for c in flash.erase_counts]
        # Greedy lowest-erase-count allocation should keep the spread tight.
        self.assertLessEqual(max(counts) - min(counts), 2)


class TestBadBlocks(unittest.TestCase):
    def test_factory_bad_block_never_programmed(self):
        writes = rewrite_sequence(120, 6)
        expect = final_image(writes)
        flash = RawFlash(bad_blocks={3})
        ftl = drive_workload(flash, 100, writes)
        self.assertIn(3, flash.quarantined_blocks)
        for ppn in range(24, 32):
            self.assertTrue(flash.is_blank(ppn))
        for lpn in range(6):
            self.assertEqual(ftl.read(lpn), expect[lpn])

    def test_block_with_bad_page_is_quarantined(self):
        flash = RawFlash(bad_pages={50})  # block 6
        ftl = FTL(flash, 100)
        for lpn, data in rewrite_sequence(120, 6):
            ftl.write(lpn, data)
        self.assertIn(6, flash.quarantined_blocks)
        for ppn in range(48, 56):
            self.assertTrue(flash.is_blank(ppn))

    def test_many_bad_blocks_lead_to_clean_exhaustion(self):
        flash = RawFlash(bad_blocks=set(range(14)))
        ftl = FTL(flash, 10_000)
        with self.assertRaises(StorageFullError):
            for i in range(40):
                ftl.write(i, b"x" * 10)
        # Bad blocks are never erased as a recovery attempt.
        self.assertEqual(
            sorted(flash.quarantined_blocks), list(range(14))
        )


class TestCapacity(unittest.TestCase):
    def test_capacity_accounting_balances(self):
        flash = RawFlash()
        ftl = FTL(flash, 100)
        cap0 = ftl.capacity()
        self.assertEqual(cap0["free_physical_pages"], 128)
        self.assertEqual(cap0["live_data_pages"], 0)
        self.assertEqual(cap0["stranded_pages"], 0)
        ftl.write(0, b"abc")
        cap1 = ftl.capacity()
        # exactly two pages used (data + commit)
        self.assertEqual(cap1["free_physical_pages"], 126)
        self.assertEqual(cap1["live_data_pages"], 1)

    def test_capacity_with_bad_blocks(self):
        flash = RawFlash(bad_blocks={0, 1})
        ftl = FTL(flash, 100)
        cap = ftl.capacity()
        self.assertEqual(cap["free_physical_pages"], 112)
        self.assertEqual(cap["stranded_pages"], 16)
        self.assertEqual(cap["bad_blocks"], [0, 1])

    def test_true_full_device_reports_full(self):
        flash = RawFlash()
        ftl = FTL(flash, 10_000)
        # Fill with DISTINCT live logical pages (each costs data+commit).
        with self.assertRaises(StorageFullError):
            for i in range(128):
                ftl.write(i, bytes([i & 0xFF]))
        # Capacity accounting stays internally consistent afterwards.
        cap = ftl.capacity()
        self.assertEqual(cap["free_physical_pages"], 8)  # spare reserved
        self.assertEqual(cap["profitable_gc_available"], False)


if __name__ == "__main__":
    unittest.main()
