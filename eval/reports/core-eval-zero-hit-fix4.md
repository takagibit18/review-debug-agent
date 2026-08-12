# MergeWarden Core Eval v1

> small curated evaluation set: 5 个 full-workspace PR fixtures （2 个 candidate，3 个 clean control）。
> 该结果用于项目能力验证，不主张统计代表性或显著性。
> Generated at：`2026-08-12T08:04:02.879402+00:00`。
> Review input：按 fixture 声明的 `full_pr` / `partial_pr` scope 从 Git range 派生。

## Infrastructure

- Core fixtures：5
- Successful workspace setups：10/10 attempts
- Completion failures：8
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
| Valid completion rate | 20.0% | 20.0% |
| Placeholder/incomplete runs | 4 | 4 |
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
| Deterministic rejected | 0 | 0 |
| Final risk findings | 0 | 0 |

## Per-fixture comparison

| Fixture | Gold | Baseline hit | MergeWarden hit | Baseline FP | MergeWarden FP | Valid A/B |
|---|---:|---:|---:|---:|---:|:---:|
| `golden_pydantic_pydantic_pr12117` | 1 | 0/1 | 0/1 | 0 | 0 | yes |
| `golden_pytest-dev_pytest_pr9350` | 1 | invalid | invalid | — | — | no |
| `golden_pydantic_pydantic_pr12568` | 0 | invalid | invalid | — | — | no |
| `golden_pydantic_pydantic_pr12590` | 0 | invalid | invalid | — | — | no |
| `golden_pytest-dev_pytest_pr13969` | 0 | invalid | invalid | — | — | no |

## Main failure mode

2/4 个 candidate attempts 未合法完成，其中 2 个没有 submit_review，0 个在 hard token cap 后结束；baseline 1 个、mergewarden 1 个 candidate fixtures 缺少 valid completion。当前首要问题是 runtime reliability 与完整 PR 上下文的预算伸缩，而不是 semantic judge；review quality A/B 暂不可比较。

## Controls and optional oracles

本轮保留 3 个已稳定零问题 controls；未把它们包装成并不存在的 paired repair。
Executable oracles 暂未加入 Core gate，后续只为最适合的 functional bugs 增补。

## Deliberately deferred

- 每个 fixture × variant 只采集一次；失败保留在报告中，不自动付费补跑。
- 不增加统计显著性、几十个 fixtures 或复杂 composite score。
- 不为所有 findings 建 executable oracle 或 Docker benchmark。
- 不把现有零问题 controls 冒充 paired repairs；真正的 repair pair 后续按证据补充。

## README conclusion

在 5 个 real-world full-workspace PR 组成的小型精选集上，当前 A/B 的 F1 同为 0.0%，尚未显示稳定优势；MergeWarden valid completion rate 为 20.0%。该结果用于说明当前实现的相对表现，不代表对更大 PR 分布的统计结论。

## Experiment interpretation

- Commit：Fix 4 `fix(verifier): support mixed trusted evidence sources`（resulting SHA 在提交后由最终交付记录）。
- 目的：逐条 evidence 独立验证 Manifest、diff 或成功工具来源；不再要求 issue 绑定 Manifest 后所有 evidence 都属于同一 Manifest。semantic verifier 的 revised issue 也从它实际收到的 bounded context 重新绑定并再次执行完整 deterministic validation。
- Tests：全量 `pytest` 644 passed / 1 skipped；`mypy src` 通过 77 个 source files；全仓 Ruff、touched-file format check、Core fixture audit 5/5 与 `git diff --check` 均通过。回归明确覆盖 Manifest+read、Manifest+diff、错误 hash、未读取位置、revised finding 正确重绑以及 verifier 新增未见位置拒绝。
- 正式采集：按单轮策略恰好生成 10 个 attempt-1 records，没有补跑。仅 Pydantic A/B 2/10 valid；从 pytest MergeWarden 开始的其余 8 个 attempts 都因 provider `connection_error` 成为 placeholder/incomplete，其中 7 个 total tokens 为 0。
- 有效 candidate 子集：raw finding 1，semantic accepted 0，deterministic rejected 0，final risk 0，gold matched 0。pytest A/B 均 invalid，无法验证本提交是否保持其 frozen-gold 命中。
- Clean controls：0/6 valid，因此 clean-control FP 是 `N/A`，不能把无有效输出写成 0 FP。

### 是否让命中链路前进

单元与集成合同向前移动；正式质量结果不可判定。2/10 valid 的矩阵不能与 Fix 3 的 10/10 valid 快照比较 deterministic rejection、final risk、gold recall 或 clean FP。按用户要求不自动重采，本失败快照原样保留；最近一份完整且可比较的正式结果仍是 Fix 3（deterministic reject 2、final risk 1、gold matched 1、clean FP 0）。
