# agent-baseline-v1 基线报告

## 结论

Graph A/B 第一阶段的 `A-agent-search` development baseline 已端到端跑通，并满足代码与运行层面的冻结条件：Reviewer 完成真实跨文件只读调查，统一 Finding Schema 可解析，Evidence Verifier 接受真实 Tool Evidence，Root-Cause Consolidator 正常执行，Eval 输出完整指标；运行过程中没有 Graph 初始化、Graph index、Graph event 或 Candidate Context Manifest。

本报告**不代表已完成正式 Graph A/B**。未运行 held-out dataset，未执行 Graph Cold/Warm 正式对比，未调整 Graph 算法，也未修改 Golden Findings。

当前工作树包含尚未提交的实现，因此未自动创建 `eval/agent-baseline-v1` tag。代码已经通过冻结前检查，需由用户审阅并提交后再创建本地 tag。

## 固定运行合同

- 基线合同：`eval/contracts/agent-baseline-v1.yaml`
- 基线 JSON：`eval/outputs/agent-baseline-v1.json`
- Variant：`A-agent-search`
- Context Mode：`agent_search`
- Graph Cache Mode：`disabled`
- Model：`deepseek-v4-flash`
- Temperature：`0.0`
- Max output tokens：`2048`
- Max iterations：`3`（实际使用 2）
- Tool budget：`64`（实际使用 3）
- Matcher：`semantic-v2`
- Finding Schema：`2.0`
- 基线 commit：`eac439c2f3ddf88b8874b9dcbfcdc156bb4d7da0`
- 实现快照 SHA-256：`ba18bbc64f35c3ed5d8a112800cd7ee02d99d700b21f65c8128bb834e3a94b27`
- 实现快照范围：19 个第一阶段实现、审计、development fixture 与测试文件；不含循环依赖的合同/报告及用户原有未跟踪文件
- 快照算法：按仓库相对路径排序，将文件换行归一化为 LF，依次输入 `path + NUL + bytes + NUL` 后计算 SHA-256

## 实际运行命令

```powershell
& '.\.venv\Scripts\python.exe' -m eval.run eval `
  --suite development `
  --fixtures-dir eval/development_fixtures `
  --variant-id A-agent-search `
  --context-mode agent_search `
  --graph-cache-mode disabled `
  --samples 1 `
  --concurrency 1 `
  --fixture-concurrency 1 `
  --review-max-iterations 3 `
  --temperature 0 `
  --output-json eval/outputs/agent-baseline-v1.json
