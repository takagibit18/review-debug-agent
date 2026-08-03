# agent-baseline-v1 正式封板报告

## 封板结论

Graph A/B 阶段一的 `A-agent-search` development baseline 已在 clean 实现提交
`8cb385e702797601a32a239ebd52870449c32df7` 上重新运行并通过。Reviewer 完成跨文件只读调查，Evidence Verifier 与 Root-Cause Consolidator 均实际执行；运行期间 Graph、Manifest 和 Graph SQLite index 均未启用或创建。

本次封板前发现 `semantic-v2` 未将 “a second time” 识别为 `twice/double` 的直接阻塞 Bug。该缺陷以最小等价词映射和回归测试修复，形成实现提交 `8cb385e…`；未修改 Prompt、Golden Findings、阈值或 held-out 数据。由于正式 baseline 实际运行在该修复后的 clean HEAD 上，冻结的 implementation commit 不再使用原提交 `3f898e4…`。

`agent-baseline-v1` 只证明统一标准下的纯 Agent Search 路径已经可复现运行，并不证明其生产质量，也不代表 Graph A/B 已完成。

## 冻结对象

- Implementation commit：`8cb385e702797601a32a239ebd52870449c32df7`
- Implementation snapshot SHA-256：`6a120e969d62dec49049ac610d60a03221647c9c65f32f41127b0078d9af5ec3`（19 个文件）
- Contract：`eval/contracts/agent-baseline-v1.yaml`
- Compact artifact：`eval/baselines/agent-baseline-v1.json`
- Annotated tag：`eval/agent-baseline-v1`
- Freeze status：`frozen`
- Formal Graph A/B executed：`false`

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

运行环境为 Windows 11 Pro、Python 3.12.13；模型为 `deepseek-v4-flash`。关键依赖版本：pydantic 2.13.4、httpx 0.28.1、click 8.4.2、pytest 9.1.1、ruff 0.16.1、mypy 2.3.0。

## Baseline 结果

| 项目 | 结果 |
|---|---:|
| Run ID | `b3b1b4dd-584e-4282-aebe-e2d02cc8a2db` |
| Fixture | `development_agent_search_cross_file` |
| Schema | valid |
| Expected / matched | 1 / 1 |
| False positives | 0 |
| Root-cause coverage | 1 / 1 |
| Repair-unit accuracy | 1 / 1 |
| Review iterations | 2 |
| Tool calls | 3 |
| `read_file` / grep / symbol lookup | 3 / 0 / 0 |
| Candidate findings | 1 |
| Verifier accepted / rejected | 1 / 0 |
| Deterministic evidence checked / passed | 1 / 1 |
| Final root-cause findings | 1 |
| Reviewer latency | 148.507 s |
| Verifier latency | 12.604 s |
| Consolidation latency | 0.118 s |
| End-to-end latency | 161.357 s |
| Eval fixture latency | 162.133 s |
| Total tokens | 15,670 |

Reviewer 使用 3 次 `read_file` 调查 changed file、`src/discounts.py` 与 `tests/test_checkout.py`。Verifier 接受 1 个具有完整确定性证据的 Finding；Consolidator 形成 1 个 root-cause block 和 1 个最终 root-cause Finding。

## Graph 隔离证据

最终 raw output 与 event log 同时满足：

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
```

事件日志不存在 `relation_graph_built`、`index_lifecycle` 或 `context_manifest_created`；存在 `finding_verification_completed` 与 `root_cause_consolidation_completed`。本次 development 临时工作区生命周期内未产生 Graph SQLite index。未执行 `B1-graph-hybrid-cold`、`B2-graph-hybrid-warm` 或任何 held-out test。

## 产物与证据哈希

| 产物 | 路径 | SHA-256 |
|---|---|---|
| Compact artifact | `eval/baselines/agent-baseline-v1.json` | `ad07e9544715289bf22d8479c69fa53eeeccf7ff33be5be1b9bb3c61931ff4fe` |
| Raw local output | `eval/outputs/agent-baseline-v1.json` | `7e8a7c335e35c0da8d0580bcd09fb4bdba2b640d5cb279a3679ff1ca5739e6d3` |
| Event log | `eval/outputs/event_logs/development_agent_search_cross_file_b3b1b4dd-584e-4282-aebe-e2d02cc8a2db.jsonl` | `6610878cf0048a22daf6f7c57f061bcc59e0f75b295aff930c253b8cb0e2d585` |

Raw output 与 event log 位于 ignored 目录，仅作为本地审计证据；compact artifact 不包含 API Key、绝对用户目录、完整模型推理或仓库源码。

## 自动化验证

- Graph A/B 阶段一合同测试：`6 passed`。
- 封板一致性测试与 Graph A/B 合并验证：`10 passed`。
- 全量 pytest：`506 passed, 1 skipped, 3 warnings`。
- `mypy src/`：通过，75 个 source files 无问题。
- 本次相关 Python 文件 `ruff check`：通过。
- 本次相关 Python 文件 `ruff format --check`：通过。

3 个 warning 均为现有 FastAPI/Starlette 弃用提示，与阶段一封板无关。未扩散处理全仓历史 Ruff 或 format 问题。

## 已知限制

- development baseline 只有 1 个经人工标注的跨文件 fixture，不能外推为生产质量结论。
- Provider 未返回 prompt/completion token 拆分，仅能冻结真实 total tokens。
- 相同 temperature=0 参数下，模型措辞仍可能波动；冻结证据以本报告所列 run ID、raw hash 和 event-log hash 为准。
- 正式 Graph Cold/Warm 与 held-out A/B 尚未执行，且本阶段没有修改 Golden Findings。

## 最终冻结状态

```text
implementation_ready = true
git_commit_ready = true
tag_created = true
freeze.status = frozen
formal_graph_ab_executed = false
```

seal commit 完成后，本地 annotated tag `eval/agent-baseline-v1` 指向该 seal commit；不执行 push、远程 tag、PR 或远程仓库修改。
