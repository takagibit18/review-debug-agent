# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。
> Generated at：`2026-08-12T05:34:57.890487+00:00`。
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
| Precision | 0.0% | 100.0% |
| Recall | 0.0% | 50.0% |
| F1 | 0.0% | 66.7% |
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
| No finding submitted | 0 | 0 |
| Non-risk not routed | 1 | 0 |
| Pre-verifier rejected | 0 | 0 |
| Calibration / rescue routed | 0 | 0 |
| Semantic rejected | 0 | 0 |
| Deterministic rejected | 1 | 1 |
| Final risk findings | 0 | 1 |

## Per-fixture comparison

| Fixture | Gold | Baseline hit | MergeWarden hit | Baseline FP | MergeWarden FP | Valid A/B |
|---|---:|---:|---:|---:|---:|:---:|
| `golden_pydantic_pydantic_pr12117` | 1 | 0/1 | 0/1 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr9350` | 1 | 0/1 | 1/1 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12568` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12590` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr13969` | 0 | 0/0 | 0/0 | 0 | 0 | yes |

## Main failure mode

Candidate funnel: no finding submitted=0, non-risk not routed=1, pre-verifier rejected=0, semantic rejected=0, deterministic rejected=2, calibration/rescue routed=0, final risk=1. Risk reached final output; any remaining gold miss is attributable after the funnel rather than to an unspecified pre-verifier disappearance.

## Controls and optional oracles

本轮保留 3 个已稳定零问题 controls；未把它们包装成并不存在的 paired repair。
Executable oracles 暂未加入 Core gate，后续只为最适合的 functional bugs 增补。

## Deliberately deferred

- 不为全部 case 默认运行 3 repeats；仅 runtime instability 才重试。
- 不增加统计显著性、几十个 fixtures 或复杂 composite score。
- 不为所有 findings 建 executable oracle 或 Docker benchmark。
- 不把现有零问题 controls 冒充 paired repairs；真正的 repair pair 后续按证据补充。

## README conclusion

在 5 个 real-world full-workspace PR 组成的小型精选集上，当前 MergeWarden 的 F1 为 66.7%，高于简单 baseline 的 0.0%；MergeWarden valid completion rate 为 100.0%。该结果用于说明当前实现的相对表现，不代表对更大 PR 分布的统计结论。

## Experiment interpretation

本快照对应只增加 deterministic rejection diagnostics 的 Fix 1；没有改变任何 evidence gate 通过条件。与 Before 相比，pytest baseline 从 warning 变为 info，pytest MergeWarden 则提交了可通过旧 gate 的新 evidence，因此本轮 Final Risk/Gold 命中变化属于模型输出 cohort 漂移，不能归因给诊断代码。逐 case 原因及 Before 对照见 `core-eval-zero-hit-chain.md`。
