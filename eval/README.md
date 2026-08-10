# 评测方案

## 当前主路径：Core Eval v1

当前阶段使用一个 **small curated evaluation set** 回答两个问题：

1. MergeWarden 是否比冻结配置的简单 `agent_search` baseline 更能发现真实 PR 问题；
2. 两个 variant 是否能可靠完成审查。

Core Eval v1 固定为 5 个 real-world full-workspace fixtures：

- Candidate：`pydantic#12117`、`pytest#9350`，各有一个人工复核、结构化的 `gold_findings`。
- Clean controls：`pydantic#12568`、`pydantic#12590`、`pytest#13969`，沿用已完成 full-workspace replay 的零问题样本。

配置位于 [`core_eval_v1.yaml`](core_eval_v1.yaml)。A/B 只比较：

- A：`A-agent-search`，graph disabled；
- B：`B-mergewarden`，当前 `graph_hybrid` 路径，每次使用 cold index，避免隐藏 cache 状态。

两边使用相同 model、temperature、4096 output-token cap、12k prompt-context budget、60k/80k soft/hard cumulative token budget、iteration/tool budget、fixture 与 judge。这组边界允许 graph-hybrid 完成一次上下文探索和一次结构化提交，同时避免无限扩张上下文；应用的日常交互默认值不受影响。默认每个 fixture × variant 只跑 1 次；只有 placeholder、incomplete、validator failure 或 runtime error 才重试，最多 3 次。

```bash
# 只检查 5 个 fixture 的 reviewed/full-workspace/gold 契约，不调用模型
python -m eval.core_eval audit

# 运行 5 × 2 的主 A/B；仅不稳定 case 会额外重试
python -m eval.core_eval run

# 从已有机器报告重新渲染
python -m eval.core_eval report --input eval/outputs/core-eval-v1.json
```

Core report 将两个维度分开：

- Review Quality（只统计 valid completions）：Precision、Recall、F1、High-severity Recall（有数据时）与 False findings / PR。
- Runtime Reliability（统计全部 attempts）：valid completion rate、placeholder/incomplete、workspace failure、fixture validation failure 与 output validator failure。

`core-semantic-v1` judge 以“是否为同一 underlying issue”为准，结合文件、允许容差的位置范围和 root-cause 文本做确定性语义匹配；它先去除明显重复 finding，再做全局一对一分配。因此一条 generated finding 不能重复命中多个 gold findings。旧 formal-readiness pipeline、历史 artifacts 和冻结 baseline 均保留，但不再是 Core Eval v1 的前置 gate。

## 概述

本目录包含两类能力：

- 黄金集自建 pipeline（GitHub 自动发现仓库 + PR 解析 + LLM 辅助标注）
- 评测执行模块（对 Agent 输出计算格式合法率、命中率、误报率、人工可接受度模板、耗时/Token）

## 目录结构

```
eval/
├── crawler/
│   ├── github_client.py
│   ├── pr_parser.py
│   ├── annotator.py
│   └── fixture_generator.py
├── schemas.py
├── runner.py
├── metrics.py
├── report.py
├── run.py
├── fixtures/          # 评测用例（固定输入 + 期望输出元数据）
│   ├── manifest.json
│   └── review_checklist.md
├── outputs/           # 评测产出物（.gitignore 已忽略）
└── README.md
```

## 评测策略

### 主路径：自建黄金集（Golden Set）

- **素材来源**：自动发现小型活跃开源仓库，并筛选已合并 bugfix PR 或被维护者指出问题的 closed/unmerged PR 候选
- **缺陷来源**：PR diff、可信 review 证据与 LLM 辅助标注 expected issues；正式黄金集必须人工复核
- **固定输入**：当前 fixture 包含 diff / 相关文件片段 / 错误日志（可选）；长期健壮形态见 [golden_fixture_snapshot_plan.md](../docs/golden_fixture_snapshot_plan.md)，目标是 PR diff + repo snapshot
- **期望行为**：
  - 检出类：输出命中目标问题类别
  - 结构类：结构化输出通过 JSON Schema 校验
  - 定位类：指向正确文件路径或行号范围

