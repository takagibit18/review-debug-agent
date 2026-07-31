# 关系图冷构建优化总结 - 2026-07-31

## 背景

v0.2.3-v0.2.5 引入持久化代码关系图后，Golden eval 在首次运行、缓存清空或索引版本升级时出现明显的冷构建性能问题。
此前对 `golden_pytest-dev_pytest_pr9350` 的诊断显示：244 个文件会生成 6,617 个节点、90,705 条边，
冷索引构建耗时约 739.780 秒，SQLite 文件约 94 MB。

缓存路径和仓库身份修复已经解决了重复运行时无法复用索引的问题，但无法降低 fresh cache 的首次构建成本。
本次工作继续分析并修复关系图构建自身的性能与缓存正确性问题。

## 本次改动

### 1. 消除边操作的二次复杂度

`CodeRelationGraph.add_edge()` 原先每插入一条边，都会线性扫描已有边进行去重；
`outgoing()` 和 `incoming()` 也会扫描全部边。图规模扩大后，这些操作会从线性流程退化为近似 O(E²)。

本次为图增加了不参与 JSON 和 SQLite 序列化的运行时索引：

- edge-id 哈希集合，用于 O(1) 去重；
- 出边邻接索引；
- 入边邻接索引。

图从 JSON/SQLite 恢复、深拷贝、删除文件或被可选 resolver 替换后，会统一重建这些私有索引，外部序列化格式保持不变。

### 2. 增加符号名称索引

AST 调用、引用和继承关系解析原先会反复遍历全部图节点查找同名符号。
现在在定义提取完成后建立 `name -> symbols` 内存索引，后续解析只过滤对应名称的候选集合。

### 3. 限制高基数歧义候选

新增配置：

```text
RELATION_GRAPH_MAX_AMBIGUOUS_TARGETS=4
```

行为如下：

- 歧义候选不超过 4 个时，继续保留现有低置信度探索边；
- 超过 4 个时，不任意选择前几个候选，也不物化 `CALLS/CALLED_BY` 等关系；
- 图中只记录确定性的汇总诊断，包括截断次数和省略候选数。

该策略主要约束无法确定 receiver 类型的 attribute call，以及其他高基数同名符号匹配。

### 4. 收紧 `TESTED_BY` 派生

`TESTED_BY` 现在只从以下关系派生：

- `evidence_eligibility=strong`；
- 置信度不低于 0.65；
- 原始关系为 `CALLS` 或 `REFERENCES`。

低置信度或歧义测试调用不会再复制成额外的 `TESTED_BY` 边，因为这些边本身也没有资格进入强证据 Context Manifest。

### 5. 修复缓存构建画像校验

索引构建版本从 `v025.1` 提升到 `v025.2`。缓存加载除 SQLite schema 外，现在还会校验：

- builder version；
- resolver mode；
- `max_files`；
- `max_ambiguous_targets`。

构建画像不一致时，只重建当前仓库对应的关系图，并记录明确的 rebuild reason；不会误报 cache hit，也不改变 SQLite 表结构。

### 6. 增强可观测性

图 metadata、orchestrator relation graph summary 和事件日志新增：

- `ambiguous_resolution_truncation_count`；
- `omitted_ambiguous_candidate_count`；
- `skipped_weak_test_relation_count`。

这些指标可以区分正常的精确解析、因候选过多而主动降级的解析，以及未派生的弱测试关系。

## 带来的提升

### 边插入微基准

插入 10,000 条唯一关系边：

| 实现 | 耗时 | 提升 |
| --- | ---: | ---: |
| 修复前 | 21.449s | - |
| 修复后 | 0.691s | 约 31 倍 |

这验证了边去重已从线性扫描改为哈希查找，主要 O(E²) 放大器被消除。

### pytest 规模合成仓库

为排除 GitHub 下载和 checkout 的影响，使用 244 个 Python 文件、6,832 个节点构造高重复 attribute 名称的本地工作负载：

| 模式 | 冷构建及持久化 | Edges | SQLite 大小 | 省略候选 |
| --- | ---: | ---: | ---: | ---: |
| 默认上限 4 | 7.768s | 13,176 | 14.137 MB | 59,536 |
| 不限制候选 | 15.391s | 132,248 | 104.660 MB | 0 |

默认限额带来的改善：

