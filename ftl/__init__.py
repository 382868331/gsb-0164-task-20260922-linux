"""合成闪存翻译层（FTL）：断电安全的页映射、原子事务、垃圾整理与磨损管理。

公开接口：
- 设备：RawNand, PhysicalAction 及几何常量 BLOCKS/PAGES_PER_BLOCK/PAGE_SIZE
- 核心：FTL, LOGICAL_PAGES, MAX_TXN_PAGES, encode_commit_record, decode_commit_record
- 校验：require_int, require_bytes, require_finite_number
- 错误：FTLError 及其子类（见 ftl.errors 文档）
"""
from .core import (
    FTL,
    GC_RESERVE_PAGES,
    LOGICAL_PAGES,
    MAX_TXN_PAGES,
    decode_commit_record,
    encode_commit_record,
)
from .device import (
    BLOCKS,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    TOTAL_PAGES,
    PhysicalAction,
    RawNand,
)
from .errors import (
    BadBlockError,
    BlockWornError,
    FTLError,
    GCBudgetExhaustedError,
    InvalidInputError,
    OutOfSpaceError,
    PowerLossError,
)
from .validation import require_bytes, require_finite_number, require_int

__all__ = [
    "FTL",
    "RawNand",
    "PhysicalAction",
    "LOGICAL_PAGES",
    "GC_RESERVE_PAGES",
    "MAX_TXN_PAGES",
    "BLOCKS",
    "PAGES_PER_BLOCK",
    "PAGE_SIZE",
    "TOTAL_PAGES",
    "encode_commit_record",
    "decode_commit_record",
    "require_int",
    "require_bytes",
    "require_finite_number",
    "FTLError",
    "InvalidInputError",
    "PowerLossError",
    "OutOfSpaceError",
    "GCBudgetExhaustedError",
    "BadBlockError",
    "BlockWornError",
]