### 指标

| 指标 | 说明 |
|------|------|
| 格式合法率 | 输出通过结构化 schema 校验的比例 |
| 关键问题命中率 | 成功检出预设问题的比例 |
| 误报率 | 非预设问题的报告比例 |
| 人工可接受度 | 人工 spot-check 评分 |
| 耗时 / Token | 工程回归指标 |

### Review target semantics

For review fixtures, the primary input is the pull request diff. The temporary
sandbox contains the files from the fixture as post-diff context so the agent can
read surrounding code when needed. Expected issues should target problems that
the submitted diff introduces, exposes, or fails to fix; the eval is not a
general audit of the pre-diff repository.

When `diff_mode=True`, the eval measures review quality for the submitted diff.
File reads are contextual evidence only.

The robust fixture shape is `PR diff + repo snapshot`: when `input.workspace`
is present with `kind="git"`, the runner restores a full temporary repository
at `checkout_sha`, passes the PR diff as the review target, and lets read-only
tools inspect unchanged context only when needed. Expected review comments must
still map back to changed lines or changed hunks. Legacy `input.files` remains
supported as a sparse offline fallback.

### 补充维度：公开 benchmark

在黄金集跑通后，可从 SWE-bench 等公开数据中抽取少量实例做外推验证。子集规模、筛选规则需写入评测说明，与主评测通过/失败口径分开汇报。

## 运行

```bash
# 1) 自动抓取并生成 fixture（需要 GITHUB_TOKEN）
python -m eval.run crawl --max-repos 5 --max-prs-per-repo 3

# 1a) 生成 rejected PR 正样本候选（需要 GITHUB_TOKEN 与模型 API）
python -m eval.run crawl --suite golden_candidates --candidate-mode rejected-pr --max-repos 5 --max-prs-per-repo 3 --min-expected-issues 1

# 2) 跑评测（调用 AgentOrchestrator）
python -m eval.run eval --suite golden

# 默认本地评测使用 fixture 级并发，并为 review fixture 保留一轮只读工具上下文探索：
# EVAL_FIXTURE_CONCURRENCY=3, EVAL_REVIEW_MAX_ITERATIONS=2,
# EVAL_REVIEW_MIN_TOOL_ITERATIONS=1。这样避免模型第一轮直接 submit_review
# 导致 golden 正样本缺少必要上下文。

# 3) 基于已有报告重新渲染终端输出
python -m eval.run report --input eval/outputs/<timestamp>_report.json
```

### MVP+ eval gate

CI uses a soft eval gate to prevent obvious MergeWarden regression. It is not a hard merge decision for user pull requests. The current `golden` suite contains 4 positive should-detect fixtures and 2 negative zero-issue fixtures. All are `annotated_by=manual` and `reviewed=true`.

Workspace-backed fixtures are validated before model execution: every added line in `diff_text` must match the restored `checkout_sha` repository snapshot. A mismatch is treated as fixture validation failure, not as a model miss or false positive.

Fixtures that intentionally pin a pre-change snapshot may set
`input.workspace.apply_fixture_diff=true`. The runner then checks out the exact
`checkout_sha`, applies `input.diff_text` without moving HEAD, and runs the same
workspace validation against the patched files.

Workspace caches are targeted bare partial-clone caches, not full repository
mirrors. On a miss the runner initializes a bare cache, requests only the
fixture checkout SHA (falling back to the fixture PR head), materializes that
snapshot's tree and blobs, verifies it with lazy fetching disabled, and then
atomically publishes the cache. Later commits from the same repository are
added incrementally. Offline restores never fetch and fail explicitly when a
snapshot is absent or incomplete.

Selected fixtures can be prefetched and offline-verified before a measured run:

