"""公开输入校验助手。

契约固定：
- 整数参数拒绝 bool（Python 中 bool 是 int 子类，必须显式排除）；
- 数值参数拒绝 NaN 与 Infinity；
- 字节串参数拒绝 str 等异质类型；
- 一切非法输入抛出带参数名与取值的 ``InvalidInputError``，不静默纠正。
"""
from __future__ import annotations

import math

from .errors import InvalidInputError

INT_CAP = 2**31 - 1


def require_int(name, value, lo=None, hi=None):
    """要求 ``value`` 为 int（拒绝 bool），可选闭区间 [lo, hi] 约束。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInputError(
            f"参数 {name} 必须是 int（拒绝 bool 及其他类型），得到 {value!r}"
        )
    if lo is not None and value < lo:
        raise InvalidInputError(f"参数 {name}={value} 小于下限 {lo}")
    if hi is not None and value > hi:
        raise InvalidInputError(f"参数 {name}={value} 超过上限 {hi}")
    return value


def require_finite_number(name, value):
    """要求 ``value`` 为有限数值（int/float），拒绝 bool、NaN、Infinity。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidInputError(
            f"参数 {name} 必须是数值（拒绝 bool 及其他类型），得到 {value!r}"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise InvalidInputError(f"参数 {name} 拒绝 NaN/Infinity，得到 {value!r}")
    return value


def require_bytes(name, value, *, min_len=0, max_len=None):
    """要求 ``value`` 为字节串（bytes/bytearray/memoryview），返回 bytes 副本。"""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise InvalidInputError(
            f"参数 {name} 必须是字节串（bytes-like），得到 {type(value).__name__}: {value!r}"
        )
    data = bytes(value)
    if len(data) < min_len:
        raise InvalidInputError(f"参数 {name} 长度 {len(data)} 小于下限 {min_len}")
    if max_len is not None and len(data) > max_len:
        raise InvalidInputError(f"参数 {name} 长度 {len(data)} 超过上限 {max_len}")
    return data
