"""扩展 Hamming SECDED 校验：每个 8 位数据字独立编码为 13 位码字。

码结构（扩展 Hamming(13,8) SECDED）：
- 码字位编号 1..13；位 1/2/4/8 为 Hamming 校验位（偶校验），
  位 13 为覆盖位 1..12 的整体偶校验位；
- 数据位依次放在位 3,5,6,7,9,10,11,12（8 位数据字）。

分类方程（读出时独立按校验方程判定）：
- 综合校验子 S = 位 1..12 中所有为 1 的位的位号异或；
- 整体校验 P = 全部 13 位的奇偶。
  - P=0 且 S=0   -> 无错（clean）；
  - P=1 且 S!=0  -> 位 S 单比特翻转（数据位或校验位），纠正后可用；
  - P=1 且 S=0   -> 整体校验位（位 13）自身翻转，数据不变，纠正；
  - P=0 且 S!=0  -> 双比特错误，不可纠正（绝不改成看似合法的数据）。

存储格式：每个 13 位码字按小端存入 2 字节（高 3 位恒为 0），
因此一页 32 字节恰好容纳 16 个数据字（16 字节用户数据）。
"""
from __future__ import annotations

from .errors import InvalidInputError
from .validation import require_bytes, require_int

DATA_BITS = 8
CODE_BITS = 13
CODE_BYTES = 2  # 13 位码字占 2 字节
WORDS_PER_PAGE = 16  # 32 字节页 / 2 字节码字
PAGE_DATA_BYTES = WORDS_PER_PAGE  # 每页用户数据上限 16 字节
PAD_BYTE = 0xFF

# 数据位在码字中的位置（位号 1..13，跳过校验位 1,2,4,8 与整体校验位 13）
DATA_POSITIONS = (3, 5, 6, 7, 9, 10, 11, 12)
# 校验位位置（位 1,2,4,8 为 Hamming 校验；位 13 为整体校验）
HAMMING_PARITY_POSITIONS = (1, 2, 4, 8)
OVERALL_PARITY_POSITION = 13

CLEAN = "clean"
CORRECTED = "corrected"
UNCORRECTABLE = "uncorrectable"


def _check_data_word(word):
    return require_int("数据字", word, 0, (1 << DATA_BITS) - 1)


def _check_codeword(cw):
    cw = require_int("码字", cw, 0, (1 << (CODE_BYTES * 8)) - 1)
    if cw >> CODE_BITS:
        raise InvalidInputError(f"码字 {cw:#x} 超出 {CODE_BITS} 位（高 3 位必须为零）")
    return cw


def _bit(cw, pos):
    return (cw >> (pos - 1)) & 1


def encode_word(word):
    """把 8 位数据字编码为 13 位扩展 Hamming 码字（返回 int）。"""
    word = _check_data_word(word)
    cw = 0
    for i, pos in enumerate(DATA_POSITIONS):
        if (word >> i) & 1:
            cw |= 1 << (pos - 1)
    for p in HAMMING_PARITY_POSITIONS:
        parity = 0
        for pos in range(1, OVERALL_PARITY_POSITION):
            if pos & p:
                parity ^= _bit(cw, pos)
        if parity:
            cw |= 1 << (p - 1)
    overall = 0
    for pos in range(1, OVERALL_PARITY_POSITION):
        overall ^= _bit(cw, pos)
    if overall:
        cw |= 1 << (OVERALL_PARITY_POSITION - 1)
    return cw


def syndrome(cw):
    """独立校验方程：返回 (综合校验子 S, 整体奇偶 P)。"""
    cw = _check_codeword(cw)
    s = 0
    for pos in range(1, OVERALL_PARITY_POSITION):
        if _bit(cw, pos):
            s ^= pos
    p = 0
    for pos in range(1, CODE_BITS + 1):
        p ^= _bit(cw, pos)
    return s, p


def classify(cw):
    """按校验方程分类：CLEAN / CORRECTED / UNCORRECTABLE。"""
    s, p = syndrome(cw)
    if p == 0 and s == 0:
        return CLEAN
    if p == 1:
        return CORRECTED  # S!=0 时错位在 S；S==0 时整体校验位自身翻转
    return UNCORRECTABLE


def decode_word(cw):
    """解码 13 位码字，返回 (状态, 数据字或 None)。

    单比特错误（含校验位自身翻转）被纠正；双比特错误明确判为
    不可纠正，绝不猜测成某个看似合法的数据。
    """
    status = classify(cw)
    if status == UNCORRECTABLE:
        return UNCORRECTABLE, None
    s, _ = syndrome(cw)
    if s:
        cw ^= 1 << (s - 1)  # 纠正位 S（可能是数据位或 Hamming 校验位）
    word = 0
    for i, pos in enumerate(DATA_POSITIONS):
        if _bit(cw, pos):
            word |= 1 << i
    return status, word


def flip_bit(cw, bit_index):
    """合成位翻转（测试/演示线束）：翻转码字的第 bit_index 位（0..12）。"""
    cw = _check_codeword(cw)
    bit_index = require_int("bit_index", bit_index, 0, CODE_BITS - 1)
    return cw ^ (1 << bit_index)


def encode_page(data):
    """把 1..16 字节用户数据编码为 32 字节页内容（不足 16 字节以 0xFF 填充）。"""
    data = require_bytes("data", data, min_len=1, max_len=PAGE_DATA_BYTES)
    padded = data.ljust(PAGE_DATA_BYTES, bytes([PAD_BYTE]))
    out = bytearray()
    for byte in padded:
        out += encode_word(byte).to_bytes(CODE_BYTES, "little")
    return bytes(out)


def decode_page(page):
    """解码 32 字节页，返回 (状态, 16 字节数据或 None)。

    状态取所有码字中最严重者：任一码字不可纠正则整页不可纠正；
    否则任一码字被纠正则整页记为 corrected。
    """
    page = require_bytes("page", page, min_len=WORDS_PER_PAGE * CODE_BYTES,
                         max_len=WORDS_PER_PAGE * CODE_BYTES)
    status = CLEAN
    out = bytearray()
    for i in range(WORDS_PER_PAGE):
        cw = int.from_bytes(page[i * CODE_BYTES:(i + 1) * CODE_BYTES], "little")
        if cw >> CODE_BITS:
            return UNCORRECTABLE, None  # 保留位非零：不可能是合法码字
        word_status, word = decode_word(cw)
        if word_status == UNCORRECTABLE:
            return UNCORRECTABLE, None
        if word_status == CORRECTED:
            status = CORRECTED
        out.append(word)
    return status, bytes(out)

