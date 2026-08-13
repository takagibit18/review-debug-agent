# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。
> Generated at：`2026-08-12T06:34:34.626143+00:00`。
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

## Candidate Finding Funnel

Counts cover valid candidate runs only; missing legacy fields deserialize as zero.

| Stage | Baseline | MergeWarden |
|---|---:|---:|
| No finding submitted | 0 | 0 |
| Non-risk not routed | 1 | 1 |
| Pre-verifier rejected | 0 | 0 |
| Calibration / rescue routed | 0 | 0 |
| Semantic rejected | 0 | 0 |
| Deterministic rejected | 1 | 1 |
| Final risk findings | 0 | 0 |

## Per-fixture comparison

| Fixture | Gold | Baseline hit | MergeWarden hit | Baseline FP | MergeWarden FP | Valid A/B |
|---|---:|---:|---:|---:|---:|:---:|
| `golden_pydantic_pydantic_pr12117` | 1 | 0/1 | 0/1 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr9350` | 1 | 0/1 | 0/1 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12568` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pydantic_pydantic_pr12590` | 0 | 0/0 | 0/0 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr13969` | 0 | 0/0 | 0/0 | 0 | 0 | yes |

## Main failure mode

Candidate funnel: no finding submitted=0, non-risk not routed=2, pre-verifier rejected=0, semantic rejected=0, deterministic rejected=2, calibration/rescue routed=0, final risk=0. The primary zeroing stage is `non-risk not routed` (2); the report no longer combines distinct pre-verifier, semantic, and deterministic losses.

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

## Experiment interpretation

该快照是 Commit 2 的第二个诊断 preflight，不是最终 Fix 2 结果。它已把直接 diff/read/symbol context 放到 Manifest 之前，但仍按“一个位置的全部可用来源”依次收集：主位置的 diff、工具 window 与 Manifest 副本会先于后续 trigger/impact 位置进入 bounded envelope。Pydantic graph finding 因而保留了主位置的冗余表示，却裁掉 `tests/test_construction.py` 中被 trigger/impact 明确引用的 diff hunk，最终仍有 2 条 `diff_evidence_context_missing`。正式实现改为两阶段收集：先让每个 evidence location 按 `主位置 → cause → contract → trigger → impact → related` 各保留一次声明来源，再在剩余预算中补同位置的其他可信表示。本 preflight 仅作为失败证据保留，不回写正式对比行。
