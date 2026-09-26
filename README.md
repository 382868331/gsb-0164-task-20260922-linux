# ftl — 合成闪存翻译层：断电搬迁、磨损管理与 SECDED 页校验

面向离线嵌入式存储工程的可复用合成 FTL（flash translation layer）模块：
逻辑页覆盖写采用“新页 + 带 CRC32 的映射提交记录”，掉电可发生在每次
页写/擦除之后；恢复时按序列号选择最后一条完整提交记录；垃圾整理先搬迁
仍有效的页再擦块；磨损上限由输入给定；坏块永不再分配。本轮新增：
页读写链上可选的扩展 Hamming SECDED 校验（每个 8 位数据字独立编码），
读出区分无错/单比特已纠正/双比特不可纠正，已纠正页进入待清洗队列，
清洗经原安全搬迁过程写入新页。

## 环境与运行

- Python **3.14.7**，仅标准库，**无需安装任何第三方依赖**
  （`requirements.txt` 仅为说明，内容为空注释）。
- Linux 原生运行；核心、测试、演示全部离线，不需要账号、网络、硬件、
  数据库、中间件、云 API 或 Docker。
- 示例数据由仓库内确定性代码生成，无外部附件。

```bash
python -m venv .venv && source .venv/bin/activate   # 准备虚拟环境（可选但推荐）
python -m unittest discover -s tests -v   # 运行全部测试（约 1 秒）
python demo.py                            # 固定输入演示（约 1 秒内完成）
python demo_iteration.py                  # 本轮 SECDED 演示（约 1 秒内完成）
```

## 设备几何与持久格式（固定契约）

| 项 | 值 |
|---|---|
| 块数 | 16 |
| 每块页数 | 8 |
| 每页字节数 | 32 |
| 物理页总数 | 128 |
| 逻辑页数 | 24（由单页提交记录容量决定，见下） |
| GC 保留页 | 8（1 块，见“设计取舍”） |

- 擦除以整块为单位，擦除后全为 `0xFF`；页只能从空白编程一次，禁止重写。
- 掉电只能发生在一次物理动作（页编程/块擦除）完成之后；页写与块擦除
  本身视为原子。
- 提交记录（32 字节，恰好一页）：`magic(0xA5, 1B) | seq(3B 小端) |
  mapping(24B) | crc32(4B 小端)`。mapping 每项为一个物理页号
  （0..127）或 `0xFF`（未映射）。记录只有整页写成功且 CRC 与结构校验
  （映射项合法、互不重复、不自引用）通过才对恢复可见。
- 恢复（挂载）：扫描全部物理页，取序列号最大（同号取物理页号大者）的
  完整提交记录重建映射；其余页（含未提交的数据页、残缺/伪造记录）
  一律视为死页。

## 公开接口

### `ftl.RawNand(max_erases, *, crash_plan=None)` — 合成 NAND 设备

- `program(block, page, data, *, purpose="")`：编程空白页（1..32 字节）。
- `erase(block, *, purpose="")`：擦除整块；磨损抛 `BlockWornError`，
  坏块抛 `BadBlockError`。
- `read_page(block, page)` / `is_blank(block, page)`。
- `mark_factory_bad(block)`：登记工厂坏块（仅限未使用的空白块）。
- `retire_block(block)`：运行时退役为坏块（幂等）。
- `reboot()`：模拟重新上电（内容/计数/坏块/日志保留，清除冻结态）。
- `set_crash_plan([k, ...])` + `arm_crash()`：崩溃注入线束——
  每完成 k 次物理动作后掉电一次；`snapshot()`/`restore()` 支持
  枚举断电点后重放同一轨迹。
- 查询：`action_log()`（物理动作日志）、`erase_count(b)`、
  `is_bad_block(b)`、`bad_blocks()`、`max_erases`、`crashed`。

### `ftl.FTL(device, *, gc_budget=None, ecc=False)` — 翻译层（挂载即恢复）

- `write(lpn, data, *, gc_budget=None)`：覆盖写逻辑页
  （新数据页 + 提交记录）。`gc_budget` 可覆盖本次操作的 GC 擦除预算。
  ECC 模式下 `data` 上限为 16 字节（见下节）。
- `read(lpn)`：非 ECC 模式返回 32 字节槽（写入数据为前缀，余为
  `0xFF` 填充）；未映射返回 `None`。ECC 模式返回 `ECCReadResult`，
  未映射返回 `None`。
