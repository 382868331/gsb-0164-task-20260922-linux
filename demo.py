#!/usr/bin/env python3
"""Fixed-input demonstration for the synthetic FTL module.

Run with:  python demo.py

It performs real computation (no sleeps, no pre-recorded output):

1. A normal run: deterministic logical writes/reads with overwrites,
   automatic garbage collection, physical-action log and remaining
   capacity.
2. A power-loss/restart recovery in the middle of garbage collection.
3. A boundary that is actually triggered: the per-block erase (wear)
   limit raises WearLimitError, after which committed data is still
   readable and bad/limited blocks stay out of allocation.

Everything is offline and uses only the Python 3.14 standard library.
"""

from __future__ import annotations

from ftl_core import (
    FTL,
    PowerLossError,
    RawFlash,
    StorageFullError,
    WearLimitError,
)

NUM_LPNS = 6
WRITES = 120
MAX_ERASES = 25  # generous for the normal run; a tight value is used later


def payload(index: int) -> bytes:
    """Deterministic 3-byte payload derived solely from the write index."""

    return bytes((65 + index % 26, (index * 7) & 0xFF, (index * 13) & 0xFF))


def expected_final() -> dict[int, bytes]:
    image: dict[int, bytes] = {}
    for i in range(WRITES):
        image[i % NUM_LPNS] = payload(i)
    return image


def section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def main() -> None:
    # ---------------------------------------------------------------- 1
    section("1. NORMAL RUN: logical writes/reads, GC, physical log, capacity")
    flash = RawFlash()
    ftl = FTL(flash, max_erases_per_block=MAX_ERASES)
    for i in range(WRITES):
        ftl.write(i % NUM_LPNS, payload(i))

    expect = expected_final()
    for lpn in range(NUM_LPNS):
        got = ftl.read(lpn)
        assert got == expect[lpn], (lpn, got, expect[lpn])
    print(f"Committed {WRITES} writes over {NUM_LPNS} logical pages; "
          f"final values verified for all {NUM_LPNS} pages.")
    print("Sample logical reads:")
    for lpn in (0, 1, 5):
        print(f"  logical page {lpn} -> physical {ftl.mapping[lpn]:3d}, "
              f"{ftl.read(lpn).hex()}")

    programs = [e for e in ftl.physical_events if e.kind == "program"]
    erases = [e for e in ftl.physical_events if e.kind == "erase"]
    print(f"Physical actions: {len(programs)} page programs, "
          f"{len(erases)} block erases (garbage collection ran "
          f"{len(erases)} times).")
    print("First 6 physical actions:",
          ", ".join(str(e) for e in ftl.physical_events[:6]))
    print("First 6 erase blocks   :",
          ", ".join(str(e.block) for e in erases[:6]))

    cap = ftl.capacity()
    print("Remaining capacity:")
    print(f"  free physical pages (incl. GC reserve): "
          f"{cap['free_physical_pages']}/128")
    print(f"  free usable while reserve held:         "
          f"{cap['free_without_reserve']}")
    print(f"  live logical data pages:                "
          f"{cap['live_data_pages']}")
    print(f"  garbage pages (occupancy):              "
          f"{cap['garbage_pages']}")
    print(f"  erase counts per block: "
          f"{tuple(cap['erase_counts'])}")
    spread = max(cap["erase_counts"]) - min(cap["erase_counts"])
    print(f"  wear spread (max-min erases): {spread}")

    # ---------------------------------------------------------------- 2
    section("2. POWER LOSS DURING GARBAGE COLLECTION, THEN RESTART RECOVERY")
    # Pick an erase ordinal known to occur during this workload and cut
    # power immediately after that physical erase completes.
    crash_flash = RawFlash(erase_faults={3: "lose_after"})
    crashed = FTL(crash_flash, MAX_ERASES)
    crashed_at = None
    for i in range(WRITES):
        try:
            crashed.write(i % NUM_LPNS, payload(i))
        except PowerLossError:
            crashed_at = i
            break
    assert crashed_at is not None
    print(f"Power lost immediately after physical erase ordinal 3, during "
          f"logical write index {crashed_at}.")
    print("FTL instance is now dead; re-mounting the same RawFlash...")

    recovered = FTL(crash_flash, MAX_ERASES)  # the "reboot"
    i = crashed_at
    while i < WRITES:
        try:
            recovered.write(i % NUM_LPNS, payload(i))
            i += 1
        except PowerLossError:
            recovered = FTL(crash_flash, MAX_ERASES)
    ok = all(recovered.read(l) == expect[l] for l in range(NUM_LPNS))
    print(f"Recovery + replay to completion finished; final image correct: "
          f"{ok}")
    print(f"Erase counts after recovery: {tuple(crash_flash.erase_counts)}")

    # ---------------------------------------------------------------- 3
    section("3. BOUNDARY ACTUALLY TRIGGERED: per-block wear limit")
    tight = RawFlash()
    limited = FTL(tight, max_erases_per_block=1)
    rejected_at = None
    try:
        for i in range(400):
            limited.write(i % NUM_LPNS, payload(i))
    except WearLimitError as exc:
        rejected_at = i
        print(f"Write index {i} was REJECTED with {type(exc).__name__}:")
        print(f"  {exc}")
    assert rejected_at is not None, "wear limit was supposed to be triggered"
    over_limit = [b for b, c in enumerate(tight.erase_counts) if c > 1]
    print(f"No block exceeded the limit (over-limit blocks: {over_limit}).")
    # Earlier committed data remains readable on the live instance.
    sample = limited.read(0)
    print(f"Already-committed data still readable, e.g. logical 0 = "
          f"{sample.hex()}.")
    # And a further write keeps failing deterministically (no silent fix).
    try:
        limited.write(0, b"should-not-land")
    except (WearLimitError, StorageFullError) as exc:
        print(f"A subsequent write is still rejected: {type(exc).__name__}.")
    print(f"Logical 0 unchanged after the rejected retry: "
          f"{limited.read(0) == sample}.")

    section("DEMO COMPLETE")
    print("Normal result, restart recovery, and a real rejection boundary "
          "were all computed live.")


if __name__ == "__main__":
    main()
