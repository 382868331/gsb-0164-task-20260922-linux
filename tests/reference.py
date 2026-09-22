"""Independent reference models used only by the test-suite.

Nothing in this module imports ``ftl_core``: expected values are
derived from the problem contract with plain dictionaries and
arithmetic, so a bug shared with the implementation cannot hide in the
oracle.  Round-trip checks against the real core are used only as a
supplement on top of these independent expectations.
"""

from __future__ import annotations

# Contract constants duplicated from the specification (not imported).
BLOCKS = 16
PAGES_PER_BLOCK = 8
PAGE_SIZE = 32
ENTRIES_PER_COMMIT = 4
# data frame = 1 tag + 4 CRC + 1 length + payload
MAX_PAYLOAD = PAGE_SIZE - 5 - 1  # 26


def programs_for_batch(num_pages: int) -> int:
    """Physical page-programs one committed batch costs: data + commit."""

    return num_pages + 1


def expected_prefix(trace: list[dict[int, bytes]], programs_landed: int) -> dict[int, bytes]:
    """Logical image after exactly *programs_landed* page programs.

    Each batch is visible only once all of its data pages AND its commit
    page have landed, so a batch of *m* pages contributes atomically at
    program count >= previous + (m + 1).  A torn batch contributes
    nothing.
    """

    image: dict[int, bytes] = {}
    spent = 0
    for batch in trace:
        cost = programs_for_batch(len(batch))
        if spent + cost <= programs_landed:
            image.update(batch)
            spent += cost
        else:
            break
    return image


def final_image(writes: list[tuple[int, bytes]]) -> dict[int, bytes]:
    """Last-writer-wins image of a sequence of single-page writes."""

    image: dict[int, bytes] = {}
    for lpn, data in writes:
        image[lpn] = data
    return image


def rewrite_sequence(count: int, num_lpns: int) -> list[tuple[int, bytes]]:
    """Deterministic rewrite workload used by several tests.

    Payload of write *i* is three bytes derived only from *i*; the
    formula is intentionally simple and independent of the core.
    """

    return [
        (i % num_lpns, bytes((65 + i % 26, (i * 7) & 0xFF, (i * 13) & 0xFF)))
        for i in range(count)
    ]


def naive_crc32(data: bytes) -> int:
    """Reference CRC32 implemented with the documented polynomial.

    Used to cross-check the core's frame CRC without trusting whatever
    library call the core happens to use.  Standard CRC-32 (IEEE 802.3),
    polynomial 0xEDB88320 reflected, init 0xFFFFFFFF, final XOR.
    """

    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if (crc & 1) else crc >> 1
    return crc ^ 0xFFFFFFFF
