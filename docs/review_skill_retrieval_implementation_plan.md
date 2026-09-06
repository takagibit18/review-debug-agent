# Review Skill Retrieval：三阶段落地实施方案

本方案把 [`review_skill_retrieval_architecture.md`](./review_skill_retrieval_architecture.md) 的推荐架构拆成三个可独立审查、可回滚的 stacked PR。目标是完成代码落地与离线验收；不在未取得用户明确授权时 push 分支或创建远端 GitHub PR。

## 0. 基线、分支与交付方式

### 基线

- 仓库：`E:\PycharmProjects\MergeWarden-recovered`
- 基线分支：`codex/adapt-stashed-verifier-context`
- 基线提交：`803385c`
- 远端默认分支：`origin/main`，当前为 `460e1db`
- 基线比 `origin/main` 多一个 verifier-context 修复；前三个 PR 均以当前基线为起点，不擅自丢弃或重写该提交。

### Stacked branches

```text
codex/adapt-stashed-verifier-context @ 803385c
  └─ codex/review-skill-retrieval-core        # PR 1
       └─ codex/review-skill-retrieval-runtime # PR 2
            └─ codex/review-skill-retrieval-eval # PR 3
```

对应远端 PR base：

1. PR 1：`codex/review-skill-retrieval-core` → `codex/adapt-stashed-verifier-context`
2. PR 2：`codex/review-skill-retrieval-runtime` → `codex/review-skill-retrieval-core`
3. PR 3：`codex/review-skill-retrieval-eval` → `codex/review-skill-retrieval-runtime`

如果 `803385c` 先合入 `main`，则 PR 1 rebase 到最新 `main` 并改 base；PR 1 合并后，PR 2 rebase 到 `main`；之后同理处理 PR 3。不得 merge 三个分支彼此制造 merge commit。

### 本地开分支

先确认 HEAD 和工作区。预期只有本次新增的三份 docs 尚未跟踪；如果出现其他未说明改动，停止并报告。

```powershell
git switch codex/adapt-stashed-verifier-context
git switch -c codex/review-skill-retrieval-core
# 完成并提交 PR 1
git switch -c codex/review-skill-retrieval-runtime
# 完成并提交 PR 2
git switch -c codex/review-skill-retrieval-eval
# 完成并提交 PR 3
```

## 1. 跨 PR 的固定设计契约

这些契约不得在三个 PR 间漂移：

1. Core 是唯一 always-on procedural memory。
2. `candidate` 与 `deprecated` 永不参与生产 retrieval。
3. `active` 表示 eligible，不表示 always inject。
4. 不新增数据库、外部服务、Embedding、Vector Store、BM25 或 agent `load_skill` tool。
5. 继续使用 `review_skills/learned.jsonl` 与 `ReviewSkill`。
6. JSONL 新 metadata 全部可选；旧记录无须迁移即可读取。
7. 生产 learned skill 按完整记录原子装载，不做半条截断。
8. Core + wrappers + selected learned skills 必须满足 hard char budget；Core 不得静默截断。
9. 超预算候选使用 `continue`，不能 `break` 阻塞后续较短 Skill。
10. Ranking 与 tie-break 完全确定；不能依赖 JSONL 行序。
11. Skill selection 在一个 Review run 内 pin 住，各模型 iteration 不得漂移。
12. Graph 只提供 routing signal，不作为 bug evidence；Graph 失败时 diff-only 降级。
13. Runtime 默认先保持 `sequential`，通过配置和 Eval variant 开启 `deterministic`；没有完整 quality gate 前不静默切生产默认。
14. 不改生产 `review_skills/learned.jsonl` 的空内容；测试和 Eval 使用隔离的 fixture bank。

## 2. PR 1：Metadata + Deterministic Retrieval Core

### 目标

建立纯函数、无运行时接线的 retrieval core；保持现有 `render()` 调用兼容。PR 1 合并后生产行为不变。

### 主要代码变化

#### `src/analyzer/review_skills.py`

扩展 `ReviewSkill`：

```python
description: str = ""
languages: tuple[str, ...] = ()
path_globs: tuple[str, ...] = ()
triggers: tuple[str, ...] = ()
```

兼容规则：