```bash
python -m eval.workspace_prefetch \
  --fixtures eval/fixtures/golden_real_requests_netrc_pr7205.json \
  --cache-dir eval/outputs/workspace_cache \
  --output eval/outputs/workspace_prefetch.json
```

Omit `--fixtures` to read the fixture manifest. The JSON result records each
checkout SHA, overlay-aware repository snapshot, cache size, and offline
checkout status. Held-out fixtures are rejected before cache access.

Graph A/B structural metrics use optional expected-issue annotations:
`structural_scope` (`local`, `direct_cross_file`, or `multi_hop`) and
`graph_observable`. Null remains unclassified and is excluded from the relevant
group denominator while still contributing to overall recall. Compact summaries
and terminal reports expose group recall plus structural and graph-observability
annotation coverage.

For the formal-readiness candidates, `pydantic#12117` is local and graph
unobservable because its compatibility mechanism is contained in
`pydantic/main.py`; the llxprt reverse fixture is graph-observable multi-hop
across retry classification, orchestration, and delay-policy files; the Haystack
reverse fixture is local and graph unobservable because all unsafe accesses are
inside `json.py`. The requests and pydantic documentation candidates contain no
expected issues, so no structural label is invented for them.

Paired Graph A/B runs support durable JSONL checkpoint/resume. The stable key
includes the experiment, fixture, sample, variant, overlay-aware repository
snapshot, and a hash of the frozen shared/variant contract. Every completed
attempt is appended with flush and fsync as `measured`, `invalid`, or
`workspace_failure`; warm priming is recorded separately and never completes a
measured key. Valid measured records resume without rerunning, while invalid
records are retained and retried by default.

```bash
python -m eval.graph_ab_pilot \
  --resume eval/outputs/graph-ab-formal-readiness/checkpoint.jsonl

# Preserve prior failures but do not retry them:
python -m eval.graph_ab_pilot --no-retry-invalid
```

Use `--no-resume` to atomically start a fresh journal. Checkpoint records omit
raw model output, prompt/API-key fields, event-log paths, and index paths.

The CI gate uses the stable MVP+ numeric target:

```bash
python -m eval.gate --report eval/outputs/ci_report.json --schema-validity-min 1.0 --hit-rate-min 0.6 --false-positive-rate-max 0.5
```

v0.2.0 can additionally compare a candidate report against a frozen baseline:

```bash
python -m eval.compare --baseline eval/baselines/v0.2.0-alpha.1.json --candidate eval/outputs/ci_report.json --output-json eval/outputs/ci_comparison.json
python -m eval.gate --report eval/outputs/ci_report.json --comparison eval/outputs/ci_comparison.json --schema-validity-min 1.0 --hit-rate-min 0.6 --false-positive-rate-max 0.5
```

The comparison fails when hit rate drops by more than `0.05`, false-positive rate increases, p95 latency grows by more than `60%`, or p95 token usage grows by more than `50%`. Process metrics include evidence binding, verifier accept/reject, first-pass acceptance, required-step completion, duplicate tool calls, and token cost per accepted finding.

- `schema_validity_rate >= 1.0`: every response must be valid structured output.
- `hit_rate >= 0.6`: at least 60% of positive golden fixtures must be matched.
- `false_positive_rate <= 0.5`: false positives above 50% fail the gate.

The current MVP+ closure baseline is documented in
[docs/mvp_plus_eval_closure.md](../docs/mvp_plus_eval_closure.md).

### 2026-05-18 MVP+ closure golden eval status

Latest baseline report: `eval/outputs/20260518_151719_report.json`.

- Suite shape: `golden`, 6 reviewed fixtures, 4 positive and 2 negative.
- Schema validity: `100.00%`.
- Hit rate: `75.00%` (3/4 positive fixtures), above the stable `60.00%` target.
- False positive rate: `0.00%`.
- Average latency: `49.74s`; P50 / P95 latency: `45.78s` / `74.24s`.
- Average tokens: `23,902`; P50 / P95 tokens: `20,375` / `33,748`.
- Matched positives: Nethermind `_gcKeeper`, pytest long parameter ID, and pytest `SafeHashWrapper.__eq__`.
- Remaining miss: `golden_pytest-dev_pytest_pr8513`, tracked as follow-up quality hardening.

