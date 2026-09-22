"""Power-loss enumeration against an independently derived logical image.

For a short write trace we inject ``lose_after`` at every possible
physical page-program point, remount, and compare the recovered image
with the prefix computed by :mod:`tests.reference` -- which never
imports the core.  We then cover erase cut points and dense cut points
amid garbage collection.
"""

from __future__ import annotations

import unittest

from ftl_core import FTL, PowerLossError, RawFlash

from reference import (
    expected_prefix,
    final_image,
    programs_for_batch,
    rewrite_sequence,
)
from util import drive_workload, map_is_self_consistent


# Mixed single- and multi-page batches; values are <= 26 bytes.
TRACE: list[dict[int, bytes]] = [
    {0: b"a0"},
    {1: b"b0"},
    {0: b"a1"},
    {2: b"c0", 3: b"d0"},
    {1: b"b1"},
    {0: b"a2", 2: b"c1", 4: b"e0", 5: b"f0"},
]


def apply_trace(ftl: FTL) -> None:
    for batch in TRACE:
        ftl.write_pages(dict(batch))


class TestProgramCutPoints(unittest.TestCase):
    def test_every_program_cut_point_matches_independent_prefix(self):
        reference = RawFlash()
        ref_ftl = FTL(reference, 10_000)
        apply_trace(ref_ftl)
        total_programs = sum(
            1 for action in reference.action_log if action.kind == "program"
        )
        # Independent count: sum of (pages + commit) per batch.
        self.assertEqual(
            total_programs, sum(programs_for_batch(len(b)) for b in TRACE)
        )

        for cut in range(total_programs + 1):
            with self.subTest(cut=cut):
                faults = {} if cut == total_programs else {cut: "lose_after"}
                flash = RawFlash(program_faults=faults)
                ftl = FTL(flash, 10_000)
                try:
                    apply_trace(ftl)
                    self.assertEqual(cut, total_programs)
                except PowerLossError:
                    pass
                recovered = FTL(flash, 10_000)
                got = {lpn: recovered.read(lpn) for lpn in sorted(recovered.mapping)}
                # Fault at ordinal `cut` fires AFTER that program landed.
                landed = total_programs if cut == total_programs else cut + 1
                self.assertEqual(got, expected_prefix(TRACE, landed))
                map_is_self_consistent(recovered)


class TestGarbageCollectionCutPoints(unittest.TestCase):
    COUNT = 120
    NLPN = 6
    MAX_ERASES = 8

    def setUp(self):
        self.writes = rewrite_sequence(self.COUNT, self.NLPN)
        self.expect = final_image(self.writes)

    def _counts_on_clean_device(self, max_erases):
        flash = RawFlash()
        ftl = FTL(flash, max_erases)
        drive_workload(flash, max_erases, self.writes)
        programs = sum(1 for a in flash.action_log if a.kind == "program")
        erases = sum(1 for a in flash.action_log if a.kind == "erase")
        self.assertGreater(erases, 0, "workload must actually trigger GC")
        return programs, erases

    def test_every_erase_cut_point(self):
        _programs, erases = self._counts_on_clean_device(self.MAX_ERASES)
        for cut in range(erases):
            with self.subTest(erase_cut=cut):
                flash = RawFlash(erase_faults={cut: "lose_after"})
                ftl = drive_workload(flash, self.MAX_ERASES, self.writes)
                for lpn in range(self.NLPN):
                    self.assertEqual(ftl.read(lpn), self.expect[lpn])
                map_is_self_consistent(ftl)

    def test_dense_program_cut_points(self):
        programs, _erases = self._counts_on_clean_device(self.MAX_ERASES)
        for cut in range(0, programs, 4):
            with self.subTest(program_cut=cut):
                flash = RawFlash(program_faults={cut: "lose_after"})
                ftl = drive_workload(flash, self.MAX_ERASES, self.writes)
                for lpn in range(self.NLPN):
                    self.assertEqual(ftl.read(lpn), self.expect[lpn])
                map_is_self_consistent(ftl)

    def test_immediately_after_every_gc_erase_map_is_consistent(self):
        _programs, erases = self._counts_on_clean_device(self.MAX_ERASES)
        for cut in range(erases):
            with self.subTest(post_erase_cut=cut):
                flash = RawFlash(erase_faults={cut: "lose_after"})
                ftl = FTL(flash, self.MAX_ERASES)
                try:
                    for lpn, data in self.writes:
                        ftl.write(lpn, data)
                    self.fail("fault did not fire")
                except PowerLossError:
                    pass
                rebooted = FTL(flash, self.MAX_ERASES)
                map_is_self_consistent(rebooted)

    def test_combined_program_and_erase_faults(self):
        flash = RawFlash(
            program_faults={77: "lose_after"},
            erase_faults={3: "lose_after"},
        )
        ftl = drive_workload(flash, self.MAX_ERASES, self.writes)
        for lpn in range(self.NLPN):
            self.assertEqual(ftl.read(lpn), self.expect[lpn])
        map_is_self_consistent(ftl)


class TestRepeatedCrashesAcrossGC(unittest.TestCase):
    """Regression: many crashes while GC erases frames must never roll a
    logical page back to an older committed value.

    Earlier recovery skipped an entire commit frame when a data page it
    named had been erased by a *later* GC relocation; that is normal and
    must instead resolve per-lpn to the newest commit whose data is still
    readable.  We inject dense write/erase faults and compare against the
    independently computed last-writer-wins image.
    """

    def test_dense_multi_crash_preserves_latest_image(self):
        import random

        rng = random.Random(20260922)
        num_lpns = 7
        num_batches = 101
        batches: list[dict[int, bytes]] = []
        counter: dict[int, int] = {}
        for _ in range(num_batches):
            d: dict[int, bytes] = {}
            for _ in range(rng.randint(1, 4)):
                lpn = rng.randrange(num_lpns)
                counter[lpn] = counter.get(lpn, 0) + 1
                d[lpn] = bytes(
                    (lpn, counter[lpn] & 0xFF, rng.randrange(256))
                )
            batches.append(d)
        expect: dict[int, bytes] = {}
        for d in batches:
            expect.update(d)

        program_faults = set(range(0, 500, 11))
        erase_faults = set(range(0, 80, 7))
        flash = RawFlash(
            program_faults={k: "lose_after" for k in program_faults},
            erase_faults={k: "lose_after" for k in erase_faults},
        )
        ftl = FTL(flash, 50)
        i = 0
        crashes = 0
        while i < len(batches):
            try:
                ftl.write_pages(dict(batches[i]))
                i += 1
            except PowerLossError:
                crashes += 1
                ftl = FTL(flash, 50)
                map_is_self_consistent(ftl)
        self.assertGreater(crashes, 10)  # the scenario really crashed a lot
        map_is_self_consistent(ftl)
        for lpn, want in expect.items():
            self.assertEqual(ftl.read(lpn), want)


if __name__ == "__main__":
    unittest.main()
