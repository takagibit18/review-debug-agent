# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。
> Generated at：`2026-08-12T07:37:23.867694+00:00`。
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
| Non-risk not routed | 0 | 0 |
| Pre-verifier rejected | 0 | 0 |
| Calibration / rescue routed | 0 | 0 |
| Semantic rejected | 0 | 1 |
| Deterministic rejected | 2 | 0 |
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

Candidate funnel: no finding submitted=0, non-risk not routed=0, pre-verifier rejected=0, semantic rejected=1, deterministic rejected=2, calibration/rescue routed=0, final risk=1. Risk reached final output; any remaining gold miss is attributable after the funnel rather than to an unspecified pre-verifier disappearance.

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

## Experiment interpretation

- Commit：Fix 3 `fix(verifier): bind evidence provenance from trusted context`（resulting SHA 在下一提交记录中回填）。
- 目的：模型只声明证据位置与语义角色；系统用实际 diff、成功工具结果和 Manifest 绑定 provenance，并把 canonical candidate ID 回填到每条 structured evidence。已正确的来源保持；错误来源仅在唯一可信表示覆盖该位置时纠正；零来源和多来源歧义继续 fail closed。
- Tests：全量 `pytest` 639 passed / 1 skipped；`mypy src` 通过 77 个 source files；全仓 Ruff、touched-file format check、Core fixture audit 5/5 与 `git diff --check` 均通过。
- 正式采集：按用户要求，这是本提交唯一正式 5 × 2 轮次；配置为 `repeat_on_instability=false`、`max_attempts=1`。10/10 valid completion，10/10 均为且仅为 attempt 1；candidate raw findings 4，semantic accepted 3，deterministic rejected 2，final risk 1，gold matched 1，3 个 clean controls 的 warning/critical FP 为 0。

### 本轮逐 case 结果

| Case | Raw / semantic | Deterministic / final | 结论 |
|---|---|---|---|
| Pydantic baseline | warning；semantic accepted | rejected；final 0 | 主 anchor `pydantic/main.py:405-418` 不含 changed line；verifier 另返回 2 条非位置文本，且两个测试 evidence 范围超过实际 retained diff hunk |
| Pydantic MergeWarden | warning；semantic rejected | deterministic 未检查；final 0 | verifier 以 claim/observed behavior/causal mechanism unsupported 拒绝；没有 provenance 误删 |
| pytest baseline | warning；semantic accepted | rejected；final 0 | impact 声称 `read_file` · `src/_pytest/fixtures.py:307-312`，但没有对应成功读取，继续 fail closed |
| pytest MergeWarden | warning；semantic accepted | passed；final 1；gold 1 | 可信来源与 candidate ID 绑定后通过既有 deterministic contract，命中 `SafeHashWrapper.__eq__` frozen gold |

### 是否让命中链路前进

合同实现向前移动，但本轮聚合漏斗没有改善。Fix 2 → Fix 3 的 deterministic rejection 从 1 增至 2，final risk / gold matched / clean FP 仍为 1 / 1 / 0；新增拒绝来自本轮 Pydantic baseline 输出 cohort，而不是已通过单元回归的绑定路径。可归因的改进是：read evidence 错标 diff 且只有一个成功 read 时可被纠正；空或错误的模型 candidate ID 被 canonical ID 覆盖；不存在位置与无法唯一确定来源仍被拒绝。本报告不把稳定的 pytest 命中或 66.7% F1 重新归因给 Fix 3。