Interpretation: this report is the current MVP+ numeric quality baseline. It clears the stable eval gate while both negative fixtures remain false-positive-free.

### Phase 2 readiness diagnostics

Eval now emits local diagnostics artifacts that can be consumed by future GitHub advisory workflows without re-reading raw JSONL logs by hand. A normal eval run writes the machine report plus two sidecar files next to it:

- `<report_stem>_diagnostics.json`: per-fixture reasons such as `hit`, `miss_filtered_issue`, `budget_capped`, `submit_invalid`, or `fp_negative`.
- `<report_stem>_run_summaries.json`: compact event-log summaries with finish reasons, budget state, submit validation errors, issue counts, model names, and token totals.

Useful local commands:

```bash
python -m eval.run report --input eval/outputs/20260518_151719_report.json --diagnostics
python -m eval.run diagnose --input eval/outputs/20260518_151719_report.json
python -m eval.run summarize-log --input eval/outputs/event_logs/<run>.jsonl
python -m eval.run trend "eval/outputs/*_report.json"
```

`trend` is intended for R10-R14 style comparisons. It reports the best run, current baseline, per-fixture hit history, false-positive history, and persistent positive-fixture misses such as `golden_pytest-dev_pytest_pr8513`.

### 2026-05-17 local golden eval status

Latest diagnostic report: `eval/outputs/20260517_152809_report.json`.

- Suite shape: `golden`, 6 reviewed fixtures, 4 positive and 2 negative.
- Schema validity: `100.00%`.
- Hit rate: `50.00%` (2/4 positive fixtures), below the stable `60.00%` target.
- False positive rate: `16.67%`.
- Average latency: `64.7s`; P50 / P95 latency: `57.5s` / `93.8s`.
- Average tokens: `21,745`; P50 / P95 tokens: `19,506` / `29,405`.
- Run shape: review eval used `EVAL_REVIEW_MAX_ITERATIONS=2`; round 0 gathered read-only context and round 1 forced `submit_review`. The run did not hit a budget hard cap or workspace checkout failure.

Interpretation: this report proves the MVP+ eval execution path is observable and debuggable, but it is not stable quality evidence. The run exposed stale fixture diff/snapshot drift; the runner now blocks that class of fixture issue before the model runs. A fresh golden eval is required before recording a new quality baseline.

## 产物说明

- `eval/fixtures/manifest.json`：fixture 索引
- `eval/fixtures/review_checklist.md`：人工审核清单（用于修正 LLM 草稿）
- `eval/outputs/*_report.json`：机器可读评测报告
- `eval/outputs/*_human_review.md`：人工可接受度打分模板（0-5）

## v0.2.3–v0.2.5 根因质量与消融 benchmark

通用 golden runner 现在可聚合 Root-Cause Coverage、Over/Under-Merge Rate、Repair-Unit Accuracy、Evidence Completeness、Final Finding Count、Finding Inflation Ratio，以及 Context Planner、关系图、cache 和 consolidator 过程指标。

不调用 provider 的最小可复现消融工具覆盖 A–I：

```bash
python -m eval.root_cause_benchmark --ablations A,B,C,D,E,F,G,H,I \
  --output artifacts/v025_benchmark.json
```

该工具使用 SafeHashWrapper 与 Vosk cache/独立问题 fixture，并实际构建代码图、Context Manifest、SQLite cache、增量更新和 resolver fallback。它不会伪造模型 token 或 tool-call 数据：因为没有 provider call，这两项记录为 0。完整指标定义和消融映射见 [根因级审查架构文档](../docs/v023_v025_root_cause_relation_graph.md#benchmark-与消融)。