- `scrub_queue()`：待清洗队列快照（仍被映射引用的物理页号，按入队序）。
- `scrub(*, gc_budget=None)`：清洗待清洗页——把已纠正的数据经原安全
  搬迁过程（新页 + 提交记录，purpose 为 `scrub-copy`/`scrub-commit`）
  写入新页；返回 `{"scrubbed": n, "unmapped": m, "uncorrectable": k}`。
- `gc_collect(*, gc_budget=None)`：手动垃圾整理，返回擦除的块数。
- `capacity()`：剩余容量/磨损快照（`free_physical_pages`、
  `reclaimable_dead_pages`、`gc_reserved_pages`、`bad_blocks`、
  `worn_blocks`、`erase_counts`、`commit_seq`、`ecc`、
  `scrub_pending_pages` 等，全为精确整数）。
- `logical_log()`：逻辑读写日志；`physical_log()`：物理动作日志；
  `mapping_snapshot()`、`commit_seq`、`device`。

### 扩展 Hamming SECDED 页校验（`ecc=True`，`ftl.ecc` 子模块）

- 每个 **8 位数据字独立编码**为 13 位扩展 Hamming 码字：位 1/2/4/8
  为 Hamming 偶校验位，位 13 为覆盖位 1..12 的整体偶校验位，数据位
  依次放在位 3,5,6,7,9,10,11,12。码字按小端存 2 字节（高 3 位恒零），
  一页 32 字节恰好容纳 **16 字节用户数据**（不足以 `0xFF` 填充后编码）。
- 读出分类（独立校验方程：综合校验子 S = 位 1..12 中为 1 的位号异或，
  整体奇偶 P = 全部 13 位奇偶）：
  - `P=0, S=0` → `clean`（无错）；
  - `P=1, S≠0` → `corrected`（位 S 单比特翻转，含数据位与校验位）；
  - `P=1, S=0` → `corrected`（整体校验位自身翻转，数据不变）；
  - `P=0, S≠0` → `uncorrectable`（双比特错误，**绝不改成看似合法的
    数据**，`data` 返回 `None`）。
- `read` 返回 `ECCReadResult(status, data)`：`status` 为
  `clean`/`corrected`/`uncorrectable`，`data` 为 16 字节槽
  （不可纠正时为 `None`）。`corrected` 页自动加入待清洗队列；
  **重复读同一待清洗页不重复排队**；不可纠正页不入队。
- `scrub()` 清洗经提交记录生效，**清洗中断电后映射仍能恢复**（重启
  重新挂载即可）；待清洗队列为内存态，重启后重读受损页会重新入队。
- 校验正确但映射世代过期的页（被覆盖写淘汰的旧数据页）**不会被救活**：
  读路径只按当前映射取页，恢复只认序列号最大的完整提交记录。
- `ftl.ecc` 公开：`encode_word`/`decode_word`/`classify`/`syndrome`/
  `encode_page`/`decode_page`/`flip_bit` 及常量 `PAGE_DATA_BYTES=16`。
- 合成位翻转线束：`RawNand.inject_bit_flip(block, page, bit_index)`
  对已编程页翻转一位（非物理动作、不计日志），仅用于测试/演示，
  不访问真实磁盘。

### 校验助手

`require_int`（拒绝 bool）、`require_finite_number`（拒绝 NaN/Infinity）、
`require_bytes`。所有公开入口使用同一套校验，非法输入抛
`InvalidInputError` 并带参数名与取值，不静默纠正。

## 输入上限

| 参数 | 合法范围 |
|---|---|
| `max_erases` | int，0..2³¹−1（拒绝 bool/浮点；0 表示禁止任何擦除） |
| `lpn` | int，0..23 |
| `data` | bytes-like，1..32 字节（ECC 模式 1..16 字节） |
| `gc_budget` | int，0..2³¹−1（默认 32 = 2×块数） |
| `ecc` | bool（默认 False，拒绝非 bool） |
| `bit_index`（位翻转注入） | int，0..255 |
| `crash_plan` 元素 | int，≥1 |
| `block` / `page` | 0..15 / 0..7 |
| 提交序列号 | 0..16777215（24 位），耗尽抛 `FTLError` |

## 错误语义

| 异常 | 含义 |
|---|---|
| `InvalidInputError` | 非法输入（类型/范围/bool 充当整数/NaN/Inf） |
| `PowerLossError` | 掉电后设备冻结；需 `reboot()` 并重新挂载 |
| `OutOfSpaceError` | **可证明无解**：空白页 ≤ 保留页且无任何可回收块 |
| `GCBudgetExhaustedError` | **预算耗尽**：仍有可回收块，但本次操作预算不允许继续 |
| `BadBlockError` | 对坏块的读/写/擦除尝试 |
| `BlockWornError` | 块擦除次数达到 `max_erases` |

