"""Shared test helpers (no independent expectations live here)."""

from __future__ import annotations

from ftl_core import FTL, PowerLossError, RawFlash


def drive_workload(flash: RawFlash, max_erases: int, writes, batch=None):
    """Apply a single-page write sequence, rebooting on power loss.

    On :class:`PowerLossError` the same logical write is retried after a
    fresh mount: commit atomicity guarantees the retry is safe whether
    or not the interrupted commit landed.
    """

    ftl = FTL(flash, max_erases)
    i = 0
    n = len(writes)
    while i < n:
        lpn, data = writes[i]
        try:
            ftl.write(lpn, data)
            i += 1
        except PowerLossError:
            ftl = FTL(flash, max_erases)
    return ftl


def map_is_self_consistent(ftl: FTL) -> None:
    """Assert every map entry names a distinct readable data frame."""

    from ftl_core.encoding import decode_frame

    ppns = list(ftl.mapping.values())
    assert len(ppns) == len(set(ppns)), "two logical pages share a physical page"
    for lpn, ppn in ftl.mapping.items():
        kind, _value = decode_frame(ftl.flash.read_raw(ppn))
        assert kind == "data", f"lpn{lpn} maps to a {kind} frame"