- 持久化耗时降低约 49.5%；
- 边数降低约 90.0%；
- SQLite 大小降低约 86.5%；
- 相对原真实 pytest fixture 的 739.780 秒，规模探针超过 10 倍改善目标。

### 当前 MergeWarden 仓库

当前工作树的真实仓库探针结果：

| 指标 | 结果 |
| --- | ---: |
| Files | 160 |
| Nodes | 3,580 |
| Edges | 20,793 |
| 冷构建及持久化 | 25.441s |
| 热缓存加载 | 4.829s |
| 热缓存状态 | `reuse` |
| Cache hit rate | 1.0 |
| SQLite 大小 | 22.816 MB |
| 歧义截断次数 | 174 |
| 省略候选数 | 2,481 |
| 未派生弱测试关系 | 598 |

说明当前实现能够正确建立索引、持久化并在第二次运行完整复用缓存。

## 正确性与兼容性

本次未修改节点、边或 SQLite 表结构，精确调用、继承、字段访问和强 `TESTED_BY` 的语义保持不变。

新增测试覆盖：

- 重复边去重；
- JSON 恢复、深拷贝、文件删除后的邻接索引一致性；
- resolver 替换图内容后的索引一致性；
- 4 个歧义候选继续保留，5 个候选整体降级；
- 弱测试调用不派生 `TESTED_BY`；
- 相同构建画像复用缓存；
- builder version、resolver、max files 或候选上限变化时安全重建；
- 5,000 条边的批量插入和重复插入回归。

验证结果：

- 全量测试：`496 passed, 1 skipped`；
- 关系图、Context Planner、eval runner 等针对性测试：`91 passed`；
- Mypy：72 个源文件通过；
- 本次涉及的 Python 文件通过 Ruff lint 和 format check；
- `git diff --check` 通过。

## 当前仍存在的问题

### 1. 真实 pytest fixture 尚未完成新版本冷构建复测

两次复测均未进入关系图构建阶段：

- eval mirror cache 首次 clone 超过 240 秒；
- 改用浅层 partial clone 后，checkout 按需获取 blob 达到 120 秒 Git 超时。

因此，本次不能把真实 `golden_pytest-dev_pytest_pr9350` 标记为已复测。当前性能结论来自同规模合成仓库和当前真实 MergeWarden 仓库。
后续应在已有完整 pytest workspace cache、网络稳定或离线预热完成的环境中重新运行真实冷索引探针。

### 2. Eval workspace 首次物化仍可能很慢

首次 mirror clone、partial clone 后的 lazy blob fetch、Git 超时均属于 workspace materialization 层问题。
它们会阻止 eval 进入关系图阶段，但不属于本次关系图构建修复范围。

后续可单独评估：

- mirror cache 是否默认启用 blob filter；
- 是否只获取 fixture 所需 commit/ref；
- checkout 与 blob fetch 是否需要独立超时和阶段指标；
- 离线 workspace cache 的预热与完整性校验。

### 3. Ruff fixture 的 Windows 长路径问题仍未处理

`golden_astral-sh_ruff_pr24648` 的 `Filename too long` 仍属于独立问题。本次没有修改 checkout root 或 `core.longpaths` 配置。

### 4. 热缓存加载仍有进一步优化空间

当前 MergeWarden 索引热加载为 4.829 秒。主要成本包括 SQLite 读取、Pydantic 模型恢复和私有邻接索引重建。
这一数值已经远低于冷构建，但如果未来索引继续扩大，可进一步考虑分区加载、惰性边加载或更紧凑的持久化载荷。

### 5. 全仓 Ruff 基线存在既有问题

新安装的 Ruff 0.16.1 对多个未修改文件启用了更严格规则，并认为约 90 个既有文件需要重新格式化。
本次仅保证所修改 Python 文件通过 lint/format，没有批量改写无关代码。后续若要求全仓 `ruff check .` 在该版本下通过，应单独进行规则配置或基线清理。

## 结论

本次修复消除了关系图边操作的主要二次复杂度，并通过符号索引、歧义候选上限和强关系派生约束显著降低图规模与 SQLite 体积。
缓存现在也能识别 builder 与构建参数变化，避免错误复用语义过期的索引。

从合成 pytest 规模探针和当前真实仓库结果看，关系图冷构建已从“数分钟至十几分钟不可用”降低到“数秒至数十秒可完成”的范围。
剩余主要风险已转移到 eval workspace 首次下载/checkout、真实大型 fixture 复测以及进一步降低热加载成本。