预算耗尽与证明无解严格分开：`gc_budget=0` 且存在死页时写操作抛
`GCBudgetExhaustedError`；`max_erases=0`（死页永不可回收）时抛
`OutOfSpaceError`（见 `tests/test_badblock_wear_space.py`）。

失败的原子性：`write` 失败（空间/预算/坏块/磨损）**不改变已提交逻辑
映像**（映射与序列号不变）；物理层可能留下未提交的孤儿页——这是已
说明的部分变更，会在后续 GC 中回收。`PowerLossError` 后设备冻结，
`reboot()` + 重新挂载即恢复到最后已提交映像。

## 设计取舍

- **整映像提交记录**：每条提交记录携带全部 24 项映射，恢复只需选
  序列号最大者，无需重放增量；代价是逻辑页数受单页容量限制（24 页）
  且每次写多占一页。
- **先提交后擦除**：GC 先把受害块的有效页搬迁他处并写入新提交记录，
  提交成功后才擦除受害块；若受害块持有当前最新提交记录，即使无需
  搬迁数据页也强制先写一条新提交记录，保证任何断电点都不丢已提交映像。
- **GC 保留页（8 页 = 1 块）**：用户态分配不得耗尽保留页，保证任何
  单块回收的搬迁（最多 7 个有效页 + 1 条提交记录）总有处可去；
  因此用户可见容量为 120 页槽而非 128。
- **静态磨损均衡**：分配按（擦除次数升序, 空白页数降序, 块号升序）
  选块；受害块按（有效页数升序, 擦除次数升序）选择。
- **崩溃注入在动作边界**：页写/擦除建模为原子，掉电只发生在动作之后，
  与题面“掉电可发生在每次页写/擦除之后”一致。

## 已知限制

- 合成教学级实现，**不宣称兼容任何真实 NAND 硬件或行业标准**
  （ONFI、UBI、FTL 规范等均未覆盖）。
- 提交记录仅靠 magic+CRC32+结构校验识别：恶意主机可伪造数据页冒充
  提交记录（误判概率约 2⁻³²），本模块不防御恶意写入者。
- `read` 返回 32 字节物理槽（写入数据为前缀、`0xFF` 填充），不记录
  原始写入长度；调用方需自行约定长度编码。
- ECC 模式每页用户数据降为 16 字节（每 8 位数据字独立占 2 字节存储）；
  提交记录本身仍由 magic+CRC32+结构校验保护，不做 SECDED 编码。
- SECDED 只保证单比特可纠、双比特可检；三比特及以上错误可能误判
  （扩展 Hamming 码的固有性质），本模块不建模更多位错误。
- 待清洗队列为内存态，不持久化；重启后需重读受损页重新发现。
- 提交序列号上限 16777215 次，耗尽后写操作抛 `FTLError`。
- 磨损上限只约束擦除次数，不建模编程/读取干扰等真实磨损源。
- 单线程、无并发；掉电不建模页写/擦除中途的撕裂页（动作内原子）。
- **浮点误差口径**：本模块不输出任何浮点结果，全部计数为精确整数，
  无误差；浮点数值参数一律按契约拒绝（NaN/Infinity 必拒）。

## 测试说明

`python -m unittest discover -s tests -v` 覆盖：

- `test_validation.py`：输入校验契约（bool/NaN/Inf/范围/类型）。
- `test_basic.py`：读写往返、容量账目、日志、确定性、重挂载恢复。
- `test_crash_points.py`：**枚举短写入轨迹（8 次写、16 个物理动作）
  的每一个断电点**，重启后与“最后已提交逻辑映像”逐一比较。
- `test_gc.py`：GC 数据完整性（每次写后全量比对）、提交先于擦除的
  顺序、手动整理、**枚举带搬迁 GC 的触发写内部每个断电点**（整理中断）。
- `test_badblock_wear_space.py`：坏块不再分配、磨损极限、
  空间不足与预算耗尽的区分、失败无逻辑部分变更。
- `test_records.py`：残缺/伪造/过期提交记录不得生效。
- `test_ecc.py`：本轮新增——小数据字枚举全部 13 个单错位置与 78 对
  双错位置，分类期望值由测试内独立复现的校验方程给出；纠正读入队与
  去重、校验位自身翻转识别、清洗搬迁、清洗中断电恢复、世代过期页不
  被救活、旧无故障写读一致。

独立参考：`tests/refimg.py` 不调用 FTL 核心算法——它仅用设备只读
接口扫描物理页，独立复现“提交记录解析 + 序列号仲裁”来重建期望映像；
测试侧另维护写影子字典做往返一致补充检查。
