"""本轮迭代演示：扩展 Hamming SECDED 页级校验与待清洗队列。

运行：python demo_iteration.py
全程确定性真实计算（无 sleep、无预录输出），1 秒内完成：
正常结果（无故障读写、单比特纠正+清洗）与边界结果（校验位翻转、
双比特不可纠正、清洗中途掉电后恢复、世代过期不被救活）。
"""
from ftl import FTL, PAGE_SIZE, PowerLossError, RawNand, UncorrectableError


def payload(i, size=16):
    """确定性示例数据（仓库内代码生成，无外部依赖）。"""
    return bytes(((i * 17 + j * 11 + 3) % 256 for j in range(size)))


def locate(ftl, lpn):
    ppn = ftl.mapping_snapshot()[lpn]
    return ppn // 8, ppn % 8


def main():
    print("=== 1. 正常结果：无故障写读（旧行为一致） ===")
    dev = RawNand(max_erases=8)
    ftl = FTL(dev)
    data = payload(1)
    ftl.write(0, data)
    slot = data.ljust(PAGE_SIZE, b"\xff")
    print(f"  写入 {len(data)} 字节，读回一致: {ftl.read(0) == slot}")
    print(f"  读状态: {ftl.read_status(0)}，待清洗队列: {ftl.scrub_pending()}")

    print("=== 2. 正常结果：数据区单比特翻转 → 纠正 + 入队 + 清洗 ===")
    b, p = locate(ftl, 0)
    dev.inject_bit_flip(b, p, 7, area="data")  # 合成位翻转，不访问真实磁盘
    print(f"  翻转后读状态: {ftl.read_status(0)}")
    print(f"  纠正后读回一致: {ftl.read(0) == slot}")
    ftl.read(0)
    ftl.read(0)  # 重复读不重复排队
    print(f"  重复读 3 次后待清洗队列: {ftl.scrub_pending()}")
    print(f"  清洗页数: {ftl.scrub()}，清洗后状态: {ftl.read_status(0)}，"
          f"队列: {ftl.scrub_pending()}")
    print(f"  清洗后读回一致: {ftl.read(0) == slot}")

    print("=== 3. 边界：校验位自身翻转也被识别（数据不变） ===")
    b, p = locate(ftl, 0)
    dev.inject_bit_flip(b, p, 2, area="spare")
    print(f"  校验区翻转后读状态: {ftl.read_status(0)}，"
          f"数据仍一致: {ftl.read(0) == slot}")
    ftl.scrub()

    print("=== 4. 边界：双比特错 → 明确损坏，绝不返回伪造数据 ===")
    ftl.write(1, payload(2))
    b, p = locate(ftl, 1)
    dev.inject_bit_flip(b, p, 0, area="data")
    dev.inject_bit_flip(b, p, 1, area="data")  # 同一数据字两比特
    print(f"  读状态: {ftl.read_status(1)}")
    try:
        ftl.read(1)
        print("  错误：未报告损坏")
    except UncorrectableError as exc:
        print(f"  read 抛出 UncorrectableError: {exc}")

    print("=== 5. 边界：清洗中途掉电 → 重启后映射与数据仍可恢复 ===")
    ftl.write(2, payload(3))
    b, p = locate(ftl, 2)
    dev.inject_bit_flip(b, p, 13, area="data")
    expected2 = ftl.read(2)  # 纠正后的数据
    dev.set_crash_plan([1])  # 清洗写的第 1 个物理动作后掉电
    dev.arm_crash()
    try:
        ftl.scrub()
        print("  错误：未发生掉电")
    except PowerLossError:
        print("  清洗中途掉电（符合注入计划）")
    dev.reboot()
    ftl2 = FTL(dev)  # 重新挂载：提交记录协议恢复映射
    print(f"  重启重挂载后读回一致: {ftl2.read(2) == expected2}")
    print(f"  收尾清洗页数: {ftl2.scrub()}，状态: {ftl2.read_status(2)}")

    print("=== 6. 边界：校验正确但世代过期的页不能被救活 ===")
    ftl2.write(3, b"old-version")
    ftl2.write(3, b"new-version")  # 旧页 ECC 仍有效但世代已过期
    b, p = locate(ftl2, 3)
    dev.inject_bit_flip(b, p, 0, area="data")
    dev.inject_bit_flip(b, p, 1, area="data")  # 当前页双比特错
    ftl3 = FTL(dev)
    print(f"  当前页损坏后读状态: {ftl3.read_status(3)}"
          "（不回退到旧世代数据）")

    print("=== 演示完成：以上结果均为本次真实计算 ===")


if __name__ == "__main__":
    main()
