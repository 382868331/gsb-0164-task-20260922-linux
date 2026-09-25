"""合成 NAND 闪存设备模型。

几何固定（题面契约）：16 块 × 每块 8 页 × 每页 32 字节。

设备规则：
- 擦除以整块为单位，擦除后全部字节为 0xFF；
- 页只能从空白（全 0xFF）编程一次，禁止重写；
- 每次页编程 / 块擦除计为一次“物理动作”，追加进物理动作日志；
- 掉电只能发生在一次物理动作完成之后（页写/擦除本身视为原子），
  由调用方通过 crash_plan + arm_crash() 注入；
- 掉电后设备冻结，一切操作抛 PowerLossError，reboot() 后恢复
  （闪存内容、擦除计数、坏块标记、动作日志全部保留）；
- 块擦除次数达到 max_erases（输入给定）即磨损，拒绝再擦除；
- 坏块拒绝一切读/写/擦除，FTL 层永不分配。
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    BadBlockError,
    BlockWornError,
    InvalidInputError,
    PowerLossError,
)
from .validation import INT_CAP, require_bytes, require_int

BLOCKS = 16
PAGES_PER_BLOCK = 8
PAGE_SIZE = 32
TOTAL_PAGES = BLOCKS * PAGES_PER_BLOCK
BLANK_BYTE = 0xFF
BLANK_PAGE = bytes([BLANK_BYTE]) * PAGE_SIZE


@dataclass(frozen=True)
class PhysicalAction:
    """一次物理动作，构成物理动作日志的条目。

    kind: "program" | "erase"
    block: 块号；page: 页内偏移（erase 时为 None）
    purpose: FTL 传入的用途标注，如 "user-data" / "commit" /
             "trim-commit" / "gc-copy" / "gc-commit" / "gc-erase"
    """

    kind: str
    block: int
    page: int | None
    purpose: str = ""


class RawNand:
    """合成 NAND 设备。max_erases 为输入给定的磨损上限（每块允许擦除次数）。"""

    def __init__(self, max_erases, *, crash_plan=None):
        self._max_erases = require_int("max_erases", max_erases, 0, INT_CAP)
        self._cells = [bytearray(BLANK_PAGE) for _ in range(TOTAL_PAGES)]
        self._erase_counts = [0] * BLOCKS
        self._bad = [False] * BLOCKS
        self._log: list[PhysicalAction] = []
        self._crash_plan: list[int] | None = None
        self.set_crash_plan(crash_plan)
        self._armed = False
        self._crashed = False

    # ------------------------------------------------------------------
    # 崩溃注入（测试/演示线束）
    # ------------------------------------------------------------------
    def set_crash_plan(self, plan):
        """设置崩溃计划：正整数列表，第 n 个元素表示“再完成 n 次动作后掉电”。

        plan 为 None 表示不注入崩溃。元素必须是 >= 1 的 int（拒绝 bool）。
        """
        if plan is None:
            self._crash_plan = None
            return
        try:
            items = list(plan)
        except TypeError:
            raise InvalidInputError(
                f"crash_plan 必须是可迭代的正整数序列或 None，得到 {plan!r}"
            ) from None
        for x in items:
            require_int("crash_plan 元素", x, 1, INT_CAP)
        self._crash_plan = items

    def arm_crash(self):
        """武装崩溃注入：此后按 crash_plan 在动作边界掉电。"""
        self._check_open()
        self._armed = True

    def disarm_crash(self):
        self._armed = False

    def reboot(self):
        """模拟重新上电：内容/计数/坏块/日志保留，清除冻结与武装状态。"""
        self._crashed = False
        self._armed = False

    @property
    def crashed(self):
        return self._crashed

    def _after_action(self):
        if not self._armed or self._crashed or not self._crash_plan:
            return
        self._crash_plan[0] -= 1
        if self._crash_plan[0] == 0:
            self._crash_plan.pop(0)
            self._crashed = True
            self._armed = False
            raise PowerLossError("模拟掉电：设备已冻结，reboot() 前拒绝一切操作")

    def _check_open(self):
        if self._crashed:
            raise PowerLossError("设备处于掉电冻结态：请 reboot() 后重新挂载 FTL")

    # ------------------------------------------------------------------
    # 物理操作
    # ------------------------------------------------------------------
    def program(self, block, page, data, *, purpose=""):
        """编程一页（1..32 字节，不足部分保持 0xFF）。页必须为空白。"""
        self._check_open()
        block = require_int("block", block, 0, BLOCKS - 1)
        page = require_int("page", page, 0, PAGES_PER_BLOCK - 1)
        data = require_bytes("data", data, min_len=1, max_len=PAGE_SIZE)
        if not isinstance(purpose, str):
            raise InvalidInputError(f"purpose 必须是 str，得到 {purpose!r}")
        if self._bad[block]:
            raise BadBlockError(f"块 {block} 是坏块，禁止编程")
        ppn = block * PAGES_PER_BLOCK + page
        cell = self._cells[ppn]
        if any(b != BLANK_BYTE for b in cell):
            raise InvalidInputError(f"物理页 {ppn} 非空白，NAND 页禁止重写")
        cell[: len(data)] = data
        self._log.append(PhysicalAction("program", block, page, purpose))
        self._after_action()

    def erase(self, block, *, purpose=""):
        """擦除整块。磨损块抛 BlockWornError，坏块抛 BadBlockError。"""
        self._check_open()
        block = require_int("block", block, 0, BLOCKS - 1)
        if not isinstance(purpose, str):
            raise InvalidInputError(f"purpose 必须是 str，得到 {purpose!r}")
        if self._bad[block]:
            raise BadBlockError(f"块 {block} 是坏块，禁止擦除")
        if self._erase_counts[block] >= self._max_erases:
            raise BlockWornError(
                f"块 {block} 擦除次数已达磨损上限 {self._max_erases}，拒绝擦除"
            )
        base = block * PAGES_PER_BLOCK
        for i in range(base, base + PAGES_PER_BLOCK):
            self._cells[i] = bytearray(BLANK_PAGE)
        self._erase_counts[block] += 1
        self._log.append(PhysicalAction("erase", block, None, purpose))
        self._after_action()

    def read_page(self, block, page):
        """读取整页（恒返回 32 字节）。坏块抛 BadBlockError。"""
        self._check_open()
        block = require_int("block", block, 0, BLOCKS - 1)
        page = require_int("page", page, 0, PAGES_PER_BLOCK - 1)
        if self._bad[block]:
            raise BadBlockError(f"块 {block} 是坏块，禁止读取")
        return bytes(self._cells[block * PAGES_PER_BLOCK + page])

    def is_blank(self, block, page):
        """页是否为空白（全 0xFF）。坏块抛 BadBlockError。"""
        self._check_open()
        block = require_int("block", block, 0, BLOCKS - 1)
        page = require_int("page", page, 0, PAGES_PER_BLOCK - 1)
        if self._bad[block]:
            raise BadBlockError(f"块 {block} 是坏块")
        return all(b == BLANK_BYTE for b in self._cells[block * PAGES_PER_BLOCK + page])

    # ------------------------------------------------------------------
    # 坏块管理
    # ------------------------------------------------------------------
    def mark_factory_bad(self, block):
        """登记工厂坏块：仅允许对从未使用（零擦除、全空白）的块登记。"""
        self._check_open()
        block = require_int("block", block, 0, BLOCKS - 1)
        if self._bad[block]:
            raise InvalidInputError(f"块 {block} 已是坏块")
        base = block * PAGES_PER_BLOCK
        used = self._erase_counts[block] != 0 or any(
            any(c != BLANK_BYTE for c in self._cells[base + p])
            for p in range(PAGES_PER_BLOCK)
        )
        if used:
            raise InvalidInputError(
                f"块 {block} 已被使用，工厂坏块登记仅适用于未使用的空白块"
            )
        self._bad[block] = True

    def retire_block(self, block):
        """运行时退役：将块标记为坏块（幂等）。调用方须保证块内无有效数据。"""
        self._check_open()
        block = require_int("block", block, 0, BLOCKS - 1)
        self._bad[block] = True

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    @property
    def max_erases(self):
        return self._max_erases

    def erase_count(self, block):
        block = require_int("block", block, 0, BLOCKS - 1)
        return self._erase_counts[block]

    def is_bad_block(self, block):
        block = require_int("block", block, 0, BLOCKS - 1)
        return self._bad[block]

    def bad_blocks(self):
        return tuple(b for b in range(BLOCKS) if self._bad[b])

    def action_log(self):
        """物理动作日志（只读副本，按发生顺序）。"""
        return tuple(self._log)

    # ------------------------------------------------------------------
    # 快照 / 恢复（测试线束：用于枚举断电点后重放同一轨迹）
    # ------------------------------------------------------------------
    def snapshot(self):
        """深拷贝当前设备状态；须与 restore() 配对使用。"""
        return {
            "cells": [bytes(c) for c in self._cells],
            "erase_counts": list(self._erase_counts),
            "bad": list(self._bad),
            "log": list(self._log),
            "plan": None if self._crash_plan is None else list(self._crash_plan),
        }

    def restore(self, snap):
        """恢复到 snapshot() 保存的状态；清除冻结与武装状态。"""
        if not isinstance(snap, dict) or any(
            k not in snap for k in ("cells", "erase_counts", "bad", "log", "plan")
        ):
            raise InvalidInputError("restore() 需要 snapshot() 返回的状态字典")
        self._cells = [bytearray(c) for c in snap["cells"]]
        self._erase_counts = list(snap["erase_counts"])
        self._bad = list(snap["bad"])
        self._log = list(snap["log"])
        self._crash_plan = None if snap["plan"] is None else list(snap["plan"])
        self._crashed = False
        self._armed = False