- `description` 缺失时使用 `principle`；
- metadata 缺失时回落为空 tuple，保留旧记录兼容；类型不合法时记录字段 warning，deterministic retrieval 跳过该条，避免把坏 metadata 静默放大成全局适用；
- required fields/status 仍使用现有校验；
- `to_record()` round-trip 新字段；
- 规范化为 lowercase language/trigger，path 使用 `/`。

新增：

- `SkillQuery`
- `SkillMatch`
- `SkillSelection`
- `build_skill_query(diff_text, context_manifests=...)`
- `ReviewSkillLoader.retrieve(query, *, top_k=5)`
- 私有的 filter/score/glob/packing helpers
- stable Skill Bank digest，基于规范化 records + Core 内容

Filter/score/packing 按架构报告第 8 节实现。Score 常量集中定义，测试固定。Tie-break 使用 `skill.id`。`render()` 保持现有无 query sequential contract，供兼容测试和旧调用使用；不得让 PR 1 改生产 Prompt 行为。

Core 超过预算时不得静默切断。建议 `retrieve()` 抛出清晰 `ValueError`，并以测试/CI 确保仓库 Core 始终 fit；legacy `render()` 可暂保留旧行为，到 PR 2 生产路径不再使用它。

#### `src/analyzer/review_lifecycle.py`

- `SkillProposal` 接受同样的可选 metadata；
- `build_contrastive_prompt()` 要求/示例中加入 metadata，但允许人工 JSON proposal 省略；
- `add_candidate()` 写入 metadata；
- `update_status()` 重建 `ReviewSkill` 时必须保留 metadata。

### 测试

扩展：

- `tests/test_review_skills.py`
- `tests/test_review_experience.py`

必须覆盖：

- candidate/deprecated never retrieved；
- language/path/trigger ranking；
- irrelevant explicit scope filter；
- Top-K；
- total hard budget；
- oversized higher-ranked item 不阻塞后续 item；
- file-order-independent deterministic ordering；
- malformed optional metadata 的 warning、deterministic skip 与 sequential compatibility；
- old JSONL compatibility；
- metadata lifecycle round-trip；
- full Core inclusion；
- digest stability/change detection；
- Graph signals absent/present 的 query extraction。

### 建议 commits

每个 commit 必须自身通过对应 targeted tests：

1. `docs: define review skill retrieval rollout`
   - 提交架构报告、实施方案、clean-agent prompt。
2. `feat: add backward-compatible review skill routing metadata`
   - ReviewSkill/SkillProposal/store round-trip + tests。
3. `feat: add deterministic review skill selection`
   - query/filter/score/Top-K/budget/digest + tests。

### PR 1 验收门

```powershell
python -m pytest tests\test_review_skills.py tests\test_review_experience.py tests\test_github_feedback.py -q
python -m pytest tests\test_prompts.py tests\test_review_workflow.py -q
python -m ruff check src\analyzer\review_skills.py src\analyzer\review_lifecycle.py tests\test_review_skills.py tests\test_review_experience.py
git diff --check codex/adapt-stashed-verifier-context...HEAD
```

验收语义：现有无 query `render()` 测试不退化；新 retrieval tests 全绿；生产 Prompt 尚未切换。

## 3. PR 2：Runtime Wiring + Run-Scoped Pinning + Telemetry

### 目标

把 PR 1 的 selector 接入真实 Review 主链路，提供 sequential/deterministic rollout 开关，保证同一 run selection 固定并可观测。

### 配置

在 `src/config.py` 与 `.env.example` 增加：

```text
REVIEW_SKILL_RETRIEVAL_MODE=sequential|deterministic  # default sequential
REVIEW_SKILL_TOP_K=5
REVIEW_SKILL_CHAR_BUDGET=4000
REVIEW_SKILL_LEGACY_FALLBACK_LIMIT=1
```

必须做范围/枚举校验。`DEFAULT_SKILL_CHAR_BUDGET` 可保留为 config default source，但生产 loader 使用 Settings 值。

### 主链路

#### `src/orchestrator/agent_loop.py`

