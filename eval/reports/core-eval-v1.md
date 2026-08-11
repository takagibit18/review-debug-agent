# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。
> Generated at：`2026-08-11T03:08:19.861569+00:00`。
> Review input：按 fixture 声明的 `full_pr` / `partial_pr` scope 从 Git range 派生。本轮 5/5 fixtures 均为 `full_pr`。

## Infrastructure

- Core fixtures：5
- Successful workspace setups：15/15 attempts
- Completion failures：7
- Matcher：`core-semantic-v1`（deterministic, one-to-one, duplicate-aware）
- Shared runtime：`deepseek-v4-pro`，temperature 0，4096 output tokens，12000 prompt-context tokens，3 iterations，64 tool calls，60000/80000 token budget

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
| Valid completion rate | 83.3% | 33.3% |
| Placeholder/incomplete runs | 1 | 6 |
| Workspace failures | 0 | 0 |
| Validator failures | 0 | 0 |

## Per-fixture comparison

| Fixture | Gold | Baseline hit | MergeWarden hit | Baseline FP | MergeWarden FP | Valid A/B |
|---|---:|---:|---:|---:|---:|:---:|
| `golden_pydantic_pydantic_pr12117` | 1 | 0/1 | invalid | 0 | — | no |
| `golden_pytest-dev_pytest_pr9350` | 1 | 0/1 | invalid | 0 | — | no |
| `golden_pydantic_pydantic_pr12568` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12590` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr13969` | 0 | 0/0 | 0/0 | 0 | 0 | yes |

## Main failure mode

6/8 个 candidate attempts 未合法完成，其中 6 个没有 submit_review，6 个在 hard token cap 后结束；mergewarden 2 个 candidate fixtures 缺少 valid completion。当前首要问题是 runtime reliability 与完整 PR 上下文的预算伸缩，而不是 semantic judge；review quality A/B 暂不可比较。

## Controls and optional oracles

本轮保留 3 个已稳定零问题 controls；未把它们包装成并不存在的 paired repair。
Executable oracles 暂未加入 Core gate，后续只为最适合的 functional bugs 增补。

## Deliberately deferred

- 不为全部 case 默认运行 3 repeats；仅 runtime instability 才重试。
- 不增加统计显著性、几十个 fixtures 或复杂 composite score。
- 不为所有 findings 建 executable oracle 或 Docker benchmark。
- 不把现有零问题 controls 冒充 paired repairs；真正的 repair pair 后续按证据补充。

## README conclusion

在 5 个 real-world full-workspace PR 组成的小型精选集上，当前合法完成不足，尚不能比较 A/B review quality；MergeWarden valid completion rate 为 33.3%。该结果用于说明当前实现的相对表现，不代表对更大 PR 分布的统计结论。
