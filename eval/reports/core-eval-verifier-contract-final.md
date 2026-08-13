# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。
> Generated at：`2026-08-12T17:16:14.301874+00:00`。
> Review input：按 fixture 声明的 `full_pr` / `partial_pr` scope 从 Git range 派生。

## Infrastructure

- Core fixtures：5
- Successful workspace setups：10/10 attempts
- Completion failures：3
- Matcher：`core-semantic-v1`（deterministic, one-to-one, duplicate-aware）
- Shared runtime：`deepseek-v4-pro`，temperature 0，4096 output tokens，12000 prompt-context tokens，3 iterations，64 tool calls，60000/80000 token budget，12000 final-submit reserve，4000 finalize prompt-context tokens，1200 retained-evidence tokens

## Review Quality

Quality 仅统计 valid completions。

| Metric | Baseline | MergeWarden |
|---|---:|---:|
| Precision | 0.0% | — |
| Recall | 0.0% | — |
| F1 | 0.0% | — |
| High-severity Recall | — | — |
| False findings / PR | 0.00 | 0.00 |

## Reliability

| Metric | Baseline | MergeWarden |
|---|---:|---:|
| Valid completion rate | 80.0% | 60.0% |
| Placeholder/incomplete runs | 1 | 2 |
| Workspace failures | 0 | 0 |
| Validator failures | 0 | 0 |

## Candidate Finding Funnel

Counts cover valid candidate runs only; missing legacy fields deserialize as zero.

| Stage | Baseline | MergeWarden |
|---|---:|---:|
| No finding submitted | 1 | 0 |
| Non-risk not routed | 0 | 0 |
| Pre-verifier rejected | 0 | 0 |
| Calibration / rescue routed | 0 | 0 |
| Semantic rejected | 0 | 0 |
| Deterministic rejected | 0 | 0 |
| Final risk findings | 0 | 0 |

## Per-fixture comparison

| Fixture | Gold | Baseline hit | MergeWarden hit | Baseline FP | MergeWarden FP | Valid A/B |
|---|---:|---:|---:|---:|---:|:---:|
| `golden_pydantic_pydantic_pr12117` | 1 | 0/1 | invalid | 0 | — | no |
| `golden_pytest-dev_pytest_pr9350` | 1 | invalid | invalid | — | — | no |
| `golden_pydantic_pydantic_pr12568` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12590` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr13969` | 0 | 0/0 | 0/0 | 0 | 0 | yes |

## Main failure mode

3/4 个 candidate attempts 未合法完成，其中 3 个没有 submit_review，0 个在 hard token cap 后结束；baseline 1 个、mergewarden 2 个 candidate fixtures 缺少 valid completion。当前首要问题是 runtime reliability 与完整 PR 上下文的预算伸缩，而不是 semantic judge；review quality A/B 暂不可比较。

## Controls and optional oracles

本轮保留 3 个已稳定零问题 controls；未把它们包装成并不存在的 paired repair。
Executable oracles 暂未加入 Core gate，后续只为最适合的 functional bugs 增补。

## Deliberately deferred

- 每个 fixture × variant 只采集一次；失败保留在报告中，不自动付费补跑。
- 不增加统计显著性、几十个 fixtures 或复杂 composite score。
- 不为所有 findings 建 executable oracle 或 Docker benchmark。
- 不把现有零问题 controls 冒充 paired repairs；真正的 repair pair 后续按证据补充。

## README conclusion

在 5 个 real-world full-workspace PR 组成的小型精选集上，当前合法完成不足，尚不能比较 A/B review quality；MergeWarden valid completion rate 为 60.0%。该结果用于说明当前实现的相对表现，不代表对更大 PR 分布的统计结论。

## Post-run evidence-chain audit

本节审计机器 JSON 与 10 个 event logs；不改变 renderer 的聚合口径。正式命令只执行一次，恰好产生 10 条 attempt-1 records，没有自动补采。

### 最终问题清单

