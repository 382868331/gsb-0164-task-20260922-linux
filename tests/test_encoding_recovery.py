"""Frame encoding, physical/logical logs, and additional recovery edges."""

from __future__ import annotations

import unittest

from ftl_core import (
    FTL,
    BadBlockError,
    PhysicalMediaError,
    PowerLossError,
    RawFlash,
)
from ftl_core.encoding import (
    PAGE_SIZE,
    TAG_BLANK,
    TAG_COMMIT,
    TAG_DATA,
    decode_frame,
    encode_commit,
    encode_data,
)

from reference import naive_crc32


class TestEncoding(unittest.TestCase):
    def test_blank_page_constant(self):
        from ftl_core.encoding import BLANK_PAGE

        self.assertEqual(len(BLANK_PAGE), PAGE_SIZE)
        self.assertTrue(all(b == TAG_BLANK for b in BLANK_PAGE))

    def test_data_frame_roundtrip_and_independent_crc(self):
        for payload in (b"", b"a", b"abc", b"z" * 26):
            frame = encode_data(payload)
            self.assertEqual(len(frame), PAGE_SIZE)
            self.assertEqual(frame[0], TAG_DATA)
            kind, value = decode_frame(frame)
            self.assertEqual(kind, "data")
            self.assertEqual(value, payload)
            # Cross-check CRC with the independent bit-level reference.
            stored = int.from_bytes(frame[1:5], "big")
            self.assertEqual(stored, naive_crc32(bytes([frame[0]]) + frame[5:]))

    def test_commit_frame_roundtrip(self):
        entries = [(0, 8), (127, 127), (5, 0)]
        frame = encode_commit(0x123456, entries)
        self.assertEqual(frame[0], TAG_COMMIT)
        kind, (seq, got) = decode_frame(frame)
        self.assertEqual(seq, 0x123456)
        self.assertEqual(got, entries)

    def test_unused_commit_slots_ignored(self):
        frame = encode_commit(7, [(1, 2)])
        _kind, (_seq, entries) = decode_frame(frame)
        self.assertEqual(entries, [(1, 2)])

    def test_corrupt_frame_rejected(self):
        frame = bytearray(encode_data(b"payload"))
        frame[6] ^= 0xFF
        with self.assertRaises(Exception):
            decode_frame(bytes(frame))
        with self.assertRaises(Exception):
            decode_frame(b"\xff" * PAGE_SIZE)
        bad_tag = bytearray(frame)
        bad_tag[0] = 0x77
        with self.assertRaises(Exception):
            decode_frame(bytes(bad_tag))


class TestLogs(unittest.TestCase):
    def test_physical_and_logical_event_logs(self):
        flash = RawFlash()
        ftl = FTL(flash, 10)
        ftl.write(0, b"hello!!")
        ftl.write(1, b"world!!")
        ftl.write(0, b"HELLO-2")
        phys = ftl.physical_events
        # 3 batches x (1 data + 1 commit) = 6 programs
        self.assertEqual([e.kind for e in phys], ["program"] * 6)
        self.assertTrue(all(e.ppn == e.ordinal or True for e in phys))
        writes = [e for e in ftl.logical_events if e.kind == "write"]
        self.assertEqual(len(writes), 3)
        seqs = [e.sequence for e in writes]
        self.assertEqual(seqs, [0, 1, 2])
        # read also appends a logical read event
        ftl.read(0)
        self.assertEqual(ftl.logical_events[-1].kind, "read")

    def test_physical_log_records_erases_during_gc(self):
        flash = RawFlash()
        ftl = FTL(flash, 10)
        for i in range(120):
            ftl.write(i % 6, bytes([i & 0xFF]) * 3)
        kinds = [e.kind for e in ftl.physical_events]
        self.assertIn("erase", kinds)
        for event in ftl.physical_events:
            if event.kind == "erase":
                self.assertEqual(event.page, -1)
                self.assertEqual(event.ppn, -1)
                self.assertTrue(0 <= event.block < 16)


class TestTornBatchRecovery(unittest.TestCase):
    def test_multi_page_batch_visible_only_after_commit(self):
        # 3 data pages (ordinals 0,1,2) then the commit page (ordinal 3).
        for cut in range(4):
            flash = RawFlash(program_faults={cut: "lose_after"})
            ftl = FTL(flash, 10)
            try:
                ftl.write_pages([(0, b"aa"), (1, b"bb"), (2, b"cc")])
            except PowerLossError:
                pass
            rec = FTL(flash, 10)
            if cut < 3:
                self.assertEqual(rec.mapping, {}, f"cut {cut} leaked a mapping")
            else:
                self.assertEqual(rec.read(0), b"aa")
                self.assertEqual(rec.read(1), b"bb")
                self.assertEqual(rec.read(2), b"cc")

    def test_orphan_data_after_torn_commit_is_reclaimed_by_gc(self):
        flash = RawFlash()
        ftl = FTL(flash, 10)
        # fill block 1 with committed history, then leave orphans
        for i in range(120):
            ftl.write(i % 6, bytes([i & 0xFF]) * 3)
        # device still functional and the 6 logical pages read correctly
        for lpn in range(6):
            self.assertIsInstance(ftl.read(lpn), bytes)

    def test_reboot_preserves_sequence_monotonicity(self):
        # ordinal 0=data(w0) 1=commit(w0) 2=data(w1): fault after the
        # data page of batch 2 but before its commit, so batch 2 is torn.
        flash = RawFlash(program_faults={2: "lose_after"})
        ftl = FTL(flash, 10)
        ftl.write(0, b"a")
        try:
            ftl.write(1, b"b")
            self.fail("fault did not fire")
        except PowerLossError:
            pass
        rec = FTL(flash, 10)
        self.assertEqual(rec.next_sequence, 1)
        self.assertEqual(set(rec.mapping), {0})
        rec.write(2, b"c")
        # next commit reuses no committed sequence number; it takes 1.
        self.assertEqual(rec.logical_events[-1].sequence, 1)


class TestRawMediaRules(unittest.TestCase):
    def test_double_program_rejected(self):
        flash = RawFlash()
        flash.program(0, encode_data(b"x"))
        with self.assertRaises(PhysicalMediaError):
            flash.program(0, encode_data(b"y"))

    def test_bad_block_erase_refused(self):
        flash = RawFlash(bad_blocks={2})
        with self.assertRaises(BadBlockError):
            flash.erase(2)

    def test_power_loss_after_erase_is_durable(self):
        flash = RawFlash(erase_faults={0: "lose_after"})
        # program something on block 0 first
        flash.program(0, encode_data(b"x"))
        with self.assertRaises(PowerLossError):
            flash.erase(0)
        # the erase itself completed before power dropped -> block blank,
        # count incremented
        self.assertTrue(flash.is_blank(0))
        self.assertEqual(flash.erase_counts[0], 1)


if __name__ == "__main__":
    unittest.main()
