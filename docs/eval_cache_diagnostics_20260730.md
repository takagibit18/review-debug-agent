# Golden Eval 缓存诊断记录 - 2026-07-30

本文记录 `codex/eval-cache-reuse-fix` 分支上的 Golden eval 缓存诊断结果。
背景是 v023-v025 引入持久化关系图索引后，Golden eval 变得很难完整跑完。
这次诊断的目标是确认困难来自 agent 缓存缺失、eval 缓存策略不合理，还是关系图冷构建本身过重。

## 总结

主要问题不是 agent 没有缓存。agent 已经有 persistent relation graph index，但 eval runner 的运行方式让缓存基本失效：

- eval 会把每个 fixture 恢复到新的临时 checkout。
- relation graph index path 原先会被解释为临时 checkout 下的相对路径。
- `repository_identity()` 原先包含 checkout 的本地绝对路径，所以即使共用外部 SQLite 文件，不同临时 checkout 也会被视为不同仓库。

小修之后，eval 专用关系图索引会放在：

`eval/outputs/workspace_cache/relation_index/`

同时，带有 `remote.origin.url` 的 Git checkout 会使用 remote URL 生成稳定 repository identity。

## 已完成的小修

- `eval.runner._eval_relation_graph_index_path()` 现在返回临时 checkout 外部的稳定绝对 SQLite 路径。
- `AgentOrchestrator` 增加可选 `relation_graph_index_path` override，供 eval runner 注入稳定索引路径。
- `repository_identity()` 现在优先 hash `remote.origin.url`；没有 remote 时才退回本地路径。
- `EvalResult.stage_timings` 新增阶段耗时：
  - `prepare_workspace_seconds`
  - `validate_fixture_seconds`
  - `agent_run_seconds`
- 测试覆盖：
  - eval index path 稳定且为绝对路径；
  - eval runner 确实把 index path 传给 orchestrator；
  - `stage_timings` 会进入结果；
  - 相同 remote、不同 checkout path 的 repository identity 一致。

## 诊断结果

### Requests fixture

Fixture: `golden_real_requests_netrc_pr7205`

非模型 relation-index 探针：

| 运行 | Workspace 准备 | Fixture 校验 | Index 耗时 | 状态 | Cache hit | Parsed files |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| 冷缓存 | 3.621s | 0.047s | 5.267s | build | 0.0 | 36 |
| 热缓存 | 2.729s | 0.031s | 0.715s | reuse | 1.0 | 0 |

绝对路径修正前的真实单 fixture eval：

| 指标 | 值 |
| --- | ---: |
| prepare_workspace_seconds | 2.770s |
| validate_fixture_seconds | 0.032s |
| agent_run_seconds | 25.735s |
| graph_build_latency_seconds | 4.924s |
| persistent_cache_hit_rate | 0.0 |
| total_tokens | 55,470 |

绝对路径修正后的真实单 fixture eval：

| 指标 | 值 |
| --- | ---: |
| prepare_workspace_seconds | 2.325s |
| validate_fixture_seconds | 0.035s |
| agent_run_seconds | 10.928s |
| graph_build_latency_seconds | 0.747s |
| persistent_cache_hit_rate | 1.0 |
| total_tokens | 49,141 |

产物：

`eval/outputs/diagnostic_requests_single_absindex_20260730_report.json`

### Pytest fixture

Fixture: `golden_pytest-dev_pytest_pr9350`

非模型 relation-index 探针：

| 运行 | Workspace 准备 | Fixture 校验 | Index 耗时 | 状态 | Cache hit | Parsed files |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| 冷缓存 | 4.364s | 0.029s | 739.780s | build | 0.0 | 244 |
| 热缓存 | 5.311s | 0.018s | 6.018s | reuse | 1.0 | 0 |

真实热缓存单 fixture eval：

| 指标 | 值 |
| --- | ---: |
| prepare_workspace_seconds | 6.683s |
| validate_fixture_seconds | 0.019s |
| agent_run_seconds | 9.273s |
| graph_build_latency_seconds | 4.756s |
| persistent_cache_hit_rate | 1.0 |
| candidate_context_tokens | 3,983 |
| included_graph_paths | 23 |
| discarded_graph_paths | 0 |
| total_tokens | 31,303 |

产物：

`eval/outputs/diagnostic_pytest9350_single_absindex_20260730_report.json`

### Pytest 冷索引缩放探针

同一 fixture: `golden_pytest-dev_pytest_pr9350`

| max_files | 构建耗时 | Nodes | Edges | SQLite 大小 |
| ---: | ---: | ---: | ---: | ---: |
| 25 | 0.734s | 259 | 572 | 0.8 MB |
| 50 | 4.010s | 1,176 | 6,013 | 6.4 MB |
| 100 | 43.192s | 2,923 | 25,974 | 25.5 MB |
| 150 | 403.661s | 5,294 | 69,838 | 68.6 MB |

完整可复用 pytest index 包含：

- 244 个文件
- 6,617 个节点
- 90,705 条边
- SQLite 文件约 94 MB

完整 pytest index 中数量最多的边类型：

| Edge kind | Count |
| --- | ---: |
| CALLS | 29,448 |
| CALLED_BY | 29,448 |
| TESTED_BY | 16,441 |
| CONTAINS | 6,373 |
| ENCLOSED_BY | 6,373 |