1. **10/10 valid：否。** 结果为 7/10 valid；baseline 4/5，MergeWarden 3/5。10/10 workspace setup 均成功，0 validator failure，0 hard-budget exhaustion。
2. **pytest A/B 正确问题能否稳定进入最终结果：不能。** A/B 都在分析文本中明确识别 `SafeHashWrapper.__eq__` 对 peer wrapper 而非 `other.obj` 比较的问题；两者均在第二次 reviewer response 达到 4096 completion tokens，以 `finish_reason=length` 截断且未调用 `submit_review`。因此正确发现没有形成 candidate，更没有进入 semantic verifier 或 deterministic gate。
3. **Pydantic 剩余问题分类：当前是 reviewer discovery/submission，而不是 evidence chain。** valid baseline 明确提交空 review；MergeWarden 的截断分析已识别默认/非-allow extra mode 下 `model_copy(update=...)` 行为变化，但同样在首次 response 达到 4096 completion tokens，未提交 finding。两条路径都没有进入 verifier，无法归因给 evidence binding 或 deterministic validation。
4. **Deterministic reject：2 个 finding / 4 条 reason records。** candidate fixtures 中为 0；两次 reject 都来自 clean control `golden_pydantic_pydantic_pr12568` 的 A/B 同一 cosmetic `info` finding。
5. **Reject 是否合理：是。** 两个 verdict 都把“skip reason 未同步更新”这种明确标注为 cosmetic/no functional impact 的 info finding 返回为 accepted，却没有提供 severity-calibration 必需的 revised issue；gate 分别记录 `finding_not_actionable_risk` 与 `verifier_revision_required`，正确阻止 warning/critical FP。
6. **Final risk finding：0。** 这是 7 个 valid completions 的真实最终输出；三个 invalid candidate 不能被当成“无 finding”。
7. **Gold matched：0。** 唯一 valid candidate 是 Pydantic baseline 且提交空 review；其余三个 candidate attempts invalid，因此不能形成可比较的 recall 结论。
8. **Clean-control FP：0。** 6/6 control attempts 全部 valid，最终 warning/critical FP 为 0。
9. **明确系统误杀：本轮未观察到。** candidate evidence chain 没有收到可验证 finding；两个 deterministic rejects 都是合理的 clean-control guard。当前阻塞已前移到 reviewer 的简洁发现/强制提交可靠性，而不是 semantic verifier → deterministic evidence validation。

### 三个 invalid candidate attempts

| Fixture / variant | Failure | Evidence-chain interpretation |
|---|---|---|
| Pydantic / MergeWarden | iteration 0，22,251 prompt + 4,096 completion tokens，`finish_reason=length`，无 tool call | 分析触及 gold 相关兼容性变化，但未提交；verifier 未运行 |
| pytest / MergeWarden | iteration 0 完成两次 read；iteration 1 为 25,162 + 4,096 tokens，`finish_reason=length` | 明确发现 frozen-gold bug，但未提交；verifier 未运行 |
| pytest / baseline | iteration 0 完成 changed-context/read；iteration 1 为 28,191 + 4,096 tokens，`finish_reason=length` | 明确发现 frozen-gold bug，但未提交；verifier 未运行 |

### Contract closure verification

- Code state：`c3de180`，包含五个独立提交 `9b52faa`、`c53a0c9`、`c499c7d`、`6592c12`、`c3de180`。
- 全量检查：684 passed / 1 skipped；`mypy src` 通过 77 个 source files；`ruff check .`、touched-file format、Core fixture audit 5/5、`git diff --check` 全部通过。
- 冻结项：gold、matcher、warning/confidence threshold、clean controls、fixture root cause、fail-closed 原则与 60k/80k token budget 均未修改；生产代码中没有 fixture、项目名、文件名或行号特例。
- 产物：`eval/outputs/core-eval-verifier-contract-final.json`（机器报告，按仓库规则忽略）、本 Markdown，以及机器报告引用的 10 个 JSONL event logs。
