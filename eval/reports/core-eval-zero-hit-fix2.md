# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。
> Generated at：`2026-08-12T06:54:42.000391+00:00`。
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
| No finding submitted | 0 | 1 |
| Non-risk not routed | 0 | 0 |
| Pre-verifier rejected | 0 | 0 |
| Calibration / rescue routed | 0 | 0 |
| Semantic rejected | 1 | 0 |
| Deterministic rejected | 1 | 0 |
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

Candidate funnel: no finding submitted=1, non-risk not routed=0, pre-verifier rejected=0, semantic rejected=1, deterministic rejected=1, calibration/rescue routed=0, final risk=1. Risk reached final output; any remaining gold miss is attributable after the funnel rather than to an unspecified pre-verifier disappearance.

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

- Commit：Fix 2 `fix(verifier): retain context for all finding evidence`（resulting SHA 在下一提交记录中回填）。
- 目的：bounded candidate context 不再只围绕 finding 主位置；按 `主位置 → cause → contract → trigger → impact → related` 为每个真实引用位置保留其声明来源，之后才用剩余预算补可信的同位置表示。
- Tests：全量 `pytest` 636 passed / 1 skipped；`mypy src` 通过 76 个 source files；全仓 Ruff、2 个 touched Python files 的 format check、Core fixture audit 5/5 与 `git diff --check` 均通过。
- 正式采集：这是本提交唯一正式 5 × 2 轮次。10/10 valid completion，10/10 首次 attempt；candidate raw findings 3，semantic accepted 2，deterministic rejected 1，final risk 1，gold matched 1，3 个 clean controls 的 warning/critical FP 为 0。

### 本轮逐 case 结果

| Case | Raw / semantic | Deterministic / final | 结论 |
|---|---|---|---|
| Pydantic baseline | warning；semantic rejected | deterministic 未检查；final 0 | verifier 判定 observed behavior 与 causal mechanism 不受证据支持；没有 provenance 误删 |
| Pydantic MergeWarden | 无 finding | 未路由；final 0 | 本轮模型输出漂移，不归因于 context 修复 |
| pytest baseline | warning；semantic accepted | rejected；final 0 | impact 声称 `read_file` · `src/_pytest/fixtures.py:308-312`，但本轮没有对应成功读取，按设计 fail closed |
| pytest MergeWarden | warning；semantic accepted | passed；final 1；gold 1 | finding 引用的多个 evidence location 都进入 bounded context，正常通过 deterministic gate 并命中 `SafeHashWrapper.__eq__` frozen gold |

### 是否让命中链路前进

是。与 Fix 1 相比，deterministic rejection 从 2 降到 1，pytest MergeWarden 的真实 finding 保持在 final risk 并命中 frozen gold，clean-control FP 仍为 0。由于每轮 reviewer 输出 cohort 会漂移，不能把所有总数变化都归因于代码；可归因的机制证据来自单元回归和离线复放：finding 明确引用的两个 diff hunks、非重叠成功 read window 与 symbol context 均可被保留，未读取的位置仍拒绝。本轮 pytest baseline 的剩余拒绝也精确证明 gate 没有因 Fix 2 被放宽。
