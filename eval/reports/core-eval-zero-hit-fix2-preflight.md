# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。
> Generated at：`2026-08-12T06:17:47.878992+00:00`。
> Review input：按 fixture 声明的 `full_pr` / `partial_pr` scope 从 Git range 派生。

## Infrastructure

- Core fixtures：5
- Successful workspace setups：10/10 attempts
- Completion failures：0
- Matcher：`core-semantic-v1`（deterministic, one-to-one, duplicate-aware）
- Shared runtime：`deepseek-v4-pro`，temperature 0，4096 output tokens，12000 prompt-context tokens，3 iterations，64 tool calls，60000/80000 token budget，12000 final-submit reserve，4000 finalize prompt-context tokens，1200 retained-evidence tokens

## Review Quality

Quality 仅统计 valid completions。

| Metric | Baseline | MergeWarden |
|---|---:|---:|
| Precision | 100.0% | 0.0% |
| Recall | 50.0% | 0.0% |
| F1 | 66.7% | 0.0% |
| High-severity Recall | — | — |
| False findings / PR | 0.00 | 0.00 |

## Reliability

| Metric | Baseline | MergeWarden |
|---|---:|---:|
| Valid completion rate | 100.0% | 100.0% |
| Placeholder/incomplete runs | 0 | 0 |
| Workspace failures | 0 | 0 |
| Validator failures | 0 | 0 |

## Candidate Finding Funnel

Counts cover valid candidate runs only; missing legacy fields deserialize as zero.

| Stage | Baseline | MergeWarden |
|---|---:|---:|
| No finding submitted | 1 | 0 |
| Non-risk not routed | 0 | 0 |
| Pre-verifier rejected | 0 | 0 |
| Calibration / rescue routed | 0 | 1 |
| Semantic rejected | 0 | 1 |
| Deterministic rejected | 0 | 1 |
| Final risk findings | 1 | 0 |

## Per-fixture comparison

| Fixture | Gold | Baseline hit | MergeWarden hit | Baseline FP | MergeWarden FP | Valid A/B |
|---|---:|---:|---:|---:|---:|:---:|
| `golden_pydantic_pydantic_pr12117` | 1 | 0/1 | 0/1 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr9350` | 1 | 1/1 | 0/1 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12568` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12590` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr13969` | 0 | 0/0 | 0/0 | 0 | 0 | yes |

## Main failure mode

Candidate funnel: no finding submitted=1, non-risk not routed=0, pre-verifier rejected=0, semantic rejected=1, deterministic rejected=1, calibration/rescue routed=1, final risk=1. Risk reached final output; any remaining gold miss is attributable after the funnel rather than to an unspecified pre-verifier disappearance.

## Controls and optional oracles

本轮保留 3 个已稳定零问题 controls；未把它们包装成并不存在的 paired repair。
Executable oracles 暂未加入 Core gate，后续只为最适合的 functional bugs 增补。

## Deliberately deferred

- 不为全部 case 默认运行 3 repeats；仅 runtime instability 才重试。
- 不增加统计显著性、几十个 fixtures 或复杂 composite score。
- 不为所有 findings 建 executable oracle 或 Docker benchmark。
- 不把现有零问题 controls 冒充 paired repairs；真正的 repair pair 后续按证据补充。

## README conclusion

在 5 个 real-world full-workspace PR 组成的小型精选集上，当前 MergeWarden 的 F1 为 0.0%，低于简单 baseline 的 66.7%；MergeWarden valid completion rate 为 100.0%。该结果用于说明当前实现的相对表现，不代表对更大 PR 分布的统计结论。

## Experiment interpretation

该快照是 Commit 2 的保留诊断 preflight，不是最终 Fix 2 结果。它确认了跨 evidence location 的收集路径已启用，但暴露同一位置内的预算顺序错误：graph Manifest 先占用 bounded verifier envelope，导致 finding 明确声明的 diff hunk被裁掉，Pydantic graph run 因而仍出现 4 条 `diff_evidence_context_missing`。后续修复为每个位置优先保留直接 diff/read/symbol context，再补 Manifest，并以相同配置重新运行正式 Fix 2。本 preflight 不回写正式对比行。
