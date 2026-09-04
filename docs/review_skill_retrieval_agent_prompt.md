# 给干净 Agent 的执行提示词

你是一个没有任何先前对话上下文的实现 Agent。请在本地仓库 `E:\PycharmProjects\MergeWarden-recovered` 中，完成 Review Skill Retrieval 的前三个 stacked PR，并做离线验收。不要只写计划；要实际修改代码、测试、创建本地分支并提交 commits。

## 必读材料

开始任何修改前，完整阅读：

1. `docs/review_skill_retrieval_architecture.md`
2. `docs/review_skill_retrieval_implementation_plan.md`
3. `src/analyzer/review_skills.py`
4. `src/analyzer/review_lifecycle.py`
5. `src/analyzer/prompts.py`
6. `src/analyzer/inference_engine.py`
7. `src/orchestrator/agent_loop.py`
8. `src/analyzer/context_state.py`
9. `src/analyzer/context_strategy.py`
10. `src/analyzer/context_planner.py`
11. `src/analyzer/context_builder.py`
12. `src/analyzer/context_priority.py`
13. `src/analyzer/diff_lines.py`
14. `src/analyzer/event_log.py`
15. `src/analyzer/run_summary.py`
16. `eval/schemas.py`、`eval/runner.py`、`eval/run.py`、`eval/run_summary.py`、`eval/compare.py`、`eval/gate.py`
17. 所有 review skill / experience / prompt / agent loop / observability / eval 相关测试。

必须沿真实调用链确认，不要只按文档机械编码。如果实现计划与代码事实冲突，选择最小、安全、可测试的调整，并在 delivery 文档记录理由。

## 仓库安全与边界

- 预期起点：分支 `codex/adapt-stashed-verifier-context`，HEAD `803385c`。
- 预期未跟踪文件只有本任务的三个 docs。若发现其他用户改动，保留它们，不覆盖；无法隔离时停止并报告。
- 禁止 `git reset --hard`、`git checkout --`、强推、删除用户文件、改写已有 commits。
- 不 push、不创建远端 PR；只创建本地 PR-ready stacked branches 和 commits。
- 使用 `apply_patch` 做手工文件修改。
- 不新增数据库、外部服务、Embedding、Vector Store、BM25、Agent Tool 或第三方依赖。
- 不修改生产 `review_skills/learned.jsonl` 和 `review_experience/feedback.jsonl` 的内容。
- 不做与任务无关的格式化或重构。

## 分支拓扑

严格创建：

```text
codex/adapt-stashed-verifier-context @ 803385c
  └─ codex/review-skill-retrieval-core
       └─ codex/review-skill-retrieval-runtime
            └─ codex/review-skill-retrieval-eval
```

先把三份 docs 提交到 PR 1。每个功能 commit 在提交前运行相关测试，禁止故意保留红色 commit。

## PR 1：Pure retrieval core

按 implementation plan 第 2 节完整实现：

- backward-compatible optional metadata；
- lifecycle/proposal/status round-trip；
- `SkillQuery / SkillMatch / SkillSelection`；
- diff + optional Graph manifest query extraction；
- deterministic hard filters、score、stable tie-break；
- Top-K、atomic hard-budget packing、oversized item `continue`；
-完整 Core fit invariant；
- stable bank digest；
- legacy `render()` 行为保留；生产行为本 PR 不切换；
- 所列 unit/regression tests。

建议 commits：

1. `docs: define review skill retrieval rollout`
2. `feat: add backward-compatible review skill routing metadata`
3. `feat: add deterministic review skill selection`

通过 PR 1 acceptance commands 后，记录结果，再从该 branch 新建 PR 2 branch。

## PR 2：Runtime + telemetry

按 implementation plan 第 3 节完整实现：

- Settings/env：mode(default sequential)、Top-K、char budget、legacy fallback limit；
- 首次 Review analyze 建 selection，run 内 pin；
- production Prompt 显式接收 selection，不再每轮隐式读 bank；
- sequential compatibility 与 deterministic rollout；
- Core-only safe fallback；
- `CONTEXT_TELEMETRY.review_skills` 及 review_complete 聚合；
- run summary/eval process metrics；
- multi-iteration fake-model pinning test；
- config/prompt/agent/observability/run-summary regressions。

建议 commits：

1. `feat: configure review skill retrieval rollout`
2. `feat: pin review skill selection per run`
3. `feat: expose review skill retrieval telemetry`
4. `test: cover retrieval runtime and observability`

通过 PR 2 acceptance commands 后，从该 branch 新建 PR 3 branch。

## PR 3：Eval + A/B readiness

按 implementation plan 第 4 节完整实现：

- EvalVariant 的 skill mode，保持旧配置兼容；
- optional expected skill IDs 与 retrieval metrics；
- 隔离的 fixed Eval Skill Bank；
- 至少 5 个真实 Golden fixtures 的人工、可解释标注；
- provider-free retrieval-only harness/report；
- sequential/deterministic runtime variant injection；
- compare/report/gate 兼容扩展；
- process/aggregate tests；
- acceptance runbook。

建议 commits：

1. `feat(eval): add review skill retrieval variants and metrics`
2. `test(eval): add a fixed skill bank and golden retrieval labels`
3. `feat(eval): report sequential versus retrieval comparisons`
4. `docs: add review skill retrieval acceptance runbook`

不要伪造 live model A/B。如果没有 provider/network 凭据，完成离线 harness 与全量测试，并在 delivery 文档明确标记 live gate 未执行。生产默认保持 sequential。

## 统一验收要求

逐 PR 执行 implementation plan 中的 targeted tests、ruff 与 `git diff --check`。PR 3 最后执行：

```powershell
python -m pytest -q
python -m ruff check src eval tests
```

如果仓库现有、与本改动无关的失败阻止全量验收：

1. 复现并确认；
2. 不顺手改无关问题；
3. 记录失败命令、测试名和与本改动无关的证据；
4. 所有相关 targeted tests 仍必须通过。

还要验证：

- `review_skills/learned.jsonl` 与 `review_experience/feedback.jsonl` 仍为空；
- 无新增 dependency；
- candidate/deprecated 永不加载；
- hard budget 无 violation；
- 同输入 retrieval deterministic；
- telemetry 不含 raw diff/source body；
- 三个 branch 的 base...head diff 只包含对应 PR 范围。

## 最终交付

新增并提交 `docs/review_skill_retrieval_delivery.md`，至少包含：

- 三个 branch 名；
- 每个 PR 的 base/head 与 commit list；
- 主要文件变化；
- 每条验收命令及结果；
- retrieval-only 指标；
- full test/ruff/diff-check 结果；
- production bank/hash 未变证明；
- 未执行的 live A/B gate；
- 已知风险与 rollback；
- 建议的 push / `gh pr create` 命令，但不要执行。

完成后停在 `codex/review-skill-retrieval-eval` 分支，保证工作区干净，并向主 Agent 返回简洁的 branches/commits/tests/blockers 摘要。

