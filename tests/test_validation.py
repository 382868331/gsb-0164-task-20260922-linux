"""Input validation and public error semantics."""

from __future__ import annotations

import math
import unittest

from ftl_core import (
    FTL,
    FTLValidationError,
    RawFlash,
    UnmappedLogicalPageError,
    FormatMismatchError,
)


def bad(fn):
    """Return a callable asserting *fn* raises FTLValidationError."""

    def runner(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except FTLValidationError:
            return
        raise AssertionError(f"expected FTLValidationError from {fn!r}")

    return runner


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.reject = lambda fn: bad(fn)()

    # -- numeric rules: no NaN/Infinity, bool is not an int -------------
    def test_max_erases_numeric_rules(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            self.reject(lambda v=value: FTL(RawFlash(), v))
        for value in (True, False, 1.0, 0.0, -1, 2**31, "3", None):
            self.reject(lambda v=value: FTL(RawFlash(), v))

    def test_max_erases_accepted_bounds(self):
        FTL(RawFlash(), 0)
        FTL(RawFlash(), 2**31 - 1)

    def test_lpn_rules(self):
        ftl = FTL(RawFlash(), 10)
        for value in (-1, 128, 1.0, True, False, float("nan"), "0", None):
            self.reject(lambda v=value: ftl.read(v))

    def test_payload_rules(self):
        ftl = FTL(RawFlash(), 10)
        self.reject(lambda: ftl.write(0, b"x" * 27))  # >26 usable bytes
        self.reject(lambda: ftl.write(0, "abc"))       # str, not bytes
        self.reject(lambda: ftl.write(0, 7))
        self.reject(lambda: ftl.write(0, [1, 2]))
        # bytearray is accepted (normalised to bytes)
        ftl.write(0, bytearray(b"ok"))
        self.assertEqual(ftl.read(0), b"ok")

    def test_batch_rules(self):
        ftl = FTL(RawFlash(), 10)
        self.reject(lambda: ftl.write_pages({}))
        self.reject(lambda: ftl.write_pages(
            {0: b"a", 1: b"b", 2: b"c", 3: b"d", 4: b"e"}))
        self.reject(lambda: ftl.write_pages([(0, b"a"), (0, b"b")]))
        self.reject(lambda: ftl.write_pages("nope"))
        self.reject(lambda: ftl.write_pages([0, 1]))
        self.reject(lambda: ftl.write_pages([(0, b"a", 1)]))

    def test_tuple_batch_accepted(self):
        ftl = FTL(RawFlash(), 10)
        ftl.write_pages([(0, b"a"), (1, b"b")])
        self.assertEqual(ftl.read(0), b"a")
        self.assertEqual(ftl.read(1), b"b")

    def test_empty_and_boundary_payload(self):
        ftl = FTL(RawFlash(), 10)
        ftl.write(5, b"")
        self.assertEqual(ftl.read(5), b"")
        ftl.write(5, b"z" * 26)
        self.assertEqual(ftl.read(5), b"z" * 26)

    # -- raw flash construction ----------------------------------------
    def test_raw_flash_construction_rules(self):
        RawFlash()
        self.reject(lambda: RawFlash(blocks=0))
        self.reject(lambda: RawFlash(blocks=1.5))
        self.reject(lambda: RawFlash(blocks=True))
        self.reject(lambda: RawFlash(bad_blocks={16}))
        self.reject(lambda: RawFlash(bad_blocks={-1}))
        self.reject(lambda: RawFlash(bad_pages={128}))
        self.reject(lambda: RawFlash(program_faults={"0": "lose_after"}))
        self.reject(lambda: RawFlash(program_faults={0: "explode"}))
        self.reject(lambda: RawFlash(program_faults=[0]))

    def test_format_mismatch(self):
        small = RawFlash(blocks=8)
        with self.assertRaises(FormatMismatchError):
            FTL(small, 10)

    # -- rejection never mutates ---------------------------------------
    def test_rejected_write_leaves_no_physical_action(self):
        flash = RawFlash()
        ftl = FTL(flash, 10)
        before = len(flash.action_log)
        with self.assertRaises(FTLValidationError):
            ftl.write(0, b"x" * 27)
        with self.assertRaises(FTLValidationError):
            ftl.write(-1, b"x")
        self.assertEqual(len(flash.action_log), before)
        self.assertEqual(ftl.mapping, {})

    def test_read_unmapped(self):
        ftl = FTL(RawFlash(), 10)
        with self.assertRaises(UnmappedLogicalPageError):
            ftl.read(127)

    def test_nan_inf_payload_length_not_finite_number(self):
        # sanity: math.isfinite is the basis of the rule
        self.assertFalse(math.isfinite(float("nan")))
        self.assertFalse(math.isfinite(float("inf")))


if __name__ == "__main__":
    unittest.main()