贡献最多的 resolver：

| Resolver | Count |
| --- | ---: |
| ast_attribute_candidates | 36,537 |
| derived_from_test_reference | 16,441 |
| ast_scope | 10,600 |
| ast_import_attribute_candidates | 5,960 |
| ast_unique_attribute_candidate | 5,757 |

这说明冷启动问题不是简单的文件解析线性变慢，而是候选边生成在图变大后迅速膨胀。
尤其是宽泛的 attribute call candidates 和 derived test relations，会主导冷构图成本。

### Ruff fixture

Fixture: `golden_astral-sh_ruff_pr24648`

该 fixture 没有进入 relation graph 构建阶段。workspace checkout 在 Windows 上失败，错误是 `Filename too long`，主要来自 Ruff 仓库里很长的 snapshot 文件路径。

这是 eval workspace/materialization 层的独立问题，不是 relation graph cache 问题。

## 结论

缓存修复对重复运行和同仓库多临时 checkout 是有效的：

- Requests 图复用从约 5 秒降到 1 秒以内。
- Pytest 热缓存复用避开了约 740 秒的冷图构建。
- 两个真实热缓存单 fixture eval 的 agent 阶段都在约 9-11 秒内完成。

剩下最硬的问题是 relation graph 冷构建。当前 graph builder 会从几百个文件生成数万条候选边。
这会让首次 Golden eval、schema bump 后的 eval、或者清空缓存后的 eval 非常昂贵。

持久化缓存可以缓解重复运行，但不能解决 fresh machine / fresh schema / fresh cache 的首次成本。

## 剩余问题

- 冷 relation graph 构建需要 eval 专用的 budgeted 或 change-centered indexing 模式。大 fixture 不应该默认先构完整仓候选图。
- `ast_attribute_candidates` 以及宽泛的 `CALLS/CALLED_BY` 生成需要更严格的上限或延迟扩展。
- `TESTED_BY` derivation 在大仓里也会膨胀，应限制到 changed symbols/files 和高概率测试邻居。
- Ruff workspace restore 需要 Windows 长路径缓解，例如使用更短 checkout root，或设置仓库级 `core.longpaths=true`。
- 两个真实 eval 探针使用了较短模型/运行超时，并且使用 `--review-max-iterations 1`；这些结果只用于诊断性能和缓存，不作为质量指标。

## 2026-07-31 冷构图修复

后续代码分析确认，候选边数量之外还存在一个更直接的复杂度放大器：
`CodeRelationGraph.add_edge()` 原先每次插入都会线性扫描全部已有边做去重，
`outgoing()` / `incoming()` 也会反复扫描全图。单独插入 10,000 条唯一边的本地探针耗时
约 21.449 秒，属于明显的二次复杂度退化。

修复包括：

- 使用非序列化的 edge-id 集合和出入边邻接索引，将插入、去重和邻接查询改为哈希查找；
- 建立符号名称索引，避免每个 AST 表达式重新遍历全部节点；
- 默认只保留最多 4 个歧义候选，超过上限时记录汇总诊断而不任意选择部分候选；
- 只从强且置信度不低于 0.65 的调用或引用派生 `TESTED_BY`；
- 缓存加载同时校验 builder version 和构建画像，配置变化时只重建对应仓库图。

同一 10,000 边插入探针修复后耗时约 0.691 秒，提升约 31 倍。

为了排除 GitHub 下载影响，另用 244 个 Python 文件、6,832 个节点的本地合成仓库模拟
pytest 规模及高重复 attribute 名称：

| 模式 | 冷构建及持久化 | Edges | SQLite 大小 | 省略候选 |
| --- | ---: | ---: | ---: | ---: |
| 默认上限 4 | 7.768s | 13,176 | 14.137 MB | 59,536 |
| 不限制候选 | 15.391s | 132,248 | 104.660 MB | 0 |

默认策略把该工作负载的持久化耗时降低约 49.5%，边数降低约 90.0%，SQLite 大小降低约
86.5%；相对原真实 pytest fixture 的 739.780 秒冷构建，规模探针已超过 10 倍改善目标。

当前 MergeWarden 工作树的真实仓库探针包含 160 个文件、3,580 个节点和 20,793 条边：
冷构建及持久化为 25.441 秒，热加载为 4.829 秒，`status=reuse` 且
`persistent_cache_hit_rate=1.0`。该图记录了 174 次高基数歧义截断、2,481 个省略候选以及
598 条未派生的弱测试关系。

真实 `golden_pytest-dev_pytest_pr9350` 复测尝试未进入构图：首次 mirror clone 超过 240 秒，
改用浅层 partial clone 后又在 checkout 按需获取 blob 时达到 120 秒 Git 超时。因此本轮不能把
真实 fixture 标记为已复测；失败仍位于 workspace materialization/network 阶段。Ruff 的 Windows
长路径问题也继续作为独立 workspace/materialization 问题保留。

## 已验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_eval_runner.py tests\test_code_relation_graph.py tests\test_eval_process_metrics.py -q
ruff check .
mypy src
git diff --check
```

结果：

- `71 passed`
- Ruff 通过
- Mypy 通过，覆盖 72 个 source files
- `git diff --check` 通过

