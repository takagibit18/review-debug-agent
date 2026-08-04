# Graph A/B 阶段二 Pilot 报告

## 1. 实验身份与边界

- 实验：`graph-ab-phase2-pilot`
- 起始 `origin/main`：`774539fa50e0dedf824552547758796ca6affb1e`
- 分支：`experiment/graph-ab-phase2-pilot`
- 冻结 tag：`eval/agent-baseline-v1`
- 冻结 target：`b5dc82bbffb38f1ba05587efa5dfcda08eb10b78`
- 正式 Graph A/B：未执行
- held-out：未执行
- Pilot 等级：`pilot-smoke`；每 fixture、每 Variant 仅 1 次，不能用于稳定性结论

本报告只说明工程有效性与初步方向性信号，不构成 Graph 优于 Agent、最终统计结论或生产路由阈值。

## 2. 冻结实验契约

共享配置逐项读取并核对 `eval/contracts/agent-baseline-v1.yaml`：

| 配置 | 值 |
|---|---:|
| model | `deepseek-v4-flash` |
| temperature | 0.0 |
| max output tokens | 2048 |
| review max iterations | 3 |
| tool budget | 64 |
| model request timeout | 90 s |
| tool timeout | 30 s |
| run timeout | 170 s |
| seed | 20260804 |

Reviewer Common Prompt、Agent Search Policy、Graph Context Policy、Verifier、Consolidator、Finding Schema、`semantic-v2` Matcher、Golden Findings、severity 阈值和模型参数均未修改。`tests/test_agent_baseline_seal.py` 通过，冻结三项产物的 Git diff 为空。

## 3. Variant 与实际契约

| Variant | 预期模式 | 实际有效运行 | 契约结果 |
|---|---|---:|---|
| A-agent-search | agent search；Graph disabled | 2/2 | PASS |
| B1-graph-hybrid-cold | graph hybrid；真实 Cold | 2/2 | PASS |
| B2-graph-hybrid-warm | Cold context priming 后真实 Warm hit | 2/2 | PASS |

A 的 event log 中无 `relation_graph_built`、`index_lifecycle` 或 `context_manifest_created`，临时工作区未产生 SQLite index。B1 均为 `cache_hit=false`，B2 均为 `cache_hit=true`；所有有效 Graph run 均为 `graph_status=ready`、manifest 非空且无 fallback。

## 4. Fixture 清单、类型与配对顺序

| Fixture | 类型覆盖 | Snapshot | 顺序 |
|---|---|---|---|
| `development_agent_search_cross_file` | cross-file、two-hop dependency、state/cache consistency、test gap | `synthetic:62f03028…bd7a3` | A → B1 → B2 |
| `local_smoke_pytest_approx_pr8513` | single-file、cross-file、public API、test gap | `synthetic:4c6b8264…28e08` | B2 → A → B1 |

同一 fixture 的三组运行使用相同 snapshot、diff、Golden Finding、模型参数、工具预算、最大轮次、timeout 和冻结契约哈希。当前仓库只有上述两个无需远程恢复且已 reviewed 的本地 fixture，未伪造缺失类型或 Golden Finding。

## 5. Measured run 明细

| Run ID | Fixture | Variant | 顺序 | Cache | Contract | Matched/Expected |
|---|---|---|---:|---|---|---:|
| `9b5cbbba-5073-48f0-ba48-743a0d8af341` | development cross-file | A | 1 | N/A | valid | 1/1 |
| `01248c37-4dec-47a5-b080-d8b845bc647e` | development cross-file | B1 | 2 | cold miss | valid | 0/1 |
| `2030c412-24bd-4066-894a-947be6ccc409` | development cross-file | B2 | 3 | warm hit | valid | 0/1 |
| `f19c8942-3912-4cf4-8fc8-c04848d74485` | local pytest approx | B2 | 1 | warm hit | valid | 0/1 |
| `2e080f0c-bd02-40df-a0d1-8cc65c8b4b19` | local pytest approx | A | 2 | N/A | valid | 0/1 |
| `73e44f6f-69cf-4b5f-a8ab-f08aeda82ee4` | local pytest approx | B1 | 3 | cold miss | valid | 0/1 |

## 6. Invalid runs

最终本地可行范围 Pilot：0 个 invalid run。

首次尝试恢复三个远程 reviewed fixtures 时，以下 9 个原始运行在模型执行前因 workspace mirror clone 失败而 invalid；它们未进入任何均值或质量比较，原始证据保存在 ignored `eval/outputs/graph-ab-phase2-pilot-network-invalid.json`：

- `failed:golden_pydantic_pydantic_pr12117:B2-graph-hybrid-warm:1`
- `failed:golden_pydantic_pydantic_pr12117:A-agent-search:1`
- `failed:golden_pydantic_pydantic_pr12117:B1-graph-hybrid-cold:1`
- `failed:golden_pytest-dev_pytest_pr9350:B1-graph-hybrid-cold:1`
- `failed:golden_pytest-dev_pytest_pr9350:B2-graph-hybrid-warm:1`
- `failed:golden_pytest-dev_pytest_pr9350:A-agent-search:1`
- `failed:golden_openclaw_openclaw_pr37717:A-agent-search:1`
- `failed:golden_openclaw_openclaw_pr37717:B1-graph-hybrid-cold:1`
- `failed:golden_openclaw_openclaw_pr37717:B2-graph-hybrid-warm:1`

