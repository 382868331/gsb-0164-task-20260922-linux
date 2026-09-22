"""Error hierarchy and input validation helpers for the FTL module.

Error semantics
---------------
* :class:`FTLValidationError` -- caller passed an illegal argument
  (wrong type, out of range, NaN/Infinity, ``bool`` where ``int`` is
  required, data larger than a page, ...).  Raised *before* any state
  changes: a rejected call never performs a partial write.
* :class:`FormatMismatchError` -- mounted medium has wrong geometry.
* :class:`UnmappedLogicalPageError` -- reading a logical page never
  successfully committed.
* :class:`PhysicalMediaError` -- a physical action failed on the medium
  (e.g. programming a non-blank page).  Its subclass
  :class:`BadBlockError` marks a block the medium refuses to erase.
* :class:`StorageFullError` / :class:`WearLimitError` -- allocation
  proved impossible (no viable block / block's erase count is at the
  configured limit).  These mean "provably impossible now", distinct
  from a transient simulated interruption.
* :class:`PowerLossError` -- deterministic fault injection: a write or
  erase completed on the medium and power was lost immediately after
  it.  The FTL object is unusable afterwards; the partially applied
  state is exactly what a real reboot would see and re-mounting the
  same :class:`~ftl_core.flash.RawFlash` recovers it.
* :class:`FTLInternalError` -- an invariant the core itself guarantees
  was violated (indicates a bug in this library, not bad input).
"""

from __future__ import annotations

import math
from typing import Any


class FTLError(Exception):
    """Base class of every error raised by this package."""


class FTLValidationError(FTLError, ValueError):
    """Illegal caller input. Nothing was mutated before it was raised."""


class FormatMismatchError(FTLError):
    """Mounted :class:`RawFlash` does not match the configured geometry."""


class UnmappedLogicalPageError(FTLError, KeyError):
    """Logical page exists but has never been committed."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return super().__str__()


class PhysicalMediaError(FTLError, OSError):
    """A physical program/erase violated the NAND access contract."""


class BadBlockError(PhysicalMediaError):
    """The medium refused to erase a block; that block stays bad forever."""


class StorageFullError(FTLError):
    """No allocatable free block exists -- allocation is impossible."""


class WearLimitError(FTLError):
    """Every candidate block is already at its maximum erase count."""


class SequenceExhaustedError(FTLError):
    """The 24-bit commit sequence number space has been used up."""


class PowerLossError(FTLError):
    """Injected power loss right after a physical page-write/erase."""


class FTLInternalError(FTLError, AssertionError):
    """Internal invariant broken (a bug in ftl_core, not bad caller input)."""


def require_int(
    value: Any,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Validate a strictly-integer parameter.

    ``bool`` is rejected even though it is an ``int`` subclass.
    Floats (including ``1.0``) are rejected: implicit truncation would
    silently "correct" caller input.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise FTLValidationError(
            f"{name!r} must be an int (got {type(value).__name__}: {value!r})"
        )
    if minimum is not None and value < minimum:
        raise FTLValidationError(
            f"{name!r} must be >= {minimum} (got {value})"
        )
    if maximum is not None and value > maximum:
        raise FTLValidationError(
            f"{name!r} must be <= {maximum} (got {value})"
        )
    return value


def require_finite_number(value: Any, name: str) -> float:
    """Validate a real, finite numeric input (NaN / Infinity rejected)."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FTLValidationError(
            f"{name!r} must be a real number (got {type(value).__name__})"
        )
    number = float(value)
    if not math.isfinite(number):
        raise FTLValidationError(f"{name!r} must be finite (got {value!r})")
    return number


def require_bytes(value: Any, name: str, *, max_length: int) -> bytes:
    """Validate a bytes-like payload no longer than ``max_length``."""

    if isinstance(value, bytearray):
        value = bytes(value)
    if not isinstance(value, bytes):
        raise FTLValidationError(
            f"{name!r} must be bytes (got {type(value).__name__})"
        )
    if len(value) > max_length:
        raise FTLValidationError(
            f"{name!r} is {len(value)} bytes; maximum is {max_length}"
        )
    return value
