# Length Recovery 持久化升级工程报告

日期：2026-08-13

## 结论

本轮按 Run Journal、Draft Finding、Length Recovery 三阶段实现并验证。Targeted 三个历史
bad cases 均从 `finish_reason="length"` 进入一次 submit-only recovery，并形成合法
`submit_review`，没有再发生 Agent Harness 把截断响应静默解释为空 review 的情况。

唯一正式 Core Eval 5×2 矩阵实测为 9/10 valid，而不是 10/10：pytest/baseline 在
length 响应中找到了 frozen-gold bug，却同时提交 blank `summary=""` + `issues=[]`；当时
runtime 把 schema-valid 对象误当成明确空 review。正式运行后已修复这个边界并增加回归，
但遵守单次采样约束没有补跑，因此报告保留真实的 9/10。

## 1. 文件范围

| 类别 | 文件 |
|---|---|
| Runtime / schema | `src/orchestrator/run_journal.py`、`src/orchestrator/draft_findings.py`、`src/orchestrator/agent_loop.py`、`src/orchestrator/tool_schemas.py`、`src/models/schemas.py` |
| Analyzer | `src/analyzer/inference_engine.py`、`src/analyzer/schemas.py`、`src/analyzer/prompts.py`、`src/analyzer/run_summary.py` |
| Eval | `eval/core_eval.py`、`eval/run_summary.py`、`eval/schemas.py`、`eval/README.md`、`eval/reports/core-eval-length-recovery.md`、本报告 |
| 文档 | `docs/architecture.md`、`docs/shared_contracts.md`、`docs/error_log.md` |
| 测试 | `tests/test_run_journal.py`、`tests/test_draft_findings.py`、`tests/test_length_recovery.py`、`tests/test_agent_loop.py`、`tests/test_inference_engine.py`、`tests/test_core_eval.py`、`tests/test_orchestrator_tool_schemas.py`、`tests/test_prompts.py` |

机器 JSON、targeted runner 结果和逐 run EventLog 位于被 Git 忽略的 `eval/outputs/`，
没有把临时 workspace 或 provider 原始文件提交进仓库。

## 2. 三个提交组

1. `feat(runtime): add append-only run journal`
   - 新增与 EventLog 分离的 `.mergewarden/runs/<run_id>/journal.jsonl`。
   - 持久化 visible model response 和完整结构化 tool result。
   - 实现 append、replay、last entry、单调 seq、尾行崩溃容错和中间损坏拒绝。
2. `feat(review): persist minimal draft findings`
   - 新增 orchestrator-owned `record_draft_finding` 伪工具和内存 store。
   - runtime 可信绑定 draft id / source response id，并在普通工具执行前持久化。
   - finalize 上下文按 draft、retained evidence、legacy concern 的顺序构造；draft 不绕过最终提交、过滤器或 verifier。
3. `fix(review): recover structured submission after length truncation`
   - 把无可用 review submit 的 `finish_reason="length"` 标为 incomplete。
   - 至多执行一次只暴露并强制选择 `submit_review` 的 recovery。
   - 增加显式状态、失败错误、保守 visible-content draft 提取和 Eval 遥测。
   - 明确空 review 必须为 `issues=[]` 且 summary 非空；blank submit 必须继续恢复或失败。

## 3. Journal schema

每行都是一个版本化 envelope：

```text
schema_version, id, seq, type, run_id, timestamp, payload
```

`schema_version` 当前为 `1.0`，`seq` 从 1 单调增加。支持四类事实：

| type | payload |
|---|---|
| `model_response` | `iteration`、`model`、`finish_reason`、visible `content`、`tool_calls`、`usage.prompt_tokens`、`usage.completion_tokens`、`usage.total_tokens` |
| `tool_result` | `source_response_id`、`tool_call_id`、`tool`、经敏感键处理的 `arguments`、完整结构化 `result` envelope |
| `draft_finding` | 一个完整的最小 `DraftFinding` |
| `length_recovery` | `status`、`source_response_ids`、`draft_finding_ids`、`submit_response_id`、`reason` |

每次 append 执行 write、flush、默认 fsync。仅 malformed 的最后一个非空行按中断写入忽略；
更早的损坏会抛出 corruption error。EventLog 继续只承担 observability / eval / telemetry。

## 4. DraftFinding 最终字段

内部对象严格只有：

```text
id
source_response_id
file
line | null
symbol | null
claim
```

模型只能提供 `file`、`claim`、可选 `line` / `symbol`；额外字段被拒绝。没有 severity、
confidence、root cause、impact、evidence role、candidate id、verifier status 或生命周期状态。

## 5. 持久化时机与顺序

`ModelClient.chat()` 返回后，runtime 先 append `model_response`，然后才解析 tool calls、
fallback、validation 或其它 agent logic。因此即使后续解析失败，provider 可见响应也已落盘。

同一响应中的处理顺序是：

```text
model_response journaled
→ parse record_draft_finding
→ runtime binds provenance and journals draft
→ update DraftFindingStore
→ execute ordinary tools
→ journal each structured tool_result immediately after execution
```

不是在 iteration 结束时批量写入。

## 6. Reasoning 边界

没有持久化 `reasoning_content`、CoT 或 hidden thinking。Journal 只保存 visible `content`。
现有进程内 transient reasoning 可继续存在；recovery 遥测只记录它是否出现的布尔值，
不会记录内容。没有新增基于隐藏推理的持久化或归因。

## 7. Length Recovery 状态流