随后获准网络重试，但现有 Eval Runner 的完整 mirror clone 在 20 分钟命令上限内仍未完成，因此没有产生新的 measured run ID。超时进程已终止，未将未完成工作伪装为有效运行。

## 7. 质量矩阵

| Variant | Valid | Matched/Expected | False positive | Mean hit rate | Precision（有命中时） | Root cause matched | Evidence pass |
|---|---:|---:|---:|---:|---:|---:|---:|
| A | 2 | 1/2 | 0 | 0.50 | 1.00 | 1/2 | 0.50 |
| B1 | 2 | 0/2 | 0 | 0.00 | N/A | 0/2 | 0.00 |
| B2 | 2 | 0/2 | 0 | 0.00 | N/A | 0/2 | 0.00 |

Development cross-file 的 A 命中而 B1/B2 未命中；local pytest approx 三组均未命中。这只是单次 Pilot 的初步方向性信号，未据此修改 Graph、Prompt、Golden、Matcher 或阈值。

## 8. 成本与工具矩阵

| Variant | Mean end-to-end | Mean tokens | Tool calls（总计） | Mean manifest tokens | Mean parsed files | Mean index size |
|---|---:|---:|---:|---:|---:|---:|
| A | 14.820 s | 14,940.5 | 7 | 0 | N/A | N/A |
| B1 | 11.149 s | 20,804.0 | 7 | 363 | 2.5 | 49,152 B |
| B2 | 15.616 s | 22,125.0 | 7 | 363 | 0 | 49,152 B |

各 Variant 总工具调用均为 7；A/B1/B2 的 read-file 调用总数分别为 5/7/5，grep 与 symbol lookup 均为 0。`prompt_tokens`、`completion_tokens`、tool search success、out-of-scope read rate、candidate revision count 与 finding fingerprint distribution 无法由当前 telemetry 准确采集，保持 `null` 或 `not_available`，未伪造为 0。

## 9. Cold build、Warm priming 与 measured 成本

| Fixture | B1 build | B1 parsed/nodes/edges | B1 index SHA-256 | B2 priming | B2 measured cache | B2 index SHA-256 |
|---|---:|---|---|---:|---|---|
| development cross-file | 0.4879 s | 3 / 7 / 15 | `1ea364c4…25159` | 0.3170 s | warm hit | `f5f0fff8…7507` |
| local pytest approx | 0.2960 s | 2 / 7 / 10 | `f6573a5e…d5be` | 0.2992 s | warm hit | `ab8c0bed…7599` |

每个 B1 measured run 前均删除并确认目标 index 不存在。每个 B2 先清理 index，再在与 measured run 相同的临时 workspace、repository snapshot 和 index path 上执行 Cold context priming；priming 的 latency 单独记录，token/Finding 为 `null`，不进入 B2 质量均值。B2 measured run 未删除 index，schema version 为 3，均命中同一 primed repository identity；若重建则契约会 invalid。

## 10. 稳定性与 Agent 行为

每组只有一次/fixture，因此 mean、median、min、max 可计算但标准差 0 不代表稳定，`pass@k` 也不能外推。有效运行率为 100%（6/6），最终本地 Pilot invalid 率为 0%；远程恢复失败另行保留，不混入 measured 质量统计。

`review_iterations`、工具分类、重复工具调用、Verifier 候选/接受/拒绝和 stop telemetry 已逐 run 收集；当前事件源不足以准确推导的行为字段保持不可用。

## 11. Graph fallback

6 个有效 measured runs 均无 Graph fallback。契约测试覆盖 B1 fallback 自动 invalid；任何 `fallback_reason` 非空的 Graph run 不进入聚合。

## 12. 自动化验证

- 阶段二 + Graph/Context/Verifier/Root Cause/Eval 核心回归：128 passed
- 全量：516 passed, 1 skipped
- `mypy src/`：PASS（75 source files）
- `ruff check`：PASS
- `ruff format --check`：PASS
- 冻结 baseline seal：4 passed

## 13. Pilot 局限

- 仅 2 个 fixture、每 Variant 每 fixture 1 次，低于推荐的 3–6 fixtures × 3 samples。
- 三个远程 reviewed fixture 的完整 mirror 恢复未在当前 20 分钟执行窗口内完成；未获得其 validation measured 结果。
- 当前样本未覆盖所有仓库规模、语言和真实 workspace cache 场景。
- 多项 Agent 行为指标和 prompt/completion token 拆分不可用。
- 未执行 held-out，亦未执行正式大规模 A/B。

## 14. Go / No-Go

**Ready for formal paired A/B: NO**

已证明：三种 Variant 可端到端运行；实际契约校验、Cold 隔离、Warm priming、配对顺序、fallback/invalid 剔除、指标采集和自动化测试均有效。

阻塞项：

1. 现有远程 reviewed fixture 的完整 mirror 恢复在 20 分钟窗口内无法完成，正式批量前必须证明固定 repository snapshots 可在目标执行环境可靠、可限时地恢复。
2. 当前 Development/Validation Pilot 只有 2 个 fixture × 1 sample，只能作为 `pilot-smoke`，尚不足以验证批量运行稳定性。
3. 需要在不运行 held-out、且不修改任何冻结规则的前提下，完成至少 3 个代表性 reviewed fixtures 的配对预演，并确认没有新的 workspace、timeout 或 schema invalid run。

