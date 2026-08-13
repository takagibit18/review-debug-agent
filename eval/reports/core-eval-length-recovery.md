# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。
> Generated at：`2026-08-13T14:57:20.819483+00:00`。
> Review input：按 fixture 声明的 `full_pr` / `partial_pr` scope 从 Git range 派生。

## Infrastructure

- Core fixtures：5
- Successful workspace setups：10/10 attempts
- Completion failures：1
- Matcher：`core-semantic-v1`（deterministic, one-to-one, duplicate-aware）
- Shared runtime：`deepseek-v4-pro`，temperature 0，4096 output tokens，12000 prompt-context tokens，3 iterations，64 tool calls，60000/80000 token budget，12000 final-submit reserve，4000 finalize prompt-context tokens，1200 retained-evidence tokens

## Review Quality

Quality 仅统计 valid completions。

| Metric | Baseline | MergeWarden |
|---|---:|---:|
| Precision | 0.0% | 100.0% |
| Recall | 0.0% | 50.0% |
| F1 | 0.0% | 66.7% |
| High-severity Recall | — | — |
| False findings / PR | 0.00 | 0.00 |

## Reliability

| Metric | Baseline | MergeWarden |
|---|---:|---:|
| Valid completion rate | 80.0% | 100.0% |
| Placeholder/incomplete runs | 0 | 0 |
| Workspace failures | 0 | 0 |
| Validator failures | 1 | 0 |

## Agent Persistence and Recovery

Counts cover all measured attempts, including invalid completions.

| Runtime fact | Baseline | MergeWarden |
|---|---:|---:|
| Model-response journal writes | 11 | 13 |
| Draft findings created | 0 | 1 |
| Length recoveries attempted | 0 | 1 |
| Length recoveries succeeded | 0 | 1 |
| Length recoveries failed | 0 | 0 |

## Candidate Finding Funnel

Counts cover valid candidate runs only; missing legacy fields deserialize as zero.

| Stage | Baseline | MergeWarden |
|---|---:|---:|
| No finding submitted | 1 | 0 |
| Non-risk not routed | 0 | 0 |
| Pre-verifier rejected | 0 | 1 |
| Calibration / rescue routed | 0 | 0 |
| Semantic rejected | 0 | 0 |
| Deterministic rejected | 0 | 0 |
| Final risk findings | 0 | 1 |

## Per-fixture comparison

| Fixture | Gold | Baseline hit | MergeWarden hit | Baseline FP | MergeWarden FP | Valid A/B |
|---|---:|---:|---:|---:|---:|:---:|
| `golden_pydantic_pydantic_pr12117` | 1 | 0/1 | 1/1 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr9350` | 1 | invalid | 0/1 | — | 0 | no |
| `golden_pydantic_pydantic_pr12568` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12590` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr13969` | 0 | 0/0 | 0/0 | 0 | 0 | yes |

## Main failure mode

1/4 个 candidate attempts 未合法完成，其中 0 个没有 submit_review，1 个虽调用 submit_review 但得到 blank `summary=""` + `issues=[]`，0 个在 hard token cap 后结束；baseline 1 个 candidate fixtures 缺少 valid completion。当前实测瓶颈是 blank submit 的明确性校验/恢复边界，而不是 workspace、hard cap 或 semantic judge；review quality A/B 暂不可比较。

## Controls and optional oracles

本轮保留 3 个已稳定零问题 controls；未把它们包装成并不存在的 paired repair。
Executable oracles 暂未加入 Core gate，后续只为最适合的 functional bugs 增补。

## Deliberately deferred

- 每个 fixture × variant 只采集一次；失败保留在报告中，不自动付费补跑。
- 不增加统计显著性、几十个 fixtures 或复杂 composite score。
- 不为所有 findings 建 executable oracle 或 Docker benchmark。
- 不把现有零问题 controls 冒充 paired repairs；真正的 repair pair 后续按证据补充。

## README conclusion

在 5 个 real-world full-workspace PR 组成的小型精选集上，当前 MergeWarden 的 F1 为 66.7%，高于简单 baseline 的 0.0%；MergeWarden valid completion rate 为 100.0%。该结果用于说明当前实现的相对表现，不代表对更大 PR 分布的统计结论。
