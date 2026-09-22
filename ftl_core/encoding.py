"""On-flash frame encoding for the synthetic FTL.

Geometry (fixed by the task statement): 16 blocks, 8 pages per block,
32 usable bytes per page.

A physical page holds exactly one 32-byte *frame*.  Frame layout::

    byte  0       tag
    bytes 1..4    big-endian CRC32 of tag||body        (zlib.crc32)
    bytes 5..31   body (27 bytes)

Tags:

* ``0xFF`` blank page (the erased state of every byte; never programmed).
* ``0x44`` ('D') data frame:  body = 1 byte length (0..27) + payload.
* ``0x43`` ('C') commit frame: body = 3-byte big-endian sequence number
  (0 .. 2**24-1) followed by four 6-byte map entries; every entry is
  3 bytes big-endian logical page id then 3 bytes big-endian physical
  page id.  Unused slots are logical id 0xFFFFFF, which is outside the
  legal logical-page range (0..127).

A mapping commit becomes visible only when the *commit frame* itself
reads back intact (tag + CRC).  A torn program therefore never exposes
a half-written map: the data pages it refers to are simply unreachable
until the next complete commit (and get reclaimed as garbage).

24-bit sequence numbers are sufficient for the interface boundary
(128 logical pages); the writer refuses to start a commit once the
sequence space is exhausted rather than silently wrapping to an
ambiguous older value.
"""

from __future__ import annotations

import zlib

BLOCKS = 16
PAGES_PER_BLOCK = 8
PAGE_SIZE = 32
TOTAL_PAGES = BLOCKS * PAGES_PER_BLOCK  # 128

MAX_LOGICAL_PAGES = TOTAL_PAGES  # logical ids 0..127
MAX_PAYLOAD = PAGE_SIZE - 5 - 1  # 26 usable data bytes per logical page

TAG_BLANK = 0xFF
TAG_DATA = 0x44  # 'D'
TAG_COMMIT = 0x43  # 'C'

_CRC_OFFSET = 1
_CRC_LEN = 4
_BODY_OFFSET = 5
_BODY_LEN = PAGE_SIZE - _BODY_OFFSET  # 27
_ENTRY_LEN = 6
_ENTRIES_PER_COMMIT = _BODY_LEN // _ENTRY_LEN  # 4
_SEQ_BYTES = 3
_SEQ_MAX = (1 << (_SEQ_BYTES * 8)) - 1  # 16_777_215
_UNUSED_LPN = 0xFFFFFF
_ID_BYTES = 3
_ID_MAX = (1 << (_ID_BYTES * 8)) - 1  # 16_777_215

BLANK_PAGE = bytes([TAG_BLANK]) * PAGE_SIZE


class FrameError(ValueError):
    """Raised when raw page bytes cannot be decoded as a frame."""


def _crc(tag: int, body: bytes) -> bytes:
    return zlib.crc32(bytes([tag]) + body).to_bytes(_CRC_LEN, "big")


def _frame(tag: int, body: bytes) -> bytes:
    if len(body) != _BODY_LEN:
        raise FrameError(f"body must be {_BODY_LEN} bytes")
    return bytes([tag]) + _crc(tag, body) + body


def encode_data(payload: bytes) -> bytes:
    """Encode at most :data:`MAX_PAYLOAD` bytes as a data frame."""

    if len(payload) > MAX_PAYLOAD:
        raise FrameError(f"payload exceeds {MAX_PAYLOAD} bytes")
    body = bytes([len(payload)]) + payload
    body += b"\x00" * (_BODY_LEN - len(body))
    return _frame(TAG_DATA, body)


def encode_commit(sequence: int, entries: list[tuple[int, int]]) -> bytes:
    """Encode a commit frame carrying up to four mapping entries."""

    if not 0 <= sequence <= _SEQ_MAX:
        raise FrameError("sequence number out of 24-bit range")
    if len(entries) > _ENTRIES_PER_COMMIT:
        raise FrameError(f"at most {_ENTRIES_PER_COMMIT} entries per commit")
    body = sequence.to_bytes(_SEQ_BYTES, "big")
    for lpn, ppn in entries:
        if not 0 <= lpn <= _ID_MAX or not 0 <= ppn <= _ID_MAX:
            raise FrameError("page id out of 24-bit range")
        body += lpn.to_bytes(_ID_BYTES, "big") + ppn.to_bytes(_ID_BYTES, "big")
    for _ in range(_ENTRIES_PER_COMMIT - len(entries)):
        body += bytes([0xFF]) * _ENTRY_LEN
    return _frame(TAG_COMMIT, body)


def decode_frame(raw: bytes) -> tuple[str, object]:
    """Decode a programmed page.

    Returns ``("data", payload_bytes)`` or ``("commit", (seq, entries))``
    where *entries* is a list of ``(lpn, ppn)`` pairs with unused slots
    removed.  Raises :class:`FrameError` for blank or corrupt pages.
    """

    if len(raw) != PAGE_SIZE:
        raise FrameError("page is not 32 bytes")
    tag = raw[0]
    if tag == TAG_BLANK:
        raise FrameError("blank page")
    body = raw[_BODY_OFFSET:]
    expected = raw[_CRC_OFFSET:_CRC_OFFSET + _CRC_LEN]
    if _crc(tag, body) != expected:
        raise FrameError("CRC mismatch")
    if tag == TAG_DATA:
        length = body[0]
        if length > MAX_PAYLOAD:
            raise FrameError("data length field out of range")
        return "data", body[1:1 + length]
    if tag == TAG_COMMIT:
        sequence = int.from_bytes(body[:_SEQ_BYTES], "big")
        entries: list[tuple[int, int]] = []
        for i in range(_ENTRIES_PER_COMMIT):
            off = _SEQ_BYTES + i * _ENTRY_LEN
            slot = body[off:off + _ENTRY_LEN]
            lpn = int.from_bytes(slot[:_ID_BYTES], "big")
            if lpn == _UNUSED_LPN:
                continue
            ppn = int.from_bytes(slot[_ID_BYTES:], "big")
            entries.append((lpn, ppn))
        return "commit", (sequence, entries)
    raise FrameError(f"unknown tag 0x{tag:02x}")


def is_blank(raw: bytes) -> bool:
    return raw == BLANK_PAGE
