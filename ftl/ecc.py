"""扩展 Hamming SECDED：每个固定长度数据字独立编码。

方案（合成教学级，字节对齐）：
- 数据字 = 4 比特（半字节）。扩展 Hamming(8,4)：4 数据位 + 3 校验位
  + 1 全局校验位 = 8 比特码字，恰好 1 字节。
- 码字位编号 1..8（对应字节的 bit0..bit7）：位置 1/2/4 为 Hamming
  校验位，位置 3/5/6/7 为数据位 d0..d3，位置 8 为全局偶校验位。
- 译码分类（SECDED）：
  * 伴随式=0 且全局校验通过        → 无错（clean）；
  * 全局校验失败                   → 单比特错（含校验位自身翻转），
    伴随式非零时翻转对应位纠正，伴随式为零则错在全局校验位本身，
    数据位不受影响（corrected）；
  * 全局校验通过但伴随式非零       → 双比特错，不可纠正
    （uncorrectable），绝不改写成看似合法的数据。
- 页级：32 字节数据槽 = 64 个半字节，每个数据字独立算出 4 比特
  校验位，逐字打包为 32 字节校验码存放在该页的备用区（spare area，
  见 device.SPARE_SIZE）。译码时用数据区的数据位 + 备用区的校验位
  重组 8 比特码字再按上表分类——数据区与校验区任意一侧的单比特
  翻转都会被识别，单字双错不影响其他字的判定。
"""
from __future__ import annotations

from .device import PAGE_SIZE, SPARE_SIZE
from .validation import require_bytes, require_int

WORD_DATA_BITS = 4  # 每个数据字的数据位数
CODEWORD_BITS = 8  # 每个码字的位数（含校验位）
WORDS_PER_PAGE = PAGE_SIZE * 8 // WORD_DATA_BITS  # 64
# 位置 1..8 中数据位所在位置（1 起始）
_DATA_POSITIONS = (3, 5, 6, 7)
# 三个 Hamming 校验位各自覆盖的位置（1 起始）
_PARITY_GROUPS = ((1, 3, 5, 7), (2, 3, 6, 7), (4, 5, 6, 7))

CLEAN = "clean"
CORRECTED = "corrected"
UNCORRECTABLE = "uncorrectable"


def _bit(value, position):
    """取码字 value 中 1 起始位置 position 的比特。"""
    return (value >> (position - 1)) & 1


def encode_word(nibble):
    """把 4 比特数据字编码为 8 比特扩展 Hamming 码字。"""
    require_int("nibble", nibble, 0, 0xF)
    return combine_word(nibble, word_parity(nibble))


def word_parity(nibble):
    """数据字的 4 比特校验位：bit0..2 = Hamming 校验 p1/p2/p4，
    bit3 = 全局偶校验 p8。页级校验码只存这 4 比特。"""
    require_int("nibble", nibble, 0, 0xF)
    code = 0
    for i, pos in enumerate(_DATA_POSITIONS):
        if (nibble >> i) & 1:
            code |= 1 << (pos - 1)
    for group, parity_pos in zip(_PARITY_GROUPS, (1, 2, 4)):
        parity = 0
        for pos in group:
            if pos in (1, 2, 4):
                continue  # 校验位自身在数据位之后计算
            parity ^= _bit(code, pos)
        if parity:
            code |= 1 << (parity_pos - 1)
    overall = 0
    for pos in range(1, 8):
        overall ^= _bit(code, pos)
    # 位置 1/2/4 → bit0/1/2（位置 4 在码字中是 bit3，需移位对齐）
    parity = (code & 0b0011) | ((code >> 1) & 0b0100)
    if overall:
        parity |= 0b1000
    return parity


def combine_word(nibble, parity):
    """用数据字 + 4 比特校验位重组 8 比特码字（译码入口）。"""
    require_int("nibble", nibble, 0, 0xF)
    require_int("parity", parity, 0, 0xF)
    code = 0
    for i, pos in enumerate(_DATA_POSITIONS):
        if (nibble >> i) & 1:
            code |= 1 << (pos - 1)
    for i, pos in enumerate((1, 2, 4, 8)):
        if (parity >> i) & 1:
            code |= 1 << (pos - 1)
    return code


def decode_word(codeword):
    """译码一个 8 比特码字，返回 (状态, 数据字)。

    状态为 "clean" / "corrected" / "uncorrectable"；uncorrectable 时
    数据字为 None（绝不返回看似合法的数据）。
    """
    require_int("codeword", codeword, 0, 0xFF)
    syndrome = 0
    for bit_index, group in enumerate(_PARITY_GROUPS):
        parity = 0
        for pos in group:
            parity ^= _bit(codeword, pos)
        syndrome |= parity << bit_index
    overall = 0
    for pos in range(1, 9):
        overall ^= _bit(codeword, pos)
    if syndrome == 0 and overall == 0:
        fixed = codeword
        status = CLEAN
    elif overall == 1:
        # 单比特错：伴随式指向出错位置；伴随式为 0 则错在全局校验位本身
        fixed = codeword if syndrome == 0 else codeword ^ (1 << (syndrome - 1))
        status = CORRECTED
    else:
        return UNCORRECTABLE, None
    nibble = 0
    for i, pos in enumerate(_DATA_POSITIONS):
        nibble |= _bit(fixed, pos) << i
    return status, nibble


def encode_page(slot):
    """把 32 字节数据槽编码为 32 字节页级校验码（存入备用区）。

    每个数据字（半字节）独立编码，只存 4 比特校验位：字节 i 的
    低半字节校验 → spare[i] 低半字节，高半字节校验 → spare[i] 高半字节。
    """
    slot = require_bytes("slot", slot, min_len=PAGE_SIZE, max_len=PAGE_SIZE)
    spare = bytearray(SPARE_SIZE)
    for i, byte in enumerate(slot):
        spare[i] = word_parity(byte & 0xF) | (word_parity(byte >> 4) << 4)
    return bytes(spare)


def decode_page(slot, spare):
    """页级译码：返回 (状态, 数据槽)。

    状态聚合 64 个数据字的判定：全部无错 → "clean"；至少一个字被
    纠正且无可纠正失败 → "corrected"；任一字双比特错 →
    "uncorrectable"（数据槽为 None，绝不改写为看似合法的数据）。
    """
    slot = require_bytes("slot", slot, min_len=PAGE_SIZE, max_len=PAGE_SIZE)
    spare = require_bytes("spare", spare, min_len=SPARE_SIZE, max_len=SPARE_SIZE)
    out = bytearray(PAGE_SIZE)
    status = CLEAN
    for i in range(PAGE_SIZE):
        # 数据区数据位 + 备用区校验位重组码字：任一侧翻转都被识别
        lo_status, lo = decode_word(combine_word(slot[i] & 0xF, spare[i] & 0xF))
        hi_status, hi = decode_word(combine_word(slot[i] >> 4, spare[i] >> 4))
        if lo_status == UNCORRECTABLE or hi_status == UNCORRECTABLE:
            return UNCORRECTABLE, None
        if lo_status == CORRECTED or hi_status == CORRECTED:
            status = CORRECTED
        out[i] = lo | (hi << 4)
    return status, bytes(out)
