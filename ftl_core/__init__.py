"""Synthetic flash translation layer (power-safe mapping + wear management).

Public surface (see README.md for the contract):

* :class:`ftl_core.RawFlash`      -- raw 16x8x32 NAND-like medium
* :class:`ftl_core.FTL`           -- log-structured mapping layer
* :class:`ftl_core.PhysicalEvent` -- one physical program/erase action
* :class:`ftl_core.LogicalEvent`  -- one logical read/write action
* exception hierarchy under :class:`ftl_core.FTLError`
* geometry constants ``BLOCKS``, ``PAGES_PER_BLOCK``, ``PAGE_SIZE``.

Only the Python standard library is used.
"""

from .errors import (
    BadBlockError,
    FTLError,
    FTLInternalError,
    FTLValidationError,
    FormatMismatchError,
    PhysicalMediaError,
    PowerLossError,
    SequenceExhaustedError,
    StorageFullError,
    UnmappedLogicalPageError,
    WearLimitError,
)
from .flash import RawFlash
from .ftl import FTL, LogicalEvent, PhysicalEvent
from .encoding import (
    BLOCKS,
    PAGES_PER_BLOCK,
    PAGE_SIZE,
    MAX_LOGICAL_PAGES,
    MAX_PAYLOAD,
)

__all__ = [
    "RawFlash",
    "FTL",
    "PhysicalEvent",
    "LogicalEvent",
    "FTLError",
    "FTLValidationError",
    "FormatMismatchError",
    "UnmappedLogicalPageError",
    "PhysicalMediaError",
    "BadBlockError",
    "WearLimitError",
    "StorageFullError",
    "PowerLossError",
    "SequenceExhaustedError",
    "FTLInternalError",
    "BLOCKS",
    "PAGES_PER_BLOCK",
    "PAGE_SIZE",
    "MAX_LOGICAL_PAGES",
    "MAX_PAYLOAD",
]
