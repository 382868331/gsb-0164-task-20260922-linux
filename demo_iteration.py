"""本轮迭代演示：扩展 Hamming SECDED 页校验与待清洗队列。

运行：python demo_iteration.py（全程确定性真实计算，无 sleep、无预录
结果，1 秒内完成）。覆盖：正常无错读、单比特纠正并入队清洗、校验位
自身翻转识别、双比特不可纠正边界、清洗中断电后映射恢复。
"""
from ftl import FTL, RawNand, ecc
from ftl.errors import PowerLossError


def payload(i, size=16):
    """确定性示例数据（仓库内代码生成，无外部依赖）。"""
    return bytes(((i * 31 + j * 7 + 1) % 256 for j in range(size)))


def part_normal():
    print("=== 第一部分（正常结果）：无错读、单比特纠正、清洗搬迁 ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev, ecc=True)
    data = payload(1)
    ftl.write(4, data)
    res = ftl.read(4)
    print(f"  无故障读: status={res.status} 数据一致={res.data == data}")

    ppn = ftl.mapping_snapshot()[4]
    dev.inject_bit_flip(ppn // 8, ppn % 8, 3)  # 合成单比特错误
    res = ftl.read(4)
    print(f"  单比特翻转后读: status={res.status} 纠正后数据一致={res.data == data}")
    ftl.read(4)
    ftl.read(4)
    print(f"  重复读 3 次后待清洗队列（不重复排队）: {list(ftl.scrub_queue())}")

    result = ftl.scrub()
    new_ppn = ftl.mapping_snapshot()[4]
    res = ftl.read(4)
    print(f"  清洗结果={result}，页 {ppn} -> {new_ppn}，清洗后读: status={res.status}")

    # 校验位自身翻转也要识别（第 0 码字的整体校验位：页位 12）
    ftl.write(5, payload(2))
    ppn5 = ftl.mapping_snapshot()[5]
    dev.inject_bit_flip(ppn5 // 8, ppn5 % 8, 12)
    res = ftl.read(5)
    print(f"  整体校验位自身翻转: status={res.status} 数据一致={res.data == payload(2)}")
    print(f"  新入队={list(ftl.scrub_queue())}，清洗={ftl.scrub()}，"
          f"清洗后队列已空={ftl.scrub_queue() == ()}")


def part_boundary():
    print("=== 第二部分（边界结果）：双错不可纠正、清洗中断电恢复 ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev, ecc=True)
    ftl.write(7, payload(3))
    ppn = ftl.mapping_snapshot()[7]
    dev.inject_bit_flip(ppn // 8, ppn % 8, 0)
    dev.inject_bit_flip(ppn // 8, ppn % 8, 1)  # 同一码字双比特错误
    res = ftl.read(7)
    print(f"  双比特翻转后读: status={res.status} data={res.data}（明确损坏，不猜数据）")

    # 清洗中断电：提交前掉电，重启后映射仍能恢复，重读重新入队
    ftl.write(8, payload(4))
    ppn8 = ftl.mapping_snapshot()[8]
    dev.inject_bit_flip(ppn8 // 8, ppn8 % 8, 5)
    ftl.read(8)
    dev.set_crash_plan([1])  # 清洗的第 1 个物理动作后掉电
    dev.arm_crash()
    try:
        ftl.scrub()
        print("  错误：应发生掉电")
    except PowerLossError as e:
        print(f"  清洗中断电: {e}")
    dev.reboot()
    ftl2 = FTL(dev, ecc=True)
    res = ftl2.read(8)
    print(f"  重启恢复: 映射页={ftl2.mapping_snapshot()[8]} "
          f"status={res.status} 数据一致={res.data == payload(4)} "
          f"重新入队={list(ftl2.scrub_queue())}")
    print(f"  再次清洗: {ftl2.scrub()}，最终读 status={ftl2.read(8).status}")

    # 字级枚举统计（真实计算）：单错全部可纠、双错全部不可纠
    singles = doubles = 0
    for word in (0x00, 0x5A, 0xA5, 0xFF):
        cw = ecc.encode_word(word)
        for i in range(13):
            if ecc.decode_word(cw ^ (1 << i))[0] == "corrected":
                singles += 1
        for i in range(13):
            for j in range(i + 1, 13):
                if ecc.decode_word(cw ^ (1 << i) ^ (1 << j))[0] == "uncorrectable":
                    doubles += 1
    print(f"  字级枚举: 4 字 × 13 单错全部纠正={singles == 52}，"
          f"4 字 × 78 双错全部不可纠={doubles == 312}")


if __name__ == "__main__":
    part_normal()
    part_boundary()
    print("演示结束。")
