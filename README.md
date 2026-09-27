# ftl — 合成闪存翻译层：只读快照、断电搬迁与磨损管理

面向离线嵌入式存储工程的可复用合成 FTL（flash translation layer）模块：
逻辑页覆盖写采用“新页 + 带 CRC32 的映射提交记录”，掉电可发生在每次
页写/擦除之后；恢复时按序列号选择最后一条完整提交记录；垃圾整理先搬迁
仍有效的页再擦块；磨损上限由输入给定；坏块永不再分配。

在此之上提供**只读快照**：把任一已提交映像固化后，当前版本可继续覆盖写，
旧版本引用的数据页与提交记录页在所有引用它的快照删除前不会被 GC 擦掉；
快照的创建/删除均为单页原子描述符，断电恢复后已提交快照与当前版本
各自完整，同一逻辑位置的新旧内容绝不混用。

## 环境与运行

- Python **3.14.7**，仅标准库，**无需安装任何第三方依赖**
  （`requirements.txt` 仅为说明，内容为空注释）。
- Linux 原生运行；核心、测试、演示全部离线，不需要账号、网络、硬件、
  数据库、中间件、云 API 或 Docker。
- 示例数据由仓库内确定性代码生成，无外部附件。

```bash
python -m unittest discover -s tests -v   # 运行全部测试（约 1 秒）
python demo.py                            # 固定输入演示（约 1 秒内完成）
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
- 快照描述符（32 字节，恰好一页）：
  `magic(0x5A,1B) | op_seq(3B 小端) | op(1B) | snap_id(2B 小端)
  | commit_seq(3B 小端) | commit_ppn(1B) | reserved(17B, 全 0xFF)
  | crc32(4B 小端)`。`op=1` 创建（引用被固化的提交记录序号与其物理页号；
  空映像快照为 `commit_seq=0/commit_ppn=0xFF`），`op=0` 删除墓碑
  （引用字段为 0/0xFF）。`op_seq` 与提交记录共用同一个 24 位全局单调
  序号；恢复时同一 `snap_id` 取 `op_seq` 最大（同号取物理页号大者）的
  描述符。被引用提交记录必须仍完整存在且序号一致，创建描述符才有效
  （悬空描述符按死页处理）。

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

### `ftl.FTL(device, *, gc_budget=None)` — 翻译层（挂载即恢复）

- `write(lpn, data, *, gc_budget=None)`：覆盖写逻辑页
  （新数据页 + 提交记录）。`gc_budget` 可覆盖本次操作的 GC 擦除预算。
- `read(lpn)`：返回 32 字节槽（写入数据为前缀，余为 `0xFF` 填充）；
  未映射返回 `None`。
- `create_snapshot(snap_id)`：把当前已提交映像固化为只读快照
  （`snap_id` 为 0..65535；重复 ID 抛 `InvalidInputError`）。仅写一页
  描述符，页写完成前掉电则快照不存在，完成后则完整可见。
- `read_snapshot(snap_id, lpn)`：从快照读旧版本（32 字节槽，未映射
  返回 `None`）；不存在的快照抛 `InvalidInputError`。
- `delete_snapshot(snap_id)`：写一页删除墓碑；仅墓碑完整写入后，快照
  引用的旧版本页才降为死页可供 GC。墓碑允许使用 GC 保留页，保证设备
  恰好只剩保留页时仍能删除快照释放空间。
- `list_snapshots()` / `snapshot_info(snap_id)`：存活快照 ID 有序元组 /
  `{'snap_id','commit_seq','op_seq','mapped_pages'}`。
- `gc_collect(*, gc_budget=None)`：手动垃圾整理，返回擦除的块数。
- `capacity()`：剩余容量/磨损快照（`free_physical_pages`、
  `reclaimable_dead_pages`、`snapshots`、`snapshot_ids`、
  `snapshot_retained_pages`（存活快照钉住的数据页+元数据页）、
  `gc_reserved_pages`、`bad_blocks`、`worn_blocks`、`erase_counts`、
  `commit_seq` 等，全为精确整数）。
- `logical_log()`：逻辑读写日志；`physical_log()`：物理动作日志；
  `mapping_snapshot()`、`commit_seq`、`device`。

### 校验助手

`require_int`（拒绝 bool）、`require_finite_number`（拒绝 NaN/Infinity）、
`require_bytes`。所有公开入口使用同一套校验，非法输入抛
`InvalidInputError` 并带参数名与取值，不静默纠正。

## 输入上限

| 参数 | 合法范围 |
|---|---|
| `max_erases` | int，0..2³¹−1（拒绝 bool/浮点；0 表示禁止任何擦除） |
| `lpn` | int，0..23 |
| `snap_id` | int，0..65535（拒绝 bool/浮点） |
| `data` | bytes-like，1..32 字节 |
| `gc_budget` | int，0..2³¹−1（默认 32 = 2×块数） |
| `crash_plan` 元素 | int，≥1 |
| `block` / `page` | 0..15 / 0..7 |
| 提交/快照全局序号 | 0..16777215（24 位，提交与快照描述符共用），耗尽抛 `FTLError` |

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
- **快照即引用、从不搬旧页**：快照只通过描述符引用某条历史提交记录，
  不复制数据。GC 视图把存活快照引用的数据页计为 `snap_data`、把其
  提交记录页与所有“仲裁仍生效”的描述符页计为 `snap_meta`；含任一此类
  页的块不作为受害块，因此旧版本页既不会被搬迁（搬迁会改变物理位置、
  破坏描述符引用）也不会被擦掉。被同 ID 更新描述符取代的旧描述符
  （含墓碑之后的旧创建页）才计为死页；墓碑自身在同 ID 重新创建前仍是
  仲裁生效页。删除最后一个引用某提交的快照后，相关页才降为死页。
- **快照描述符的单页原子性**：创建/删除都只写一页，页写在动作边界
  原子完成，故掉电只可能发生在“描述符不存在”或“描述符完整生效”
  两种状态之间，无需多页回滚。
- **崩溃注入在动作边界**：页写/擦除建模为原子，掉电只发生在动作之后，
  与题面“掉电可发生在每次页写/擦除之后”一致。

## 已知限制

- 合成教学级实现，**不宣称兼容任何真实 NAND 硬件或行业标准**
  （ONFI、UBI、FTL 规范等均未覆盖）。
- 提交记录仅靠 magic+CRC32+结构校验识别：恶意主机可伪造数据页冒充
  提交记录（误判概率约 2⁻³²），本模块不防御恶意写入者。
- `read` 返回 32 字节物理槽（写入数据为前缀、`0xFF` 填充），不记录
  原始写入长度；调用方需自行约定长度编码。
- 提交序列号上限 16777215 次，耗尽后写操作抛 `FTLError`。
- 快照 ID 最多 65536 个；同一时刻存活快照受物理页数上限约束（每个
  快照至少占 1 页描述符），空间不足时 `create_snapshot` 与写操作一样
  抛 `OutOfSpaceError` / `GCBudgetExhaustedError`。
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
- `test_snapshots.py`：快照读旧版本/当前继续写、空映像快照、重复 ID、
  GC 压力下旧版本不被擦、快照钉满空间时明确拒绝写入且映像完整、
  删除后释放、创建/删除各断电点恢复、与独立参考解析器交叉验证。

独立参考：`tests/refimg.py` 不调用 FTL 核心算法——它仅用设备只读
接口扫描物理页，独立复现“提交记录解析 + 序列号仲裁”（`parse_snapshots`
同时独立复现“快照描述符仲裁 + 引用校验”）来重建期望映像；测试侧另
维护写影子字典做往返一致补充检查。
