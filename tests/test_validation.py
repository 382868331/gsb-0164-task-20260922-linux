"""输入校验契约测试：非法输入给可定位错误，不静默纠正。"""
import unittest

from ftl import (
    FTL,
    InvalidInputError,
    RawNand,
    require_bytes,
    require_finite_number,
    require_int,
)


class TestValidators(unittest.TestCase):
    def test_require_int_rejects_bool_and_float(self):
        for bad in (True, False, 1.0, 0.0, "3", None, [3]):
            with self.assertRaises(InvalidInputError, msg=f"应拒绝 {bad!r}"):
                require_int("n", bad)
        self.assertEqual(require_int("n", 3), 3)
        self.assertEqual(require_int("n", -5, -10, 10), -5)

    def test_require_int_range(self):
        with self.assertRaises(InvalidInputError):
            require_int("n", -1, 0, 10)
        with self.assertRaises(InvalidInputError):
            require_int("n", 11, 0, 10)

    def test_require_finite_number_rejects_nan_inf(self):
        for bad in (float("nan"), float("inf"), float("-inf"), True, "1.5", None):
            with self.assertRaises(InvalidInputError, msg=f"应拒绝 {bad!r}"):
                require_finite_number("x", bad)
        self.assertEqual(require_finite_number("x", 1.5), 1.5)
        self.assertEqual(require_finite_number("x", 3), 3)

    def test_require_bytes(self):
        for bad in ("abc", 12, None, [1, 2]):
            with self.assertRaises(InvalidInputError, msg=f"应拒绝 {bad!r}"):
                require_bytes("d", bad)
        self.assertEqual(require_bytes("d", bytearray(b"ab")), b"ab")
        with self.assertRaises(InvalidInputError):
            require_bytes("d", b"", min_len=1)
        with self.assertRaises(InvalidInputError):
            require_bytes("d", b"x" * 33, max_len=32)


class TestDeviceInputValidation(unittest.TestCase):
    def test_max_erases_validation(self):
        for bad in (True, -1, 2.5, "8", float("inf")):
            with self.assertRaises(InvalidInputError, msg=f"应拒绝 {bad!r}"):
                RawNand(max_erases=bad)
        RawNand(max_erases=0)  # 0 合法：任何擦除都会触发磨损拒绝

    def test_program_validation(self):
        dev = RawNand(max_erases=4)
        with self.assertRaises(InvalidInputError):
            dev.program(16, 0, b"x")
        with self.assertRaises(InvalidInputError):
            dev.program(0, 8, b"x")
        with self.assertRaises(InvalidInputError):
            dev.program(True, 0, b"x")
        with self.assertRaises(InvalidInputError):
            dev.program(0, 0, b"")
        with self.assertRaises(InvalidInputError):
            dev.program(0, 0, b"x" * 33)
        with self.assertRaises(InvalidInputError):
            dev.program(0, 0, "str")
        dev.program(0, 0, b"x")
        with self.assertRaises(InvalidInputError, msg="页禁止重写"):
            dev.program(0, 0, b"y")

    def test_crash_plan_validation(self):
        dev = RawNand(max_erases=4)
        for bad in ([0], [-1], [1.5], [True], 3, "12"):
            with self.assertRaises(InvalidInputError, msg=f"应拒绝 {bad!r}"):
                dev.set_crash_plan(bad)
        dev.set_crash_plan([1, 2])
        dev.set_crash_plan(None)


class TestFTLInputValidation(unittest.TestCase):
    def setUp(self):
        self.ftl = FTL(RawNand(max_erases=4))

    def test_write_lpn_validation(self):
        for bad in (True, -1, 24, 100, 0.0, "0"):
            with self.assertRaises(InvalidInputError, msg=f"应拒绝 lpn={bad!r}"):
                self.ftl.write(bad, b"data")

    def test_write_data_validation(self):
        for bad in (b"", b"x" * 33, "str", 12, None):
            with self.assertRaises(InvalidInputError, msg=f"应拒绝 data={bad!r}"):
                self.ftl.write(0, bad)

    def test_read_lpn_validation(self):
        for bad in (True, -1, 24, 0.0):
            with self.assertRaises(InvalidInputError):
                self.ftl.read(bad)

    def test_gc_budget_validation(self):
        for bad in (True, -1, 1.5, "4"):
            with self.assertRaises(InvalidInputError):
                FTL(RawNand(max_erases=4), gc_budget=bad)
            with self.assertRaises(InvalidInputError):
                self.ftl.write(0, b"d", gc_budget=bad)
            with self.assertRaises(InvalidInputError):
                self.ftl.gc_collect(gc_budget=bad)

    def test_constructor_device_type(self):
        with self.assertRaises(InvalidInputError):
            FTL(object())
        with self.assertRaises(InvalidInputError):
            FTL(None)


if __name__ == "__main__":
    unittest.main()
