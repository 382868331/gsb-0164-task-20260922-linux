"""Raw NAND-like medium: whole-block erase, one-shot page programming.

The medium is an in-memory byte array with strict access semantics:

* Every page starts blank (all ``0xFF``).
* :meth:`RawFlash.program` can only target a fully blank page; writing
  a page twice raises :class:`~ftl_core.errors.PhysicalMediaError` and
  leaves the stored bytes untouched (the operation is rejected, not
  partially applied).
* :meth:`RawFlash.erase` resets a whole block to blank. A block may be
  registered as *bad*: erasing it raises
  :class:`~ftl_core.errors.BadBlockError`, the block keeps its old
  contents, and the FTL never allocates it again.
* Programming a page flagged bad at the page level raises
  :class:`PhysicalMediaError`; the FTL handles that by quarantining the
  whole containing block (real NAND replacement granularity is a
  block).

Deterministic fault injection supports power-loss testing:

* ``program_faults`` / ``erase_faults`` map a physical action ordinal
  (0-based, counted across that kind of action) to ``"lose_after"``:
  the action itself completes and the bytes are durably stored, but
  :class:`~ftl_core.errors.PowerLossError` is raised the instant after.
  That models "power may be lost after every page write / erase".
* ``bad_blocks`` is a set of blocks whose erase always fails.
* ``bad_pages`` is a set of physical pages whose program always fails.

The medium also keeps the authoritative *physical action log* (exactly
the durable operations in program/erase order).  Snapshot/restore work
on copies so tests can model "what if power failed here" without
touching the live device.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .encoding import BLANK_PAGE, BLOCKS, PAGE_SIZE, PAGES_PER_BLOCK
from .errors import (
    BadBlockError,
    FTLValidationError,
    PhysicalMediaError,
    PowerLossError,
    require_int,
)


@dataclass(frozen=True)
class RawAction:
    """One durable physical action, in execution order."""

    kind: str  # "program" | "erase"
    block: int
    page: int  # page index within block for program; -1 for erase
    ppn: int  # global physical page number for program; -1 for erase
    ordinal: int  # per-kind action counter (0-based)

    def __str__(self) -> str:
        if self.kind == "erase":
            return f"erase(block={self.block})"
        return f"program(ppn={self.ppn}, block={self.block}, page={self.page})"


@dataclass
class RawFlash:
    """Strict in-memory raw flash device (16 blocks x 8 pages x 32 B)."""

    blocks: int = BLOCKS
    pages_per_block: int = PAGES_PER_BLOCK
    page_size: int = PAGE_SIZE
    bad_blocks: frozenset[int] = frozenset()
    bad_pages: frozenset[int] = frozenset()
    program_faults: dict[int, str] = field(default_factory=dict)
    erase_faults: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("blocks", self.blocks),
            ("pages_per_block", self.pages_per_block),
            ("page_size", self.page_size),
        ):
            require_int(value, name, minimum=1, maximum=256)
        self.total_pages = self.blocks * self.pages_per_block
        self._pages = [bytearray(self.page_size) for _ in range(self.total_pages)]
        for page in self._pages:
            page[:] = BLANK_PAGE[: self.page_size]
        self._erase_counts = [0] * self.blocks
        self._bad_blocks: set[int] = set(self._validate_block_set(self.bad_blocks, "bad_blocks"))
        self._bad_pages: set[int] = set(self._validate_page_set(self.bad_pages, "bad_pages"))
        self._validate_faults(self.program_faults, "program_faults")
        self._validate_faults(self.erase_faults, "erase_faults")
        self._program_faults = dict(self.program_faults)
        self._erase_faults = dict(self.erase_faults)
        self._program_ordinal = 0
        self._erase_ordinal = 0
        self.action_log: list[RawAction] = []

    # -- validation helpers -------------------------------------------------

    def _validate_block_set(self, values: Iterable[int], name: str) -> list[int]:
        if not isinstance(values, (set, frozenset, list, tuple)):
            raise FTLValidationError(
                f"{name!r} must be a set/frozenset/list/tuple of ints"
            )
        result: list[int] = []
        for value in values:
            result.append(require_int(value, f"element of {name}", minimum=0, maximum=self.blocks - 1))
        return result

    def _validate_page_set(self, values: Iterable[int], name: str) -> list[int]:
        if not isinstance(values, (set, frozenset, list, tuple)):
            raise FTLValidationError(
                f"{name!r} must be a set/frozenset/list/tuple of ints"
            )
        result: list[int] = []
        for value in values:
            result.append(
                require_int(value, f"element of {name}", minimum=0, maximum=self.total_pages - 1)
            )
        return result

    @staticmethod
    def _validate_faults(faults: dict[Any, Any], name: str) -> None:
        if not isinstance(faults, dict):
            raise FTLValidationError(f"{name!r} must be a dict[int, str]")
        for key, value in faults.items():
            require_int(key, f"key of {name}", minimum=0)
            if value != "lose_after":
                raise FTLValidationError(
                    f"fault kind in {name} must be 'lose_after' (got {value!r})"
                )

    # -- geometry -----------------------------------------------------------

    def split(self, ppn: int) -> tuple[int, int]:
        """Return ``(block, page_within_block)`` for a global page id."""

        return divmod(ppn, self.pages_per_block)

    def is_blank(self, ppn: int) -> bool:
        return bytes(self._pages[ppn]) == BLANK_PAGE[: self.page_size]

    def read_raw(self, ppn: int) -> bytes:
        require_int(ppn, "ppn", minimum=0, maximum=self.total_pages - 1)
        return bytes(self._pages[ppn])

    @property
    def erase_counts(self) -> tuple[int, ...]:
        return tuple(self._erase_counts)

    @property
    def quarantined_blocks(self) -> frozenset[int]:
        """Blocks currently marked bad (factory bad + runtime-quarantined)."""

        return frozenset(self._bad_blocks)

    # -- mutating physical actions -----------------------------------------

    def program(self, ppn: int, data: bytes) -> None:
        """Program one blank page, or raise without changing anything."""

        require_int(ppn, "ppn", minimum=0, maximum=self.total_pages - 1)
        if not isinstance(data, (bytes, bytearray)):
            raise FTLValidationError("data must be bytes")
        if len(data) != self.page_size:
            raise FTLValidationError(
                f"program data must be exactly {self.page_size} bytes"
            )
        block, page = self.split(ppn)
        if block in self._bad_blocks or ppn in self._bad_pages:
            raise PhysicalMediaError(f"page {ppn} (block {block}) is bad")
        if not self.is_blank(ppn):
            raise PhysicalMediaError(f"page {ppn} is not blank")
        ordinal = self._program_ordinal
        # The program lands durably in one shot at the end of the call.
        self._pages[ppn] = bytearray(data)
        self._program_ordinal += 1
        self.action_log.append(
            RawAction("program", block, page, ppn, ordinal)
        )
        if ordinal in self._program_faults:
            raise PowerLossError(
                f"power lost immediately after program ordinal {ordinal} "
                f"(ppn={ppn})"
            )

    def erase(self, block: int) -> None:
        """Erase a whole block, honouring its per-block erase limit."""

        require_int(block, "block", minimum=0, maximum=self.blocks - 1)
        if block in self._bad_blocks:
            raise BadBlockError(f"block {block} is bad and cannot be erased")
        ordinal = self._erase_ordinal
        base = block * self.pages_per_block
        for ppn in range(base, base + self.pages_per_block):
            self._pages[ppn] = bytearray(BLANK_PAGE[: self.page_size])
        self._erase_counts[block] += 1
        self._erase_ordinal += 1
        self.action_log.append(RawAction("erase", block, -1, -1, ordinal))
        if ordinal in self._erase_faults:
            raise PowerLossError(
                f"power lost immediately after erase ordinal {ordinal} "
                f"(block={block})"
            )

    def mark_block_bad(self, block: int) -> None:
        """Quarantine a block at runtime (e.g. after a program failure)."""

        require_int(block, "block", minimum=0, maximum=self.blocks - 1)
        self._bad_blocks.add(block)

    # -- snapshotting (purely a test/demo aid; not a flash primitive) -------

    def snapshot(self) -> dict[str, Any]:
        return {
            "pages": [bytes(p) for p in self._pages],
            "erase_counts": list(self._erase_counts),
            "bad_blocks": set(self._bad_blocks),
            "bad_pages": set(self._bad_pages),
            "program_ordinal": self._program_ordinal,
            "erase_ordinal": self._erase_ordinal,
            "action_log": list(self.action_log),
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self._pages = [bytearray(p) for p in snap["pages"]]
        self._erase_counts = list(snap["erase_counts"])
        self._bad_blocks = set(snap["bad_blocks"])
        self._bad_pages = set(snap["bad_pages"])
        self._program_ordinal = snap["program_ordinal"]
        self._erase_ordinal = snap["erase_ordinal"]
        self.action_log = list(snap["action_log"])

    @property
    def geometry(self) -> tuple[int, int, int]:
        return (self.blocks, self.pages_per_block, self.page_size)

    @property
    def declared_bad_pages(self) -> frozenset[int]:
        """Pages declared bad at construction (block-replacement granularity)."""

        return frozenset(self._bad_pages)