- constructor 支持测试注入 `ReviewSkillLoader` 或等价 selector dependency；
- `_reset_run()` 清空 selection cache/telemetry；
- 首次 Review analyze 在 effective diff 已解析、Graph manifests 已写入 state 后构造 query；
- 根据 mode 调用 legacy sequential selection 或 deterministic retrieval；
- 缓存同一 `SkillSelection`，后续 iteration 复用；
- selection/build 失败时 fail-safe：Core 仍进入，learned skills 为空，并记录 fallback；不得令整个 Review 因坏 optional metadata 崩溃；Core 本身越界属于配置错误，应明确记录。

不要为了此 PR 顺手重构 Graph strategy。CLI `--diff` 的 Graph timing gap可记录为独立后续问题；Skill retrieval 在 analyze 时必须能使用已加载 diff。

#### `src/analyzer/inference_engine.py` 与 `src/analyzer/prompts.py`

- 显式传递 `SkillSelection`/skill context；
- 生产 `review_system_prompt` 不再隐式创建 loader 并访问磁盘；
- 保留测试友好的兼容 overload，但生产调用必须显式；
- Debug path 不注入 Review Skill。

### Telemetry

优先复用 `CONTEXT_TELEMETRY`，加入 `review_skills` namespace：

- retrieval version/mode；
- selection id/bank digest；
- record/active/scoped/unscoped/candidate counts；
- loaded IDs、score、reasons；
- skipped IDs/reasons 和 reason counts；
- Top-K、budget、Core/learned/total chars、estimated tokens；
- retrieval latency；
- fallback/error class；
- iteration 是否 reuse pinned selection。

在 `review_complete` 放入聚合值，供：

- `src/analyzer/run_summary.py`
- `eval/run_summary.py`
- `eval/schemas.py` 的 `ReviewProcessMetrics`

提取 loaded count、Skill chars/tokens、retrieval latency 与 fallback count。完整 IDs/scores 只留 per-run event artifact。

### 测试

重点：

- deterministic mode 只注入 relevant active Skill；
- sequential mode 与旧 Prompt byte-level/semantic contract 兼容；
- same run 多 iteration selection 不变，即使磁盘 JSONL 中途变化；
- next run 可看到新 bank digest；
- Graph available/absent fallback；
- config defaults/validation；
- telemetry 字段完整且不包含 raw diff；
- Debug Prompt 无 Skills；
- Core-only safe fallback。

### 建议 commits

1. `feat: configure review skill retrieval rollout`
2. `feat: pin review skill selection per run`
3. `feat: expose review skill retrieval telemetry`
4. `test: cover retrieval runtime and observability`

允许把测试和对应功能放在同一 commit；禁止提交明知失败的中间 commit。

### PR 2 验收门

```powershell
python -m pytest tests\test_config.py tests\test_prompts.py tests\test_agent_loop.py tests\test_review_loop_observability.py tests\test_run_summary.py tests\test_runtime_run_summary.py -q
python -m pytest tests\test_review_skills.py tests\test_review_experience.py tests\test_review_workflow.py -q
python -m ruff check src tests
git diff --check codex/review-skill-retrieval-core...HEAD
```

另外做一个离线两轮 fake-model test，证明 selection pinning，而不是只测 selector 纯函数。

## 4. PR 3：Eval Variant + Retrieval Accuracy + A/B Readiness

### 目标

使 retrieval 自身和最终 Review quality 都可评测；提供固定 Skill Bank、真实 Golden PR annotations、variant injection 和报告字段。无 provider 凭据时完成全部离线验收，并明确标记 live A/B 未执行，不能伪造结果。

### Eval data contract

#### `eval/schemas.py`

- `EvalVariant` 增加 `skill_retrieval_mode`，默认 sequential 保持旧 YAML/CLI 兼容；同时允许显式覆盖 `skill_top_k`、`skill_char_budget`、`skill_legacy_fallback_limit`，未指定时解析同一组 Settings；
- fixture expected/result 增加可选 `expected_skill_ids`；
- 增加 retrieval metrics：Recall@K、Precision@K、irrelevant rate、budget-loss rate、loaded count/chars/tokens、latency；
- 聚合逻辑对未标注 fixture 不计入 retrieval denominator。

#### Fixture Skill Bank

新增隔离目录，例如：

```text
eval/skill_banks/retrieval-v1/
  core.md
  learned.jsonl
```

包含少量经过人工可解释的 active/candidate/deprecated skills，覆盖 concurrency、compatibility fallback、precision/boundary、contracts、error handling 等。不得改生产空 bank。

