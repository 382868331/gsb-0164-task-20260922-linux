"""The log-structured flash translation layer itself.

Visibility rule
---------------
A logical write programs its data page(s) first and the CRC-protected
commit page last.  The in-memory map is updated only after the commit
page is durably programmed; recovery therefore never exposes a mapping
whose commit was torn by power loss.

Recovery rule
-------------
Every non-blank page is scanned once at mount.  Commit frames are
folded in ascending ``(sequence, physical page)`` order; a commit is
accepted only when its frame CRC is good and every data page it names
is readable.  Torn/orphan frames are ignored, so the last *complete*
commit sequence defines the visible logical image.  Sequence numbers
name each commit (deltas), so a missing commit simply leaves its
changes invisible while later complete commits still fold correctly.

Garbage-collection rule
----------------------
One fully-blank healthy block is kept in reserve as relocation
workspace whenever reclaimable blocks exist.  A victim block is erased
only after all of its live data pages have been copied elsewhere and
the relocation commit(s) are durable.  Copies may be packed across any
healthy blocks other than the victim; before programming a copy the
layer first reuses an already-written orphan data frame whose payload
matches, so a relocation interrupted by power loss is resumed
idempotently.  Whatever interruption point is reached, the committed
map and the readable media agree: uncommitted copies are orphans,
committed copies make the old page stale garbage, and the victim is
erased only when nothing live points into it.

Wear rule
--------
``max_erases_per_block`` bounds each block's erase count; a block at
the limit is never chosen as a GC victim, though its remaining blank
pages stay programmable.  Bad blocks (factory declared, containing a
declared-bad page, or retired after a refused erase) are quarantined
and never allocated or erased.

Victim search is exhaustive over ``BLOCKS`` blocks; a raised
:class:`StorageFullError` / :class:`WearLimitError` is therefore a
proof that no legal move exists in the current state -- never a
"search budget spent" timeout.  Wear limit (reclaimable blocks exist
but all are pinned at their erase ceiling) and true exhaustion (fully
live media, no safe relocation workspace) are reported separately.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .encoding import (
    BLOCKS,
    MAX_LOGICAL_PAGES,
    MAX_PAYLOAD,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    TOTAL_PAGES,
    FrameError,
    decode_frame,
    encode_commit,
    encode_data,
)
from .errors import (
    BadBlockError,
    FTLInternalError,
    FTLValidationError,
    FormatMismatchError,
    PhysicalMediaError,
    PowerLossError,
    SequenceExhaustedError,
    StorageFullError,
    UnmappedLogicalPageError,
    WearLimitError,
    require_bytes,
    require_int,
)

if TYPE_CHECKING:
    from .flash import RawAction, RawFlash

ENTRIES_PER_COMMIT = 4
_SEQ_MAX = (1 << 24) - 1


@dataclass(frozen=True)
class LogicalEvent:
    """One logical-level action visible to the FTL client."""

    kind: str  # "write" | "read"
    sequence: int | None  # commit sequence for writes; None for reads
    entries: tuple[tuple[int, int, int], ...]  # (lpn, ppn, payload_length)

    def __str__(self) -> str:
        if self.kind == "write":
            body = ", ".join(
                f"lpn{l}->ppn{p}({n}B)" for l, p, n in self.entries
            )
            return f"write seq={self.sequence}: {body}"
        l, p, n = self.entries[0]
        return f"read lpn{l} -> ppn{p} ({n}B)"


@dataclass(frozen=True)
class PhysicalEvent:
    """Public view of one durable physical action."""

    kind: str  # "program" | "erase"
    block: int
    page: int
    ppn: int
    ordinal: int

    @classmethod
    def _from_action(cls, action: "RawAction") -> "PhysicalEvent":
        return cls(action.kind, action.block, action.page, action.ppn, action.ordinal)

    def __str__(self) -> str:
        if self.kind == "erase":
            return f"erase(block={self.block})"
        return f"program(ppn={self.ppn})"


class FTL:
    """Log-structured mapping layer over a :class:`~ftl_core.flash.RawFlash`."""

    def __init__(self, flash: "RawFlash", max_erases_per_block: int) -> None:
        from .flash import RawFlash

        if not isinstance(flash, RawFlash):
            raise FTLValidationError(
                f"flash must be a RawFlash instance (got {type(flash).__name__})"
            )
        if flash.geometry != (BLOCKS, PAGES_PER_BLOCK, PAGE_SIZE):
            raise FormatMismatchError(
                f"flash geometry {flash.geometry} != required "
                f"{(BLOCKS, PAGES_PER_BLOCK, PAGE_SIZE)}"
            )
        require_int(
            max_erases_per_block,
            "max_erases_per_block",
            minimum=0,
            maximum=2**31 - 1,
        )
        self.flash = flash
        self.max_erases_per_block = max_erases_per_block
        self.logical_events: list[LogicalEvent] = []
        self._alive = True
        # ppn -> "data" | "commit" | "torn"
        self._page_kind: dict[int, str] = {}
        # ppn -> payload for every readable data frame on the medium
        self._data_payloads: dict[int, bytes] = {}
        # commit ppn -> its validated (lpn, ppn) entries
        self._commit_entries: dict[int, list[tuple[int, int]]] = {}
        self._map: dict[int, int] = {}
        self._next_sequence = 0
        self._worn_pinned_candidates = 0
        self._quarantine_declared_bad_pages()
        self._mount()
        # Power may have dropped in the middle of garbage collection,
        # leaving the relocation workspace partially consumed. Finish any
        # such pass so the "one fully-blank spare block" invariant the
        # allocator relies on holds when the device accepts writes.
        self._recover_spare()

    def _recover_spare(self) -> None:
        """Restore a fully-blank spare block after an interrupted GC.

        One successful victim erase produces a fully blank block; each
        relocation is idempotent (durable orphan copies are reused), so
        repeating this at every boot is safe even if power drops again.
        """

        for _ in range(TOTAL_PAGES + 1):
            counts = self._blank_counts()
            if len(counts) < 2 or any(
                n == PAGES_PER_BLOCK for n in counts.values()
            ):
                return
            plan = self._choose_victim()
            if plan is None:
                return  # genuinely nothing safely reclaimable
            victim, _need = plan
            self._run_gc(victim)

    # ------------------------------------------------------------------ mount

    def _quarantine_declared_bad_pages(self) -> None:
        """A block containing any declared-bad page is replacement granularity.

        Retiring whole blocks at mount means the allocator can never
        pick such a block; the only fault a live operation observes is
        power loss *after* a successful program/erase (plus a refused
        erase of a block the medium later marks bad).
        """

        for ppn in self.flash.declared_bad_pages:
            self.flash.mark_block_bad(ppn // PAGES_PER_BLOCK)

    def _mount(self) -> None:
        data_pages: set[int] = set()
        commits: list[tuple[int, int, list[tuple[int, int]]]] = []
        for ppn in range(TOTAL_PAGES):
            raw = self.flash.read_raw(ppn)
            if all(b == 0xFF for b in raw):
                continue
            try:
                kind, value = decode_frame(raw)
            except FrameError:
                # Torn program (power can drop mid-write in the general
                # NAND model): ignore for mapping, keep it as reclaimable.
                self._page_kind[ppn] = "torn"
                continue
            if kind == "data":
                assert isinstance(value, bytes)
                data_pages.add(ppn)
                self._data_payloads[ppn] = value
                self._page_kind[ppn] = "data"
            else:
                seq, entries = value  # type: ignore[misc]
                valid_entries = [
                    (lpn, p)
                    for lpn, p in entries
                    if 0 <= lpn < MAX_LOGICAL_PAGES and 0 <= p < TOTAL_PAGES
                ]
                commits.append((ppn, seq, valid_entries))
                self._page_kind[ppn] = "commit"
                self._commit_entries[ppn] = valid_entries

        # Every CRC-good commit frame was programmed last, after all of
        # its data frames; under the atomic-page-program model a CRC-good
        # commit therefore always had its data present.  A referenced data
        # page can be unreadable now only when a *later* commit already
        # repointed that lpn and garbage collection later erased the old
        # page.  So for each lpn we keep the newest commit (sequence then
        # physical order) whose data page is still readable; older or
        # stale entries never win.  Torn commit frames never made the
        # list at all, preserving whole-batch atomicity.
        self._map: dict[int, int] = {}
        self._map_origin: dict[int, int] = {}
        latest_seq = -1
        for commit_ppn, seq, entries in sorted(
            commits, key=lambda c: (c[1], c[0])
        ):
            latest_seq = max(latest_seq, seq)
            for lpn, src in entries:
                if src in data_pages:
                    self._map[lpn] = src
                    self._map_origin[lpn] = commit_ppn
        self._next_sequence = latest_seq + 1

    # ------------------------------------------------------------- small utils

    def _check_alive(self) -> None:
        if not self._alive:
            raise PowerLossError(
                "this FTL instance lost power; mount the RawFlash again with "
                "FTL(flash, max_erases_per_block) to recover and continue"
            )

    @staticmethod
    def _validate_lpn(lpn: Any) -> int:
        return require_int(lpn, "lpn", minimum=0, maximum=MAX_LOGICAL_PAGES - 1)

    def _healthy_blocks(self) -> set[int]:
        return set(range(BLOCKS)) - set(self.flash.quarantined_blocks)

    def _blank_counts(self) -> dict[int, int]:
        counts = {b: 0 for b in self._healthy_blocks()}
        for ppn in range(TOTAL_PAGES):
            block = ppn // PAGES_PER_BLOCK
            if block in counts and self.flash.is_blank(ppn):
                counts[block] += 1
        return counts

    def _blank_ppns(self, blocks: set[int] | None = None) -> list[int]:
        allowed = self._healthy_blocks() if blocks is None else blocks
        return [
            ppn
            for ppn in range(TOTAL_PAGES)
            if ppn // PAGES_PER_BLOCK in allowed and self.flash.is_blank(ppn)
        ]

    # ------------------------------------------------------------- inspection

    @property
    def mapping(self) -> dict[int, int]:
        """Committed ``lpn -> ppn`` map (a defensive copy)."""

        return dict(self._map)

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    @property
    def physical_events(self) -> list[PhysicalEvent]:
        """All durable physical actions, in program/erase order."""

        return [PhysicalEvent._from_action(a) for a in self.flash.action_log]

    @property
    def alive(self) -> bool:
        return self._alive

    def block_view(self) -> list[dict[str, Any]]:
        """Classify every physical page of every block."""

        live_ppns = set(self._map.values())
        bad = self.flash.quarantined_blocks
        view: list[dict[str, Any]] = []
        for block in range(BLOCKS):
            base = block * PAGES_PER_BLOCK
            counts = {"blank": 0, "live": 0, "stale_data": 0, "commit": 0, "torn": 0}
            for ppn in range(base, base + PAGES_PER_BLOCK):
                if self.flash.is_blank(ppn):
                    counts["blank"] += 1
                elif ppn in live_ppns:
                    counts["live"] += 1
                elif self._page_kind.get(ppn) == "data":
                    counts["stale_data"] += 1
                elif self._page_kind.get(ppn) == "commit":
                    counts["commit"] += 1
                else:
                    counts["torn"] += 1
            erases = self.flash.erase_counts[block]
            view.append(
                {
                    "block": block,
                    "bad": block in bad,
                    "erase_count": erases,
                    "at_wear_limit": erases >= self.max_erases_per_block,
                    **counts,
                }
            )
        return view

    def capacity(self) -> dict[str, Any]:
        """Remaining-capacity accounting.

        ``free_physical_pages``  : blank pages programmable right now on
          non-bad blocks, *including* the reserved GC spare block.
        ``free_without_reserve`` : pages usable by ordinary writes while
          keeping one fully-blank block as relocation workspace.
        ``gc_reserve_pages``     : size of that reserve (0 or 8).
        ``garbage_pages``        : non-live occupied pages (stale data,
          commit records, torn frames) on healthy blocks below the wear
          ceiling.  This is *occupancy*, not a promise of profitable
          reclamation: a block whose live mapping records force a
          full-size re-assertion may yield no net pages even though it
          holds garbage.  ``profitable_gc_blocks`` says whether at least
          one block currently yields strictly positive net pages.
        ``stranded_pages``       : pages GC can never recover (bad blocks;
          occupied pages on wear-pinned blocks).
        """

        view = self.block_view()
        free = garbage = stranded = 0
        spares = 0
        for b in view:
            occupied = b["stale_data"] + b["commit"] + b["torn"]
            if b["bad"]:
                stranded += PAGES_PER_BLOCK
                continue
            free += b["blank"]
            if b["blank"] == PAGES_PER_BLOCK:
                spares += 1
            if b["at_wear_limit"]:
                stranded += occupied
            else:
                garbage += occupied
        reserve_blocks = 1 if spares >= 1 and garbage > 0 else 0
        reserve_pages = reserve_blocks * PAGES_PER_BLOCK
        plan = self._choose_victim()
        return {
            "free_physical_pages": free,
            "free_without_reserve": free - reserve_pages,
            "gc_reserve_pages": reserve_pages,
            "live_data_pages": len(self._map),
            "garbage_pages": garbage,
            "profitable_gc_available": plan is not None,
            "stranded_pages": stranded,
            "bad_blocks": sorted(self.flash.quarantined_blocks),
            "erase_counts": list(self.flash.erase_counts),
            "wear_headroom": [
                max(0, self.max_erases_per_block - c)
                for c in self.flash.erase_counts
            ],
            "mapped_logical_pages": len(self._map),
            "max_logical_pages": MAX_LOGICAL_PAGES,
        }

    # ----------------------------------------------------------------- reads

    def read(self, lpn: Any) -> bytes:
        """Return the last committed payload of a logical page."""

        self._check_alive()
        lpn = self._validate_lpn(lpn)
        try:
            ppn = self._map[lpn]
        except KeyError:
            raise UnmappedLogicalPageError(
                f"logical page {lpn} has never been committed"
            )
        try:
            kind, value = decode_frame(self.flash.read_raw(ppn))
        except FrameError as exc:  # map only ever names validated frames
            raise FTLInternalError(f"lpn{lpn} maps to unreadable ppn {ppn}") from exc
        if kind != "data":
            raise FTLInternalError(f"lpn{lpn} maps to non-data ppn {ppn}")
        assert isinstance(value, bytes)
        self.logical_events.append(
            LogicalEvent("read", None, ((lpn, ppn, len(value)),))
        )
        return value

    # ---------------------------------------------------------------- writes

    def write(self, lpn: Any, data: Any) -> None:
        """Commit one logical page (``data`` 0..:data:`MAX_PAYLOAD` bytes)."""

        self.write_pages({lpn: data})

    def write_pages(self, pages: Any) -> None:
        """Commit 1..4 logical pages atomically under one commit record.

        Either every page becomes visible together or none does.
        """

        self._check_alive()
        if isinstance(pages, dict):
            raw_items = list(pages.items())
        elif isinstance(pages, (list, tuple)) and all(
            isinstance(x, (list, tuple)) and len(x) == 2 for x in pages
        ):
            raw_items = list(pages)
        else:
            raise FTLValidationError(
                "pages must be a dict {lpn: bytes} or a list of (lpn, data)"
            )
        if not 1 <= len(raw_items) <= ENTRIES_PER_COMMIT:
            raise FTLValidationError(
                f"one commit carries 1..{ENTRIES_PER_COMMIT} pages "
                f"(got {len(raw_items)})"
            )
        prepared: list[tuple[int, bytes]] = []
        seen: set[int] = set()
        for raw_lpn, raw_data in raw_items:
            lpn = self._validate_lpn(raw_lpn)
            if lpn in seen:
                raise FTLValidationError(f"duplicate lpn {lpn} in one write batch")
            seen.add(lpn)
            data = require_bytes(
                raw_data, f"data for lpn {lpn}", max_length=MAX_PAYLOAD
            )
            prepared.append((lpn, data))
        prepared.sort(key=lambda item: item[0])

        required = len(prepared) + 1
        self._ensure_space(required)
        # Data frames are unreachable orphans until the commit frame
        # lands; a refused program therefore leaves only reclaimable
        # garbage, never a partially visible mapping.
        try:
            self._commit_batch(prepared)
        except _QuarantinedDuringBatch as exc:
            raise BadBlockError(
                f"block {exc.block} refused the operation and was "
                "quarantined; the write is not committed (orphaned garbage "
                "pages may be reclaimed by later GC)"
            ) from exc

    def _commit_batch(self, prepared: list[tuple[int, bytes]]) -> None:
        entries: list[tuple[int, int]] = []
        for lpn, data in prepared:
            ppn = self._alloc_for_client_write()
            self._program(ppn, encode_data(data), "data")
            self._data_payloads[ppn] = data
            entries.append((lpn, ppn))
        seq = self._take_sequence()
        commit_ppn = self._alloc_for_client_write()
        self._program(commit_ppn, encode_commit(seq, entries), "commit")
        # Commit durable: the new mapping becomes visible only now.
        for lpn, ppn in entries:
            self._map[lpn] = ppn
            self._map_origin[lpn] = commit_ppn
        self._commit_entries[commit_ppn] = list(entries)
        lengths = dict(prepared)
        self.logical_events.append(
            LogicalEvent(
                "write",
                seq,
                tuple((lpn, ppn, len(lengths[lpn])) for lpn, ppn in entries),
            )
        )

    # ----------------------------------------------------------- allocation

    def _reserve_block(self, counts: dict[int, int]) -> int | None:
        """Pick the fully-blank healthy block kept as GC workspace.

        One fully-blank block is reserved unconditionally whenever at
        least two healthy blocks exist: relocation must have a place to
        land before a victim can be erased, and consuming the last blank
        block would deadlock the device with no first legal move.  The
        erased victim becomes the new reserve after each completed GC.
        Prefer an unworn block; a worn-out fully-blank block is the last
        resort (it can still hold copies, it just can never be erased).
        """

        if len(counts) < 2:
            return None
        full = [b for b, n in counts.items() if n == PAGES_PER_BLOCK]
        if not full:
            return None
        # Hold the *most worn* fully-blank block as workspace: client
        # allocation then consumes less-worn blocks first, and those get
        # erased to catch up, which levels erase counts upward.  Tie on
        # the lowest block id for deterministic behaviour.
        return min(full, key=lambda b: (-self.flash.erase_counts[b], b))

    def _pick_from(self, counts: dict[int, int], allow: set[int]) -> int:
        """Lowest blank ppn on the tightest allowed block (wear tiebreak)."""

        candidates = {b: n for b, n in counts.items() if b in allow and n > 0}
        if not candidates:
            raise StorageFullError("no blank programmable pages remain")
        partial = {b: n for b, n in candidates.items() if n < PAGES_PER_BLOCK}
        if partial:
            block = min(
                partial,
                key=lambda b: (partial[b], self.flash.erase_counts[b], b),
            )
        else:
            block = min(
                candidates,
                key=lambda b: (self.flash.erase_counts[b], b),
            )
        return min(self._blank_ppns({block}))

    def _alloc_for_client_write(self) -> int:
        """Allocate one client page, always keeping the GC spare block."""

        counts = self._blank_counts()
        reserve = self._reserve_block(counts)
        if reserve is None:
            allow = set(counts)  # fewer than 2 healthy blocks: no GC possible
        else:
            allow = set(counts) - {reserve}
        return self._pick_from(counts, allow)

    def _gc_sets(
        self, victim: int
    ) -> tuple[list[tuple[int, int]], list[int]]:
        """Partition the state that erasing *victim* would destroy.

        Returns ``(copy_items, assert_lpns)``:

        * ``copy_items``  : ``(lpn, data_ppn)`` whose current data frame
          sits in the victim -- the bytes must be copied out.
        * ``assert_lpns`` : lpns whose current mapping was last asserted
          by a commit frame inside the victim, while their data already
          lives elsewhere -- the mapping entry must be re-asserted in a
          new commit even though no data copy is needed.
        """

        rng = set(range(victim * PAGES_PER_BLOCK, (victim + 1) * PAGES_PER_BLOCK))
        copy_items = sorted(
            (lpn, ppn) for lpn, ppn in self._map.items() if ppn in rng
        )
        copy_lpns = {lpn for lpn, _ in copy_items}
        assert_lpns = sorted(
            lpn
            for lpn, commit_ppn in self._map_origin.items()
            if commit_ppn in rng and lpn not in copy_lpns
        )
        return copy_items, assert_lpns

    @staticmethod
    def _commit_count(entries: int) -> int:
        return math.ceil(entries / ENTRIES_PER_COMMIT) if entries else 0

    def _find_orphan(
        self, payload: bytes, victim: int, taken: set[int]
    ) -> int | None:
        """Find a durable orphan copy of *payload* usable for this GC pass."""

        used = set(self._map.values()) | taken
        for ppn, candidate in self._data_payloads.items():
            if ppn in used:
                continue
            block = ppn // PAGES_PER_BLOCK
            if block == victim or block in self.flash.quarantined_blocks:
                continue
            if candidate == payload:
                return ppn
        return None

    def _orphan_plan(
        self, copy_items: list[tuple[int, int]], victim: int
    ) -> dict[int, int]:
        """Assign reusable orphan frames to live lpns (one each)."""

        taken: set[int] = set()
        plan: dict[int, int] = {}
        for lpn, old_ppn in copy_items:
            payload = self._data_payloads.get(old_ppn)
            if payload is None:
                continue
            orphan = self._find_orphan(payload, victim, taken)
            if orphan is not None:
                plan[lpn] = orphan
                taken.add(orphan)
        return plan

    @staticmethod
    def _commit_count(entries: int) -> int:
        return math.ceil(entries / ENTRIES_PER_COMMIT) if entries else 0

    def _relocation_need(
        self,
        copy_items: list[tuple[int, int]],
        assert_lpns: list[int],
        victim: int,
        orphan_plan: dict[int, int] | None = None,
    ) -> tuple[int, int, int]:
        """Pages a relocation must freshly program, and its frame totals.

        Returns ``(fresh_pages, data_copies, commit_pages)``.  Durable
        orphan frames reused for a live page cost no new program.
        """

        if orphan_plan is None:
            orphan_plan = self._orphan_plan(copy_items, victim)
        commits = self._commit_count(
            len(copy_items) + len(assert_lpns)
        )
        fresh_data = len(copy_items) - len(orphan_plan)
        return fresh_data + commits, fresh_data, commits

    def _gc_target(
        self,
        victim: int,
        copy_items: list[tuple[int, int]],
        orphan_plan: dict[int, int],
        fresh_pages: int,
    ) -> int | None:
        """Pick the single block receiving every new GC frame.

        Returns ``None`` when *fresh_pages* is zero (nothing to program;
        the victim can be erased directly).  Resume case first: a
        partially filled block holding orphan copies from the interrupted
        attempt is extended.  Otherwise the fully blank reserved
        workspace is used.  The target must have room for every frame so
        a pass never spills across blocks once begun.
        """

        if fresh_pages == 0:
            return None
        counts = self._blank_counts()
        # Orphan-bearing blocks (the interrupted GC's half-used target).
        score: dict[int, int] = {}
        for ppn in orphan_plan.values():
            b = ppn // PAGES_PER_BLOCK
            score[b] = score.get(b, 0) + 1
        candidates = [
            b for b in score
            if b != victim and counts.get(b, 0) >= fresh_pages
        ]
        if candidates:
            return min(candidates, key=lambda b: (-score[b], b))
        full = [
            b for b, n in counts.items()
            if b != victim and n >= fresh_pages
            and (n == PAGES_PER_BLOCK or b in score)
        ]
        if full:
            # Consume the most-worn blank block first (matches the
            # reserve policy) so less-worn blocks catch up via erases.
            return min(full, key=lambda b: (-self.flash.erase_counts[b], b))
        return None

    def _ensure_space(self, required: int) -> None:
        # Each chosen victim yields strictly positive net blank pages;
        # erasing it also restores the fully-blank spare block. Bound the
        # loop by TOTAL_PAGES rounds without cutting off any provably
        # feasible relocation chain.
        for _ in range(TOTAL_PAGES + 1):
            counts = self._blank_counts()
            reserve = self._reserve_block(counts)
            if reserve is None:
                usable = sum(counts.values())
            else:
                usable = sum(counts.values()) - PAGES_PER_BLOCK
            if usable >= required:
                return
            plan = self._choose_victim()
            if plan is None:
                worn_pinned = self._worn_pinned_candidates
                if worn_pinned:
                    raise WearLimitError(
                        f"{worn_pinned} reclaimable block(s) exist but every "
                        f"candidate is already at the erase limit "
                        f"({self.max_erases_per_block})"
                    )
                raise StorageFullError(
                    "device is full and no block is reclaimable: occupied "
                    "blocks are fully live or lack safe relocation space, "
                    "and bad blocks are never reused"
                )
            victim, _need = plan
            self._run_gc(victim)
        raise FTLInternalError("GC allocation loop did not converge")

    def _choose_victim(self) -> tuple[int, int] | None:
        """Exhaustively pick the cheapest safely reclaimable victim.

        Returns ``(block, fresh_pages_to_program)``.  A block qualifies
        only when it is healthy, below the wear ceiling, yields strictly
        positive net blank pages once its live data and live mapping
        records are relocated, and a single target block (an interrupted
        GC's half-filled target, or the reserved fully-blank block) can
        hold every new frame.  Blocks skipped only because of the wear
        ceiling are counted in ``self._worn_pinned_candidates`` so true
        exhaustion and the wear limit are reported separately.
        """

        best: tuple[tuple[int, int, int], int] | None = None
        worn_pinned = 0
        counts = self._blank_counts()
        for block in range(BLOCKS):
            if block in self.flash.quarantined_blocks:
                continue
            base = block * PAGES_PER_BLOCK
            blank_in_block = sum(
                1 for ppn in range(base, base + PAGES_PER_BLOCK)
                if self.flash.is_blank(ppn)
            )
            if blank_in_block == PAGES_PER_BLOCK:
                continue  # fully free: allocator opens it directly
            copy_items, assert_lpns = self._gc_sets(block)
            entries = len(copy_items) + len(assert_lpns)
            orphan_plan = self._orphan_plan(copy_items, block)
            need, _fresh, _commits = self._relocation_need(
                copy_items, assert_lpns, block, orphan_plan
            )
            # Net gain = pages currently occupied in the block minus the
            # pages relocation reprograms. Must be strictly positive.
            occupied = PAGES_PER_BLOCK - blank_in_block
            if occupied - need <= 0:
                continue
            if self.flash.erase_counts[block] >= self.max_erases_per_block:
                worn_pinned += 1
                continue
            target = self._gc_target(
                block,
                copy_items,
                orphan_plan,
                need,
            )
            # need == 0 means the block holds nothing live: it is pure
            # garbage and may be erased with no relocation target at all.
            if need > 0 and target is None:
                continue  # no single-block relocation workspace
            key = (entries, self.flash.erase_counts[block], block)
            if best is None or key < best[0]:
                best = (key, need)
        self._worn_pinned_candidates = worn_pinned
        if best is None:
            return None
        key, need = best
        return key[2], need

    def _run_gc(self, victim: int) -> None:
        """Relocate everything erasing *victim* would destroy, then erase it.

        Every newly programmed frame of one relocation lands in a single
        target block (extending an interrupted attempt's orphan target
        when one exists).  The pass is idempotent across power loss:
        durable orphan copies are reused, and mapping records whose
        commit page sits in the victim are re-asserted even when their
        data already lives elsewhere.
        """

        copy_items, assert_lpns = self._gc_sets(victim)
        orphan_plan = self._orphan_plan(copy_items, victim)
        need, _fresh, _commits = self._relocation_need(
            copy_items, assert_lpns, victim, orphan_plan
        )
        target = self._gc_target(victim, copy_items, orphan_plan, need)
        if target is None and need > 0:  # caller verified; defensive
            raise StorageFullError("no single-block relocation workspace")
        # Full relocation table: lpn -> data frame ppn it must map to.
        pending: list[tuple[int, int]] = []
        for lpn, old_ppn in copy_items:
            if lpn in orphan_plan:
                new_ppn = orphan_plan[lpn]  # durable orphan, no program
            else:
                kind, payload = decode_frame(self.flash.read_raw(old_ppn))
                if kind != "data":
                    raise FTLInternalError(
                        f"live lpn{lpn} maps to non-data ppn {old_ppn}"
                    )
                assert isinstance(payload, bytes)
                new_ppn = self._alloc_on(target)
                self._program(new_ppn, encode_data(payload), "data")
                self._data_payloads[new_ppn] = payload
            pending.append((lpn, new_ppn))
        # Mapping-only entries reuse their existing data frame ppn.
        for lpn in assert_lpns:
            pending.append((lpn, self._map[lpn]))
        pending.sort(key=lambda item: item[0])
        for i in range(0, len(pending), ENTRIES_PER_COMMIT):
            chunk = pending[i : i + ENTRIES_PER_COMMIT]
            seq = self._take_sequence()
            commit_ppn = self._alloc_on(target)
            self._program(commit_ppn, encode_commit(seq, chunk), "commit")
            self._commit_entries[commit_ppn] = list(chunk)
            for lpn, new_ppn in chunk:  # commit durable: repoint now
                self._map[lpn] = new_ppn
                self._map_origin[lpn] = commit_ppn
        # Every live byte and mapping entry is durably reachable before
        # the victim is allowed to be erased.
        self._erase_block(victim)

    def _alloc_on(self, block: int) -> int:
        pages = self._blank_ppns({block})
        if not pages:
            raise FTLInternalError(f"relocation target block {block} is full")
        return min(pages)

    # -------------------------------------------------------- phys primitives

    def _program(self, ppn: int, frame: bytes, kind: str) -> None:
        block = ppn // PAGES_PER_BLOCK
        try:
            self.flash.program(ppn, frame)
        except PowerLossError:
            # Frame landed, instance ends here; remount decides
            # visibility from whether the commit frame exists.
            self._alive = False
            raise
        except PhysicalMediaError as exc:
            self.flash.mark_block_bad(block)
            raise _QuarantinedDuringBatch(block) from exc
        self._page_kind[ppn] = kind

    def _erase_block(self, block: int) -> None:
        if self.flash.erase_counts[block] >= self.max_erases_per_block:
            raise WearLimitError(
                f"block {block} is already at the erase limit "
                f"({self.max_erases_per_block})"
            )
        try:
            self.flash.erase(block)
        except PowerLossError:
            self._alive = False
            raise
        except PhysicalMediaError as exc:
            self.flash.mark_block_bad(block)
            raise _QuarantinedDuringBatch(block) from exc
        base = block * PAGES_PER_BLOCK
        for ppn in range(base, base + PAGES_PER_BLOCK):
            self._page_kind.pop(ppn, None)
            self._data_payloads.pop(ppn, None)
            self._commit_entries.pop(ppn, None)

    def _take_sequence(self) -> int:
        if self._next_sequence > _SEQ_MAX:
            raise SequenceExhaustedError(
                "24-bit commit sequence space exhausted"
            )
        seq = self._next_sequence
        self._next_sequence += 1
        return seq

    # ----------------------------------------------------------- manual GC

    def force_gc(self) -> int:
        """Reclaim one victim block now; return its block id.

        Raises :class:`StorageFullError` / :class:`WearLimitError` when
        nothing reclaimable exists.  Writes trigger GC automatically;
        this method only makes compaction observable.
        """

        self._check_alive()
        plan = self._choose_victim()
        if plan is None:
            if self._worn_pinned_candidates:
                raise WearLimitError(
                    f"all {self._worn_pinned_candidates} reclaimable block(s) "
                    f"are at the erase limit ({self.max_erases_per_block})"
                )
            raise StorageFullError("no reclaimable block")
        victim, _need = plan
        self._run_gc(victim)
        return victim


class _QuarantinedDuringBatch(Exception):
    """Internal: a physical action refused media; its block was retired."""

    def __init__(self, block: int) -> None:
        super().__init__(f"block {block} quarantined")
        self.block = block
