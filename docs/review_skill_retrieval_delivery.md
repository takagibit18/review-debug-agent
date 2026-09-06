# Review Skill Retrieval 前三层 PR 交付与验收记录

日期：2026-09-05

## 1. 交付结论

前三个本地 stacked PR 已完成，分支线性、工作区干净，生产默认仍为 `sequential`。针对真实仓库复核追加的 hardening 也已在独立分支完成：修复 scoped trigger relevance gate、malformed metadata fail-safe、bounded legacy fallback，以及 Eval/Production loader contract parity。确定性 retrieval 通过固定 Skill Bank 的离线门；未执行需要 provider 凭据与预算的 live A/B，因此本次没有把生产默认切到 `deterministic`，也没有 push 或创建远端 PR。

## 2. Stack 与 PR base

| PR | Base | Head | 目的 |
|---|---|---|---|
| PR1 | `codex/adapt-stashed-verifier-context` (`803385c`) | `codex/review-skill-retrieval-core` (`831c423`) | metadata、lifecycle round-trip、deterministic filter/rank/packing/digest |
| PR2 | `codex/review-skill-retrieval-core` | `codex/review-skill-retrieval-runtime` (`99a1d78`) | rollout config、run-scoped pinning、显式 Prompt 注入、telemetry/summary |
| PR3 | `codex/review-skill-retrieval-runtime` | `codex/review-skill-retrieval-eval` (`e1dc81c`) | Eval variant/metrics、固定 bank、Golden 标签、报告与 A/B readiness |
| Review follow-up | `codex/review-skill-retrieval-eval` | `codex/review-skill-retrieval-hardening` (`630ea53`) | 针对性复核修复、契约对齐、holdout/验收补强 |

三个 `merge-base --is-ancestor` 检查均通过；每层 `base...head` diff 没有跨层内容泄漏。

## 3. Commit 清单

### PR1

- `6b09e97 docs: define review skill retrieval rollout`
- `c9d650d feat: add deterministic review skill selection`
- `831c423 fix: align skill routing with graph manifests`

### PR2

- `f8e429f feat: pin review skill retrieval per run`
- `99a1d78 fix: enforce review skill hard budget`

### PR3

- `ffdea06 feat(eval): add review skill retrieval variants and metrics`
- `da8806d test(eval): add fixed bank and golden retrieval labels`
- `73d47a8 docs: add review skill retrieval acceptance runbook`
- `8ad79d5 fix(eval): preserve fixed skill bank digest contracts`

### Review follow-up (`codex/review-skill-retrieval-hardening`)

- `095fbd5 fix: harden review skill retrieval semantics`
- `7ee2a9d fix(eval): align review skill loader contract`
- `630ea53 docs: stage review skill retrieval holdout`

## 4. 验收证据

| 范围 | 结果 |
|---|---|
| PR1 targeted | 58 passed；独立 clean agent 使用其验收集合复跑 59 passed |
| PR2 targeted A | 93 passed |
| PR2 targeted B | 37 passed；独立 clean agent 使用其验收集合复跑 87 passed |
| PR3 targeted | 89 passed |
| digest 修复回归 | 主 agent 73 passed；独立 clean agent 78 passed |
| Hardening targeted | `tests/test_review_skills.py tests/test_review_experience.py tests/test_agent_loop.py tests/test_skill_retrieval_eval.py tests/test_eval_runner.py tests/test_eval_run_cli_phase2.py tests/test_graph_ab_pilot.py`：175 passed |
| Ruff | `src`、`eval`、`tests`（排除既有 `eval/outputs` 历史产物）通过 |
| Diff hygiene | 三层 `git diff --check base...head` 全部通过 |
| 离线 retrieval | 6 个已标注真实 fixture；Recall@5 = 1.0；Precision@5 = 1.0；irrelevant rate = 0；budget loss = 0；candidate/deprecated selection = 0；hard-budget violation = 0 |
| Determinism | 相同输入重复运行报告一致；固定 bank digest 为 `16f444a4756464e59863b0a7a5be79b3fe1f98ed3e22fecdf7ee0651106fa13a` |
| 独立审查 | clean agent 初审发现 2 个 Eval digest 问题；修复后复验 PASS，无剩余 actionable finding |

独立审查还确认了真实 Graph manifest 的 `edges[].kind`、active-only status gate、legacy fallback、完整 Core、oversize `continue` packing、稳定 tie-break/digest、同 run pinning、telemetry 不记录 raw diff、Eval denominator 和 fixed-bank digest contract。

## 5. 全量测试与既有环境阻塞

全量运行结果为 822 passed、1 skipped、3 failed。3 个失败最初都被本机全局 Git SSH signing 配置阻断，因为 `C:/Users/Lenovo/.ssh/id_ed25519` 不存在。仅对测试进程关闭 signing 后，revision-pinning 用例通过；剩余 2 个 provider integration 用例暴露本机 `openai 2.8.0` / `httpx 0.28.1` client wrapper 的 `_mounts` 兼容性问题，并在未包含 hardening 的 `803385c` 基线分支完全复现。因此它们记录为既有环境失败，不是本次回归。

直接对包含历史快照的整个 `eval/outputs` 运行 Ruff 会命中 27 个既有产物问题；源码验收使用 `--exclude eval/outputs`，本次改动文件和 `src/eval/tests` 的有效源码均通过。

## 6. 数据与依赖边界

- `review_skills/learned.jsonl`：0 字节，base blob 未变。
- `review_experience/feedback.jsonl`：0 字节，base blob 未变。
- 未修改依赖 manifest/lockfile。
- 未新增数据库、embedding、vector store、动态 Skill tool 或网络依赖。
- Eval Skill Bank 仅存在于 `eval/skill_banks/retrieval-v1/`，不进入生产 lifecycle data。
- `eval/holdout/retrieval-v1.json` 只登记真实 fixture 的 pending 候选；未完成人工 adjudication 前不进入 retrieval denominator。

## 7. 未执行 gate

Provider-backed live A/B 未执行，不能据此宣称最终 Review finding recall、false-positive rate、token cost 或 p95 latency 已优于 baseline。上线切换默认值前，必须按验收手册使用同一 model、temperature、fixture subset、samples、context mode、Graph cache mode 和相同非空 Skill Bank digest 完成 A/B。

## 8. Rollback

- 即时行为回滚：保持或设置 `REVIEW_SKILL_RETRIEVAL_MODE=sequential`。
- PR3 可独立 revert，不影响生产 Review。
- PR2 可在 PR3 之后 revert，不影响 lifecycle 数据格式。
- PR1 是向后兼容的数据模型与纯 selector；如需完全撤销，按 PR3 -> PR2 -> PR1 顺序回滚。

## 9. 远端提交顺序

如获授权 push/open PR，应严格按 PR1 -> PR2 -> PR3 顺序，并分别把 base 设置为 `codex/adapt-stashed-verifier-context`、`codex/review-skill-retrieval-core`、`codex/review-skill-retrieval-runtime`。每个下层 PR 合并后，再把后续 PR 的 base 重定向到最终目标分支。当前交付只包含本地分支和 commits。