至少为 5 个现有真实 Golden PR fixtures 添加 `expected_skill_ids`，同时包含正样本、clean control、Python 与非 Python/unknown language 路径。标注必须根据 fixture diff/expected findings 手工核对，并在一个 README/manifest 解释理由。另建独立 holdout manifest，先登记真实 fixture 的候选角色与标注状态；未完成人工 adjudication 的条目不得进入 retrieval denominator。

### Retrieval-only harness

新增轻量离线 runner（可放 `eval/skill_retrieval.py`）：

- 读取 fixture diff 与固定 bank；
- 构造 query 并 select；
- 输出 per-fixture expected/retrieved/scores/reasons；
- 汇总 Recall@K、Precision@K、irrelevant rate、budget loss；
- 完全不调用 provider。

### Golden Review A/B

扩展现有 eval CLI/runner：

```text
baseline:  same context/model/settings + skill_retrieval_mode=sequential
candidate: same context/model/settings + skill_retrieval_mode=deterministic
```

固定 Skill Bank path/digest，并把 digest 写入 Eval report。继续复用现有 finding hit/FP/tokens/tools/latency 指标。`eval/compare.py` 展示 Skill 指标 delta；需要时给 gate 增加阈值，但不得改变未启用 Skill A/B 的旧 gate 语义。

### 建议 commits

1. `feat(eval): add review skill retrieval variants and metrics`
2. `test(eval): add a fixed skill bank and golden retrieval labels`
3. `feat(eval): report sequential versus retrieval comparisons`
4. `docs: add review skill retrieval acceptance runbook`

### PR 3 离线验收门

```powershell
python -m pytest tests\test_eval_runner.py tests\test_eval_process_metrics.py tests\test_eval_gate.py tests\test_eval_artifacts.py tests\test_review_skills.py -q
# 执行新增 retrieval-only CLI/runner，输出 JSON 报告
python -m pytest -q
python -m ruff check src eval tests
git diff --check codex/review-skill-retrieval-runtime...HEAD
```

最低 retrieval-only gate：

- 所有标注 fixture 都成功解析；
- expected relevant skills 的 Recall@5 = 1.0；
- candidate/deprecated selection count = 0；
- hard budget violation count = 0；
- 相同输入重复运行报告（排除 timestamp）一致；
- 旧 EvalVariant/YAML 仍可解析。

### Live A/B gate（需要 provider/network，不能伪造）

在凭据和预算可用时，用相同 model、temperature、fixtures、samples、context mode、Graph cache mode 运行两组：

- candidate finding recall 不低于 baseline；
- false-positive rate 不高于 baseline；
- schema/workflow validity 不退化；
- skill prompt tokens/chars 下降或相关性提高；
- tool calls 与 end-to-end p95 无不可接受回归；
- report 中两个 Skill Bank digest 完全一致。

只有这个 gate 通过后，才另起极小 commit/PR 把生产 default 从 sequential 改成 deterministic。前三个 PR 不强行切默认。

## 5. 独立最终验收

完成 PR 3 后，由主 Agent 做与实现 Agent 独立的复核：

1. 检查三个 branch 的 commit topology；
2. 分别检查每个 PR base...head diff，没有跨层泄漏；
3. 核对 production bank 未变；
4. 核对没有新增 dependency/database/tool；
5. 复跑 targeted tests、full tests、ruff、diff check；
6. 阅读核心实现，重点查：状态过滤、legacy fallback、Core budget、`continue` packing、stable tie-break、run pinning、telemetry data leakage、Eval denominator；
7. 输出 `docs/review_skill_retrieval_delivery.md`，记录 branches、commits、tests、未执行的 external gates 与 rollback。

## 6. Rollback

- PR 1 是纯能力与兼容 API，可单独保留或 revert。
- PR 2 的即时 rollback 是设置 `REVIEW_SKILL_RETRIEVAL_MODE=sequential`；代码问题再 revert PR 2，不影响 lifecycle data。
- PR 3 只改 Eval/schema/fixtures，可独立 revert，不影响 production Review。
- JSONL 新字段为可选；旧代码忽略未知字段的能力需在兼容测试中验证。不得用一次性 destructive migration。