```text
provider response
→ journal model_response
→ parse drafts / tools and persist their facts
→ finish_reason=length + no usable review business output
→ required
→ optional conservative visible-content DraftFinding
→ attempted
→ submit-only prompt(drafts → retained evidence → fallback concerns)
→ journal recovery model response
→ usable submit: succeeded
→ blank/invalid submit, hard cap, timeout, or provider failure: failed + blocking error
```

普通 read/search 工具不在 recovery schema 中。合法显式空 review 是 `issues=[]` 加非空、
明确的 summary；`summary=""` + `issues=[]` 不是成功。length 响应即使带普通 tool calls，
只要没有可用 review submit，也必须在保留这些工具结果后进入 recovery。

## 8. Targeted bad cases

每个指定 case 只采样一次：

| Case | Valid | Journal writes | Drafts | Recovery | Raw / final / gold match |
|---|---:|---:|---:|---:|---:|
| pytest / MergeWarden | 是 | 3 | 0 | 1 attempted / 1 succeeded / 0 failed | 1 / 0 / 0 |
| pytest / baseline | 是 | 3 | 0 | 1 / 1 / 0 | 1 / 1 / 1 |
| Pydantic / MergeWarden | 是 | 3 | 0 | 1 / 1 / 0 | 1 / 1 / 1 |

三条都包含 `model_finish_reason_length_no_submit`，三条都最终看到了合法 submit，且没有
placeholder。pytest/MergeWarden 的正确问题已从 recovery 进入后续 pipeline，但最终未被
下游过滤/验证保留；这是质量门后的结果，不是 harness 静默丢失。Targeted 中模型主动
draft 合规率仍为 0/3，这个事实没有被解释成“模型未发现问题”，也没有重采样掩盖。

## 9. 正式 Core Eval 5×2

正式矩阵只运行一次；完整报告见 `eval/reports/core-eval-length-recovery.md`。

- Workspace setup：10/10 成功。
- Valid completion：9/10；MergeWarden 5/5，baseline 4/5。
- 唯一 invalid：pytest/baseline，`Empty review output: no summary or issues.`。
- MergeWarden quality（valid runs）：precision 100%，recall 50%，F1 66.7%；baseline 为 0%。由于一个 candidate baseline invalid，完整 A/B 不可比较。
- Clean controls：6/6 valid，warning/critical false positives = 0。
- Journal writes：baseline 11、MergeWarden 13，共 24。
- Drafts：baseline 0、MergeWarden 1；唯一 draft 来自 Pydantic/MergeWarden，且最终命中 frozen gold。
- Length recovery：baseline 0、MergeWarden 1；1 succeeded、0 failed。
- pytest/MergeWarden 在 length 后恢复并提交正确 frozen-gold bug，随后被既有 finding filter 以 warning confidence / evidence specificity 门槛剔除；未修改该门槛。
- 最大累计 tokens 61,313，低于未变的 80,000 hard budget；单次 output 仍为 4,096。

正式 run 后新增的 blank-submit 修复有 inference、agent-loop、recovery 和 report regression，
但没有把正式报告追写成 10/10。

## 10. 仍存在的失败与限制

存在一条正式实测的“发现正确问题但未形成合法 draft / submit”案例：pytest/baseline 的
length 响应发现了 gold bug，但只给出 blank submit，导致正式结果 invalid。该具体边界已
在正式后修复并由本地测试覆盖，尚未通过第二次正式模型采样重新测量。

此外，模型仍可能不主动调用 `record_draft_finding`：targeted 为 0/3，正式矩阵仅 1 个
draft。当前保障是先持久化完整 visible response，再由受限 recovery 形成 submit；如果信息
只存在于 hidden reasoning 且既未写 visible content、也未 record draft，则跨进程仍不能恢复，
这是不持久化 CoT 的明确边界。保守 extractor 也只接受明确 repo source path 与问题 claim，
不会从无路径 preview 猜测文件。

## 11. 测试与静态检查

- PR1 阶段全量：691 passed，1 skipped。
- PR2 阶段全量：699 passed，1 skipped。
- PR3 最终全量：716 passed，1 skipped，3 个既有 deprecation warnings。
- PR3 最终相关回归：110 passed；覆盖 length + tool、已有/无 draft、visible fallback、
  无路径拒绝、root-level/最近路径、显式空 review、blank submit、blank recovery、hard cap、
  debug 隔离、Journal/EventLog/Eval 统计及报告渲染。
- `ruff check .` 通过；触及 Python 文件的 `ruff format --check` 通过；`mypy src` 通过。
- 全仓 `ruff format --check .` 会要求格式化 81 个既有无关文件，因此未做批量机械改写。

## 12. 明确未做

- 未实现 Session tree、parent/leaf branching、长期/向量 memory、完整 event sourcing、数据库或分布式 journal。
- 未实现复杂 DraftFinding lifecycle 或自动 severity / root-cause / impact 推断。
- 未实现完整跨进程 mid-run resume；Journal 先建立正确 durable fact 边界。
- 未持久化 reasoning / CoT，也未用 transient reasoning 自动生成复杂 finding。
- 未重构 verifier / evidence / root-cause pipeline，未降低既有 finding filter 或质量门槛。
- 未修改 frozen gold、matcher、thresholds、clean controls、expected root cause 或 Core fixture 定义。
- 未提高 model output、soft token 或 hard token budget。
- 未为 9/10 正式结果补跑样本；保留失败证据，避免通过重复采样制造 GO。
