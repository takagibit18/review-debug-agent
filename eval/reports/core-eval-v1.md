# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。

## Infrastructure

- Core fixtures：5
- Successful workspace setups：10/10 attempts
- Completion failures：0
- Matcher：`core-semantic-v1`（deterministic, one-to-one, duplicate-aware）
- Shared runtime：`deepseek-v4-pro`，temperature 0，4096 output tokens，12000 prompt-context tokens，3 iterations，64 tool calls，60000/80000 token budget

## Review Quality

Quality 仅统计 valid completions。

| Metric | Baseline | MergeWarden |
|---|---:|---:|
| Precision | 0.0% | 0.0% |
| Recall | 0.0% | 0.0% |
| F1 | 0.0% | 0.0% |
| High-severity Recall | — | — |
| False findings / PR | 0.00 | 0.00 |

## Reliability

| Metric | Baseline | MergeWarden |
|---|---:|---:|
| Valid completion rate | 100.0% | 100.0% |
| Placeholder/incomplete runs | 0 | 0 |
| Workspace failures | 0 | 0 |
| Validator failures | 0 | 0 |

## Per-fixture comparison

| Fixture | Gold | Baseline hit | MergeWarden hit | Baseline FP | MergeWarden FP | Valid A/B |
|---|---:|---:|---:|---:|---:|:---:|
| `golden_pydantic_pydantic_pr12117` | 1 | 0/1 | 0/1 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr9350` | 1 | 0/1 | 0/1 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12568` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12590` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr13969` | 0 | 0/0 | 0/0 | 0 | 0 | yes |

## Main failure mode

4 个 valid candidate runs 共提出 4 条 raw findings，但其中 4 个 runs 在最终输出前被过滤；语义 verifier 拒绝 1 条，deterministic evidence gate 拒绝 3 条。当前首要问题是 verifier/evidence 链造成的 recall loss，而不是 workspace、completion 或 semantic judge failure。

## Controls and optional oracles

本轮保留 3 个已稳定零问题 controls；未把它们包装成并不存在的 paired repair。
Executable oracles 暂未加入 Core gate，后续只为最适合的 functional bugs 增补。

## Deliberately deferred

- 不为全部 case 默认运行 3 repeats；仅 runtime instability 才重试。
- 不增加统计显著性、几十个 fixtures 或复杂 composite score。
- 不为所有 findings 建 executable oracle 或 Docker benchmark。
- 不把现有零问题 controls 冒充 paired repairs；真正的 repair pair 后续按证据补充。

## README conclusion

在 5 个 real-world full-workspace PR 组成的小型精选集上，当前 A/B 的 F1 同为 0.0%，尚未显示稳定优势；MergeWarden valid completion rate 为 100.0%。该结果用于说明当前实现的相对表现，不代表对更大 PR 分布的统计结论。
