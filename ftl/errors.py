"""ftl 模块的错误层次与错误语义契约。

错误分类（调用方可据此定位处理）：

- ``InvalidInputError``：非法输入（类型错误、范围越界、bool 充当整数、
  NaN/Infinity 等）。模块不静默纠正任何非法输入。
- ``PowerLossError``：模拟掉电后设备进入冻结态，或对已冻结设备继续操作；
  需要 ``reboot()`` 并重新挂载 FTL。
- ``OutOfSpaceError``：可证明当前无解——无空白物理页且不存在任何
  可回收块（含磨损上限导致的不可回收）。
- ``GCBudgetExhaustedError``：垃圾整理的擦除预算耗尽。与
  ``OutOfSpaceError`` 严格区分：此时仍存在可回收块，只是本次操作的
  预算不允许继续。
 - ``BadBlockError``：对坏块的读/写/擦除尝试；坏块永不再分配。
 - ``BlockWornError``：块的擦除次数达到输入给定的磨损上限。
 - ``EraseFailureError``：注入的硬件故障——块不可再擦写（但仍可读）。
 - ``PageUnreadableError``：注入的硬件故障——指定物理页已不可读。
 - ``DataLossError``：逻辑页数据已不可恢复（所在块已退役或物理页
   不可读）；模块绝不凭空伪造内容，读取一律显式报错。
"""


class FTLError(Exception):
    """本模块所有错误的公共基类。"""


class InvalidInputError(FTLError, ValueError):
    """非法输入：类型、范围或取值域违反契约。"""


class PowerLossError(FTLError):
    """模拟掉电：设备已冻结，重启（reboot）前拒绝一切操作。"""


class OutOfSpaceError(FTLError):
    """空间不足：无空白页且无可回收块，可证明当前无解。"""


class GCBudgetExhaustedError(FTLError):
    """垃圾整理擦除预算耗尽（尚有可回收块，但预算不允许继续）。"""


class BadBlockError(FTLError):
    """坏块访问：坏块不得再分配、读写或擦除。"""


class BlockWornError(FTLError):
    """磨损极限：块的擦除次数已达输入给定的上限。"""


class EraseFailureError(FTLError):
    """擦写失败：块已不可再擦写（注入的硬件故障），但仍可读取。"""


class PageUnreadableError(FTLError):
    """页不可读：指定物理页已损坏（注入的硬件故障），禁止读取。"""


class DataLossError(FTLError):
    """数据丢失：逻辑页内容已不可恢复，拒绝返回任何伪造内容。"""