```

## Development fixture 结果

| Fixture | 目的 | Schema | Hit/Recall | False Positive | Repair Unit | Evidence |
|---|---|---:|---:|---:|---:|---:|
| `development_agent_search_cross_file` | changed caller 与 helper/test 的跨文件调查 | 100% | 1/1（100%） | 0 | 1/1（100%） | 100% complete |

该 fixture 的变更在 `src/checkout.py:4` 重复扣减折扣。Reviewer 通过只读工具读取 changed file、`src/discounts.py` 和 `tests/test_checkout.py`，确认 helper 已经完成折扣计算并形成单一根因 Finding。

## 运行指标

| 指标 | 结果 |
|---|---:|
| Run ID | `d48c8510-6e91-4922-867c-07fe4be7d184` |
| Review iterations | 2 |
| Tool calls | 3 |
| `read_file` calls | 3 |
| grep calls | 0 |
| symbol lookup calls | 0 |
| Candidate findings | 1 |
| Verifier accepted / rejected | 1 / 0 |
| Deterministic evidence pass | 1 / 1 |
| Final root-cause findings | 1 |
| Consolidator blocks | 1 |
| Reviewer latency | 14.713 s |
| Verifier latency | 4.177 s |
| Consolidation latency | 0.003 s |
| End-to-end latency | 18.921 s |
| Total tokens | 15,638 |
| Prompt/completion split | provider 未提供，记录为 `null` |

## Graph 完全未运行的证据

运行事件 `context_telemetry` 与最终 `review_complete` 同时记录：

```text
context_mode=agent_search
relation_graph_enabled=false
graph_status=disabled
graph_cache_mode=not_applicable
manifest_count=0
manifest_token_cost=0
parsed_file_count=null
graph_node_count=null
graph_edge_count=null
cache_hit=null
fallback_reason=""
```

此外，事件日志不存在 `relation_graph_built`、`index_lifecycle` 或 `context_manifest_created` 事件；development 临时工作区中未创建 SQLite Graph index。Graph 字段使用 `disabled/not_applicable/null`，没有用零成本伪装正常 Graph 运行。

证据日志：`eval/outputs/event_logs/development_agent_search_cross_file_d48c8510-6e91-4922-867c-07fe4be7d184.jsonl`。

## Graph Hybrid 回归

Graph Hybrid 未执行正式 A/B，但代码路径与回归测试保持可用：

- `GraphHybridContextStrategy` 实际构建关系图和 Change-Centered Context Manifest；
- 测试验证 Manifest 非空且 included spans 带有效 `context_hash`；
- Reviewer 只读工具仍可用；
- Graph 失败会记录 `fallback_agent_search` 与 `fallback_reason`，不会冒充 Graph 正常运行；
- `tests/test_code_relation_graph.py`、`tests/test_change_context_planner.py`、Finding provenance、Root-Cause pipeline 等 Graph 相关回归已纳入 199 项核心回归集合并通过。

## 自动化验证

- 全量 pytest：`501 passed, 1 skipped, 3 warnings`。
- Graph/Context/Verifier/Root-Cause/Eval 核心回归：`199 passed`。
- 新增 Graph A/B 第一阶段合同测试：`5 passed`。
- `mypy src/`：通过，75 个 source files 无问题。
- 本次修改文件 `ruff check`：通过。
- 本次修改文件 `ruff format --check`：通过。
- 全仓 `ruff check .`：仍有 141 个历史问题，均不在本次改动文件。
- 全仓 `ruff format --check .`：仍有 87 个历史未格式化文件，其中包括用户原有未跟踪文档；本阶段未扩散修改。

## 冻结条件判断

| 条件 | 状态 | 证据 |
|---|---|---|
| 显式 `agent_search` / `graph_hybrid` | 满足 | Settings、Orchestrator 构造参数、Eval Variant 均可注入 |
| Context Strategy 脱离 Orchestrator | 满足 | Orchestrator 只调用 `ContextStrategy.prepare` |
| Agent 不初始化/构建 Graph | 满足 | 无 Graph 事件、无索引、Graph telemetry disabled |
| Graph Hybrid 未破坏 | 满足 | Graph/Planner/Manifest 回归通过 |
| Common + Mode Prompt | 满足 | Common 文本同一常量，mode policy 独立 |
| 统一 Finding Schema | 满足 | Graph 字段可选，无第二套类型 |
| Diff/Tool/Manifest Evidence | 满足 | 统一 Verifier + EvidencePolicy 来源契约 |
| Eval 不依赖 Graph 字段判正确性 | 满足 | `semantic-v2` 只匹配位置、机制、不变量、影响路径和修复单元 |
| Agent 真实跨文件工具调查 | 满足 | 3 次成功 `read_file`，跨 changed/helper/test 文件 |
| 全量测试 | 满足 | 501 passed / 1 skipped |
| Contract / Report | 满足 | 两个固定路径均已生成 |
| 当前提交可直接打 tag | 待用户提交 | 自动提交被任务明确禁止；当前 HEAD 是基线父提交 |

因此，`agent-baseline-v1` 的实现与运行快照已具备冻结条件；Git 层冻结状态为 `ready_after_user_commit`，不能在未提交工作树上创建有意义的 tag。

建议在审阅并提交后执行：

```powershell
git tag -a eval/agent-baseline-v1 -m "Freeze agent-baseline-v1"
git show --stat eval/agent-baseline-v1
```

## 已知限制与第二阶段入口

- 当前 baseline 只包含 1 个明确跨文件 development fixture，不能外推为生产质量结论。
- Provider 未返回 prompt/completion token 拆分，只能冻结真实 total tokens。
- 全仓 Ruff/format 存量未在本阶段清理；本次改动文件自身通过。
- 第二阶段应在提交和 tag 后，使用相同 contract/dataset/matcher 分别运行 `A-agent-search`、`B-graph-cold`、`B-graph-warm`，再进行正式 held-out A/B；不得回看 held-out 结果修改 Golden Findings。
