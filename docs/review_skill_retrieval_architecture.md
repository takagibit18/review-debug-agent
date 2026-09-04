# MergeWarden Review Skill Retrieval 架构调查报告

调查基线：本地仓库 `803385c`（2026-09-01），分支 `codex/adapt-stashed-verifier-context`。本报告只做架构调查与实施设计，不修改 Review Skill 运行时代码。

## 结论先行

当前最合适的演进不是 BM25、Embedding 或模型主动 `load_skill`，而是：

> Core 保持 always-on；`active` 改为“可参与生产检索”；用轻量、可选的 metadata 做确定性过滤和排序；按 Top-K 与现有 4000-char hard budget 选择完整 Skill；把选择结果写入现有事件日志和 Golden PR Eval。

这项工作当前是 **scale-readiness**，不是线上性能修复：仓库中的 `review_skills/learned.jsonl` 和 `review_experience/feedback.jsonl` 都是空文件，当前没有 learned skill 会污染 Prompt。但顺序垄断在代码机制上已经被确认；一旦 Skill Bank 增长，它会成为确定性、可复现的召回缺陷。

## 1. Current implementation

### 1.1 持久化与生成链路

当前有两个 append-oriented JSONL store：

- `review_experience/feedback.jsonl` 保存人工反馈。`FeedbackStore.append()` 去重并追加记录。
- `review_skills/learned.jsonl` 保存 learned skills。`SkillStore.add_candidate()` 追加 candidate，ID 按现有 `skill-NNN` 最大序号递增。
- `review_skills/core.md` 是 always-on Core Skill。

真实生成链路为：

```text
GitHub reply / CLI record
  -> FeedbackStore
  -> CLI propose --model 或 --proposal-json
  -> contrastive prompt / StaticImprover
  -> SkillProposal
  -> SkillStore.add_candidate()
  -> learned.jsonl(status=candidate)
```

`PromptImprover` 要求模型从 agent reasoning 与 human correction 中提炼可迁移的 `category / principle / why / source_feedback_ids`。生成不是 Review runtime 的自动后台步骤，而是显式 CLI workflow。

证据：[`review_lifecycle.py`](../src/analyzer/review_lifecycle.py)、[`review_improver.py`](../src/analyzer/review_improver.py)、[`review_experience.py`](../scripts/review_experience.py)、[`github_feedback.py`](../src/integrations/github_feedback.py)。

### 1.2 Lifecycle 的真实语义

状态机只有：

```text
candidate --activate--> active --deprecate--> deprecated
```

代码约束是：只有 candidate 可激活，只有 active 可废弃。`activate` 是人工执行的 CLI 命令；仓库中没有把 skill-specific Eval 结果绑定到 `update_status(..., "active")` 的自动门禁。因此“Eval / Human-in-the-loop → Active”中，HITL 已存在，Eval gate 尚未成为可验证的运行时契约。

### 1.3 哪些 Skill 会进入 Prompt

`ReviewSkillLoader.load_active_skills()`：

1. 按 `learned.jsonl` 的物理行顺序读取；
2. 忽略空行和 JSON 解析失败的行；
3. 用 `ReviewSkill.from_record()` 校验；
4. 只返回 `status == "active"` 的记录。

candidate 和 deprecated 都不会进入 Review Prompt。Core 先进入，所有能在剩余预算中顺序装下的 active skills 随后进入。Debug Prompt 不加载 Review Skill。

### 1.4 加载时机与 Review 主链路

GitHub PR 流程先生成 revision diff，再构造 `ReviewRequest(diff_mode=True, diff_text=...)`。Agent 主链路为：

```text
run_review
  -> prepare_context
  -> ContextStrategy.prepare
       graph_hybrid: build/reuse graph -> changed anchors -> manifests
       agent_search: empty manifests
  -> analyze (每轮)
       resolve diff / changed file contents / project structure
       -> InferenceEngine.analyze
       -> build_review_messages
       -> review_system_prompt
       -> ReviewSkillLoader().render()
       -> model call
  -> finding integrity / workflow / telemetry
```

也就是说 Skill 在 **每次模型调用构建 System Prompt 时** 从磁盘读取，不是在 lifecycle、Graph build 或 verifier 阶段加载。当前没有 run-scoped skill snapshot；理论上同一 Review 期间修改 JSONL 会令不同 iteration 看见不同 Skill 集合。

CLI `review --diff` 创建的 `ReviewRequest` 没有 `diff_text`；本地 diff 到 `AgentOrchestrator.analyze()` 才加载。Graph strategy 更早执行，所以此入口当前得不到 Graph manifests。Skill retrieval 若放在统一的 `analyze/message build` 边界可以覆盖它；若只放在 Graph strategy 会漏掉它。

证据：[`github_pr_review.py`](../src/integrations/github_pr_review.py)、[`agent_loop.py`](../src/orchestrator/agent_loop.py)、[`inference_engine.py`](../src/analyzer/inference_engine.py)、[`prompts.py`](../src/analyzer/prompts.py)。

### 1.5 4000-char budget 的精确行为

`DEFAULT_SKILL_CHAR_BUDGET = 4000` 是 `review_skills.py` 中的常量，不是 Settings/env 配置。算法为：

1. 预算先扣 `<review_skills>\n` 和 `\n</review_skills>`；
2. `core.md` 以 `core[:available]` 放入，因此 Core 过长时会被静默截断；
3. active skill 渲染为两行 `principle + why`，不包含 id；
4. 按文件顺序逐个试装；
5. 第一个装不下的 Skill 触发 `break`，后续 Skill 不再被考察；
6. learned skill 不会被局部截断。

当前实测：wrapper 33 chars、Core 443 chars、已渲染 Skill section 476 chars、active learned count 0，learned skills 尚有 3524 chars 可用。

这个 4000-char budget 与 `PROMPT_INPUT_TOKEN_BUDGET` 是两套独立预算。后者只选择 user payload 的 meta/diff/manifests/files/structure；System Prompt 中的 Skills 不参加 `ContextBuilder.truncate_context()`。所以 4000 chars 确实限制 Skill section，但不代表完整 model request 被同一个 token budget 覆盖。

## 2. Confirmed limitations

| 发现 | 状态 | 当前严重度 | 扩展后严重度 |
|---|---|---:|---:|
| 无 PR relevance filter/ranking，active 即顺序注入 | 已确认 | 无实际影响（0 learned） | 高 |
| 第一个超预算 Skill 触发 `break`，后续更短或更相关 Skill 无机会 | 已确认 | 无实际影响 | 高 |
| 顺序由 JSONL 行序决定；add 保持追加，status rewrite 保持原位置 | 已确认 | 无实际影响 | 高 |
| Context pollution | 机制上成立、当前未发生 | 无 | 中到高 |
| early-skill budget monopoly | 机制上已确认；现有测试还固定了该行为 | 无 | 高 |
| `active` 等同于 always inject | 已确认 | 低 | 高 |
| Core 可能被静默截断 | 已确认的边界条件；当前 Core 未超限 | 低 | 中 |
| Skill 选择无专属 telemetry | 已确认 | 中 | 高 |
| 同一 run 没有 Skill Bank snapshot/hash | 已确认 | 低 | 中 |
| 自动 Eval promotion gate | 不存在；当前只有人工 activate | 流程风险 | 流程风险 |
| framework/repository metadata 与 PR title/body/labels | 当前 schema 不提供 | 低 | 视需求而定 |

还有两个次要行为：loader 对坏 JSON 静默跳过，而 `SkillStore.read()` 对坏记录报错；重复/高度相似 Skill 没有去重。它们不是 retrieval MVP 的主问题，但应进入 telemetry 与后续治理。

## 3. Existing reusable abstractions

可直接复用：

- `ReviewSkill` / `ReviewSkillLoader` / `SkillStore`：继续作为 JSONL 与 lifecycle 边界，不新建数据库。
- `ContextBuilder.estimate_tokens()`：为选中 Skill 补充 estimated token telemetry。
- `ContextBuilder.truncate_context()` 的设计思想：先按确定性 priority 排序、超预算项 `continue`，与 Skill loader 当前 `break` 的缺陷形成直接参考。不要直接把 procedural memory 混入 code context parts，但可复用“rank + skip + hard budget”的 packing 规则。
- `ChangeCenteredContextPlanner`：已经有确定性 score、稳定 tie-break、预算拒绝原因和 manifest telemetry，适合作为实现风格参考；不应复用它的 code-graph data model。
- `ContextState.candidate_context_manifests`：Graph signals 已经在首次 Review model call 前可读。
- `EventLog` + `CONTEXT_TELEMETRY`：已有逐 model call 的 JSONL 遥测，适合追加 skill selection 字段；无需另建 telemetry backend。
- Golden fixture runner / `EvalVariant` / `ReviewProcessMetrics` / report comparison：可扩展成 sequential 与 retrieval 两个 variant，继续测 finding recall、FP、tokens、tools、latency。
- `parse_unified_diff_hunks()` / `changed_new_lines_by_file()` / `extract_changed_anchors()`：可在不调用 LLM 的情况下得到 changed files、hunks、changed lines、change kind；Graph 可用时再补 symbol/edge 信号。

不能直接复用或当前不存在：

- 没有 Review Skill 的通用 rank/filter abstraction。
- 没有 PR-level framework detector。
- 没有 Graph communities 数据；Prompt 虽提到 community，Graph model/manifest 没有 community 字段。
- Graph language support 当前只覆盖 Python/Rust/C#；不能把它当作所有 PR 的语言检测器。
- 现有 GlobTool 是面向仓库文件枚举的 agent tool，不适合作为 metadata path match API；Skill routing 应使用纯函数 glob matching。

## 4. Available runtime signals

### 4.1 无需 LLM、所有 diff review 可用

- repo path、diff mode；
- unified diff 全文；
- changed files、扩展名、目录与 basename；
- new-side changed lines；
- hunk headers 与新增/删除文本；
- 可通过扩展名确定的语言；
- 从 diff 文本确定性提取的 import/package names、关键词、decorator 与明显 symbol names；
- project structure 和预读的 changed file contents（它们在 analyze 阶段可用，但 MVP 不必扫描全文）。

`ReviewRequest` 当前不含 PR title、body、labels、owner/repo、PR number、base/head SHA。GitHub 上游掌握其中一部分，但创建 analyzer request 时只传 repo path、diff 与 model。因此不要把这些信号写成 MVP 已可用能力。

### 4.2 仅 graph_hybrid 且 Graph 成功时可用

`candidate_context_manifests` 提供：

- `changed_anchor.file / line / changed_lines / hunk_header / hunk_text`；
- enclosing `symbol_id`；
- `change_kind`（如 field_state、signature、type_protocol、api_handler）；
- included spans 的 file、symbol_id、role；
- graph path 的 node ids、edge kinds、path、semantic role、confidence 与 eligibility；
- imports/references/calls/field reads-writes/tests/inheritance/implements 等关系类型。

这些值可以是 Skill routing 的弱/强信号，但仍只是 navigation signal。它们不能证明 Skill 适用，更不能证明 Bug 存在。

### 4.3 推荐的信号优先级

MVP 使用：changed paths、languages、bounded diff lexical triggers；Graph 可用时补 change kinds、symbol IDs/names、edge kinds。Framework、repository identity 与 historical utility 暂缓，直到有可靠数据源。

## 5. Architecture options comparison

评分 1（差）到 5（强）；Complexity 单独使用 Low/Medium/High。Recall 是 Skill retrieval recall，不是最终 finding recall。

| 方案 | Complexity | Determinism | Recall | Context Efficiency | Latency | Maintainability | Evalability | Backward Compatibility |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 当前 sequential budget | Low | 5 | 2（规模化后） | 1 | 5 | 4 | 3 | 5 |
| Metadata filter + deterministic ranking | Low-Medium | 5 | 4 | 4 | 5 | 4 | 5 | 5（可选字段） |
| Metadata filter + BM25 | Medium | 5（固定 tokenizer 时） | 4 | 4 | 4 | 3 | 5 | 4 |
| Embedding/vector retrieval | High | 2 | 4 | 5 | 2 | 1 | 2 | 3 |
| Agent-triggered `load_skill` | High | 1 | 2-3 | 5 | 1 | 2 | 1 | 3 |
| Runtime retrieval + optional dynamic load | High | 3 | 5 | 5 | 2 | 2 | 3 | 3 |

判断：

- **Deterministic metadata** 最符合当前代码、0 learned skill 的实际规模和可测性要求。
- **BM25** 在几十条 Skill 时收益不足。只有标注集证明 lexical relevance 排序是主要 miss 来源，且 Skill Bank 达到人工权重难维护的规模后再引入；可先用纯本地实现，仍不需要外部服务。
- **Embedding** 会引入模型版本、向量持久化、重建、成本、延迟和复现问题；当前没有证据支持。
- **Agent-driven load** 把 recall 依赖于模型“想起来调用”，还消耗本来就严格受限的 tool rounds。当前 runtime 已有 minimum exploration、pre-budget submit、force-submit 与 workflow gates，不应再把主要 Skill recall 交给可省略的 tool call。
- **Hybrid** 的合理含义在近期应是 deterministic coarse filter + deterministic lexical rank，而不是立即增加动态 tool。

## 6. Recommended architecture

推荐 V2.1：

```text
Core (always-on, validated to fit)
        +
ReviewRequest + resolved diff + optional Graph manifests
        -> SkillQuery（纯确定性特征）
        -> read active skills snapshot
        -> metadata hard eligibility filters
        -> deterministic relevance score + stable tie-break
        -> Top-K atomic packing
        -> 4000-char hard budget
        -> SkillSelection + telemetry
        -> one pinned System Prompt skill section for the whole run
```

语义调整：

- candidate：未批准，禁止生产 retrieval；
- active：已批准，**eligible for production retrieval**，不再代表每次注入；
- deprecated：不参与 retrieval，保留 provenance。

Lifecycle transition 本身无需改变。会变化的是 runtime selection contract，以及两个“激活后无条件出现在 render()”的测试预期。为保持 API 兼容，`render()` 无 query 时可保留 legacy sequential 行为；生产 Review 改为显式 `retrieve(query)`。上线用 feature flag/A-B，不做一次性切换。

分层方面，只保留真正影响 ownership/routing 的层：

- `core.md`：唯一 always-on 层；
- learned JSONL：用 metadata 表达 language/path/topic scope。

第一版不要为 Python/FastAPI/repository-specific 新建目录树。当前 loader、lifecycle 与 store 都是单文件模型，且 analyzer 没有稳定 repository identity；目录分层只会增加外观复杂度。未来确有不同 owner、审批策略或 repository store 时，再把层级变成真实 lifecycle boundary。

## 7. Proposed data model

### 7.1 JSONL：只新增可选字段

```json
{
  "id": "skill-017",
  "status": "active",
  "category": "concurrency",
  "description": "Avoid false race reports on serialized async paths.",
  "languages": ["python"],
  "path_globs": ["**/*.py"],
  "triggers": ["asyncio", "event loop", "create_task", "await"],
  "principle": "Confirm that paths can execute concurrently before reporting a race.",
  "why": "Shared mutable state alone does not establish concurrent execution.",
  "source_feedback_ids": ["github-review-comment-123"]
}
```

MVP 不加入 `frameworks`、`symbols`、`utility`、embedding、repository ids 或 learned `alwaysApply`。Framework 可先作为 trigger；Core 已承担 always-on。不要允许任意 regex trigger，避免正则安全/性能与复现负担；先做规范化 literal token/phrase match。

### 7.2 Python types

保留 `ReviewSkill`，新增三种最小类型：

```python
@dataclass(frozen=True)
class SkillQuery:
    changed_files: tuple[str, ...]
    languages: tuple[str, ...]
    lexical_corpus: str
    changed_symbols: tuple[str, ...] = ()
    change_kinds: tuple[str, ...] = ()
    graph_edge_kinds: tuple[str, ...] = ()

@dataclass(frozen=True)
class SkillMatch:
    skill: ReviewSkill
    score: int
    reasons: tuple[str, ...]

@dataclass(frozen=True)
class SkillSelection:
    context: str
    selected: tuple[SkillMatch, ...]
    skipped: tuple[tuple[str, str], ...]
    core_chars: int
    learned_chars: int
    total_chars: int
    estimated_tokens: int
    bank_digest: str
```

接口：

```python
loader.retrieve(query: SkillQuery, *, top_k: int) -> SkillSelection
loader.render() -> str  # legacy compatibility only
```

不要创建通用“Memory Retrieval Framework”。Code/Documentation Context 是 declarative knowledge；Review Skill 是 procedural memory。二者可共享 query signals、排序风格和预算原则，但保持不同数据模型、lifecycle 与 telemetry namespace。

### 7.3 Migration

- missing `description`：使用 `principle` 作为显示/未来 lexical fallback；
- missing `languages/path_globs/triggers`：视为 `unscoped_legacy`，不报错；
- malformed optional metadata：规范化失败后回落为空并记录 metadata warning，不丢弃整个合法 principle；
- existing required fields/status 的错误仍沿用当前 fail/skip 行为；
- status rewrite 必须保留所有新 metadata，避免 `SkillStore.update_status()` 重建对象时丢字段；
- migration 不重写全库。新 proposal 开始写 metadata，旧 active skills 逐条回填。

兼容 rollout：如果已有旧 active skill，unscoped records 以最低分进入 fallback pool。存在 scoped match 时最多补 1 条 legacy；完全没有 scoped match 时允许按 Top-K fallback，避免升级后所有旧经验突然消失。Telemetry 暴露 `unscoped_active_count`，完成回填后可收紧策略。当前仓库为 0 learned，迁移负担实际上为零。

## 8. Retrieval algorithm

### 8.1 Query extraction

只读取 already-available data：

1. 用统一 diff parser 得到 changed files/hunks；
2. 用共享、覆盖常见扩展名的纯函数得到 languages（不要依赖当前只支持 py/rs/cs 的 Graph mapper）；
3. lexical corpus 只组合 normalized changed paths、hunk headers、added/removed lines；设置最大 chars，避免超大 diff 令匹配成本失控；
4. 从 Graph manifests 补充 changed symbol ids、included span symbol ids、change kinds 与 edge kinds；
5. 不读网络、不调用 LLM、不把 graph edge 当 evidence。

### 8.2 Filter semantics

1. status 非 active：硬排除；
2. `languages` 非空且与 query 无交集：硬排除；
3. `path_globs` 非空且无 changed path 命中：硬排除；
4. trigger 作为强正向信号。只有 trigger metadata、没有 language/path scope 的 Skill 在 trigger 完全不命中时排除；已有 language/path scope 的 Skill 可保留低分，避免过严 trigger 导致 recall 损失；
5. unscoped legacy 进入低分 fallback pool。

### 8.3 Deterministic score

初始权重保持少而可解释，例如：

```text
+100  trigger phrase/token match（封顶，防关键词堆砌）
 +40  path_glob match（按最具体 match 封顶）
 +20  language match
 +15  changed symbol / change_kind / graph edge trigger match（封顶）
  +1  unscoped legacy fallback
```

按 `(-score, skill.id)` 排序；记录 reasons。权重应成为常量并由 fixture 固定，不在首版学习或在线调参。

### 8.4 Packing

1. 完整 Core 先放入；CI/test 保证 wrapper + Core 不超过 hard budget，禁止静默截断 Core；
2. 按 ranking 扫描；达到 Top-K 停止；
3. Skill 必须原子加入；超预算时记录 `budget` 并 `continue`，让后续较短 Skill 有机会；
4. wrapper + Core + learned skills 总长度始终 `<= max_chars`；
5. tie-break 不使用 JSONL 行序；
6. selection 与 bank digest 在 run 内 pin 住，所有 model iterations 使用同一结果。

## 9. Prompt loading flow

生产路径不再让 `review_system_prompt()` 隐式创建 loader 并无参 `render()`。建议显式依赖：

```text
AgentOrchestrator first analyze
  -> effective diff 已解析
  -> build SkillQuery(state manifests included)
  -> ReviewSkillLoader.retrieve(query)
  -> cache run-scoped SkillSelection
  -> emit selection telemetry
  -> InferenceEngine/build_review_messages(selection)
  -> review_system_prompt(skill_context=selection.context)
```

这能避免：Prompt builder 隐式 I/O、iteration 间 Skill Bank 漂移、测试无法精确注入 selection。Graph 不可用时 query 自动降级为 diff-only，不阻断 Review。

## 10. Telemetry design

当前已有 `context_telemetry` 事件能记录总 message chars/estimated tokens、各 user context part、tool schemas；最终 review envelope 记录 total tokens 和 latency。但它不能回答 loaded/skipped Skill IDs、score、Skill chars/tokens 或 retrieval latency，且 final `prompt_tokens/completion_tokens` 当前为 `None`。

建议在现有 JSONL `CONTEXT_TELEMETRY` 中增加独立 `review_skills` 对象；首次选择记录完整结果，后续 iteration 记录同一 selection id：

```json
{
  "review_skills": {
    "retrieval_version": "deterministic-v1",
    "mode": "deterministic",
    "bank_digest": "...",
    "selection_id": "...",
    "total_records": 120,
    "active_records": 84,
    "scoped_active_records": 72,
    "unscoped_active_records": 12,
    "candidate_count": 9,
    "loaded_skill_ids": ["skill-017", "skill-044"],
    "loaded": [{"id": "skill-017", "score": 160, "reasons": ["language:python", "trigger:asyncio"]}],
    "skipped": [{"id": "skill-002", "reason": "language_mismatch"}],
    "skipped_count_by_reason": {"status": 36, "language_mismatch": 50, "budget": 1},
    "top_k": 5,
    "char_budget": 4000,
    "core_chars": 443,
    "learned_chars": 286,
    "total_chars": 762,
    "estimated_tokens": 181,
    "retrieval_latency_ms": 1.4
  }
}
```

若 Skill Bank 很大，完整 skipped list 可写事件日志但在聚合报告中只保留 counts 和 bounded IDs。不要记录原始 diff corpus。`ReviewProcessMetrics` 与 `MetricSummary` 增加 loaded count、skill chars/tokens、retrieval latency；ID/score 留在 per-run artifact，不做无意义的跨 suite 平均。

## 11. Evaluation plan

### 11.1 Unit tests

在现有 `tests/test_review_skills.py` 扩展：

- inactive/candidate never retrieved；
- deprecated never retrieved；
- language match ranking；
- path match ranking；
- trigger match ranking；
- irrelevant language/path skill filtered；
- hard budget respected；
- oversized ranked skill 不阻塞后续可装 Skill；
- Top-K respected；
- deterministic ordering 与 JSONL file order independence；
- malformed optional metadata fallback；
- existing old JSONL compatibility；
- Core always fully included；
- selection pinned/bank digest stable；
- graph signals absent时安全降级。

在 `tests/test_review_experience.py` 保留 candidate/activate/deprecate、feedback consumption、proposal citation guard，并新增 metadata round-trip/status update preservation。`tests/test_prompts.py` 验证生产 Prompt 使用 selection，而非无条件 active list。`tests/test_review_loop_observability.py` 或新测试验证遥测。

### 11.2 Retrieval accuracy eval

给 Golden PR fixture 增加可选 `expected_skill_ids`（默认空，旧 fixture 兼容），选一批有代表性的真实 PR 人工标注 relevant/irrelevant skills。模型调用前即可评估：

- Recall@K；
- Precision@K；
- irrelevant skill rate；
- no-relevant-skill 时的 empty/legacy fallback 行为；
- budget loss rate（相关 Skill 因预算未载入）。

不要用“Skill 是否被模型引用”代替 retrieval relevance ground truth。

### 11.3 Review quality A/B

复用现有 Golden PR harness，不新建评测框架。`EvalVariant` 当前只有 context mode 与 graph cache mode，需要新增 `skill_retrieval_mode = sequential | deterministic` 和固定 Skill Bank fixture/snapshot。

```text
baseline:  相同模型/温度/context mode/graph cache + sequential active loading
candidate: 相同所有条件 + deterministic retrieval
```

比较：finding hit/recall、false positives、schema/workflow validity、prompt/total tokens、skill tokens、tool calls、reviewer/end-to-end latency。先保证 quality non-regression，再看 context cost。Skill Bank 必须在两组间固定 hash；建议多样本而不是只跑一次。

### 11.4 已验证回归基线

调查期间执行：

```text
tests/test_review_skills.py
tests/test_review_experience.py
tests/test_github_feedback.py
tests/test_prompts.py
tests/test_review_workflow.py
```

结果：48 passed。

## 12. Incremental implementation plan

### PR 1 — Pure retrieval core（不切生产默认）

- 扩展可选 metadata 与 backward-compatible parsing；
- 新增 `SkillQuery / SkillMatch / SkillSelection`；
- 实现 deterministic filter/rank/Top-K/atomic budget；
- Core full-inclusion invariant；
- 保留无参 `render()` legacy path；
- 完整 unit/lifecycle compatibility tests。

### PR 2 — Runtime wiring + telemetry + feature flag

- 在首次 analyze 使用 resolved diff + optional manifests 建 query；
- run-scoped pin selection 与 bank digest；
- Prompt 改为显式接收 selection；
- Settings 增加 mode/top-k/char-budget；
- 事件日志与 run/eval process metrics；
- 默认先 `sequential`，允许环境/variant 开启 `deterministic`。

### PR 3 — Retrieval labels + Golden A/B + default gate

- fixture 可选 `expected_skill_ids`；
- retrieval accuracy report；
- EvalVariant 增加 skill mode 与 bank fixture/hash；
- 跑 sequential vs deterministic 的 real Golden PR A/B；
- 达到 recall/FP/cost gate 后才把生产默认切到 deterministic。

### Future（不进入 MVP）

- 标注证明简单 lexical ranking 不足后加入 BM25；
- 有足够曝光与反馈数据后加入小权重、经 propensity/exposure 校正的 utility；
- 只有证明 rare-tail Skill 的 harness retrieval 召回不足且额外 tool round 值得时，才实验 optional `load_skill`；
- Embedding/vector store 保持最后选项。

## 13. Exact files likely to change

### PR 1

- `src/analyzer/review_skills.py`：metadata、query、score、selection、packing、digest；
- `src/analyzer/review_lifecycle.py`：proposal metadata validation、status update 保留字段；
- `tests/test_review_skills.py`：retrieval/budget/determinism/compat；
- `tests/test_review_experience.py`：metadata proposal 与 lifecycle round-trip；
- 可选 `src/analyzer/language_detection.py`：若把 extension mapping 从多个模块统一出来；否则首 PR 内部纯函数即可。

### PR 2

- `src/orchestrator/agent_loop.py`：构建并 pin run-scoped selection、记录完成 telemetry；
- `src/analyzer/inference_engine.py`：传递 selection 与 per-call telemetry；
- `src/analyzer/prompts.py`：显式 skill context 注入，移除生产路径隐式 loader I/O；
- `src/config.py`、`.env.example`：retrieval mode、Top-K、char budget；
- `src/analyzer/event_log.py`：只有决定新增专用 event type 时才改；复用 `CONTEXT_TELEMETRY` 则无需改；
- `src/analyzer/run_summary.py`、`eval/run_summary.py`：聚合新指标；
- `tests/test_prompts.py`、`tests/test_agent_loop.py`、`tests/test_review_loop_observability.py`、`tests/test_run_summary.py`。

### PR 3

- `eval/schemas.py`：skill variant/retrieval metrics/fixture annotation；
- `eval/runner.py`：固定 Skill Bank 并注入 variant；
- `eval/run.py`、`eval/report.py`、`eval/compare.py`、必要的 gate tests；
- 一小组 `eval/fixtures/*.json`：人工标注 expected skill ids；
- 对应 `tests/test_eval_runner.py`、`tests/test_eval_process_metrics.py`、`tests/test_eval_gate.py`。

当前不需要改 `context_planner.py`、`code_graph.py` 或 persistent SQLite schema。Graph 只是 query signal provider，Skill lifecycle 仍留在 JSONL。

## 14. Risks / open questions

1. **Metadata authoring quality**：模型可提出 metadata，但 activation 前必须由人确认；错误的窄 scope 会造成 false negative。
2. **Filter recall**：首版只把明确 language/path mismatch 作为 hard filter，trigger 多用于 ranking；等标注证明安全后再收紧。
3. **Language mapping fragmentation**：Graph 只支持 py/rs/cs，而 ContextBuilder 已知道 js/ts/tsx 邻居。应有一个 lightweight shared mapper，不能误称 Graph 已覆盖 TypeScript。
4. **CLI diff timing**：当前本地 `--diff` 在 Graph prepare 后才读取。Retrieval 可在 analyze 覆盖，但若未来要统一 Graph+Skill signals，应单独把 effective diff resolution 前移并做回归测试。
5. **Core 与 hard budget 冲突**：当前会截断 Core。推荐 CI 保证完整 Core fit；运行时发现超限应 telemetry + 禁止 learned skills，不应静默切断 invariant 文本。
6. **Legacy fallback pollution**：兼容策略只能用于迁移期。持续存在大量 unscoped active skill 会重新产生污染，应以 telemetry 驱动回填并设置收紧门槛。
7. **Utility attribution**：当前没有 usage_count、hit_count、last_used 或 skill-level outcome。未来不能按 raw usage 排序；需记录 eligibility/exposure，并用 capped、Bayesian-smoothed、低权重 outcome signal，保留 exploration，避免 rich-get-richer。
8. **Skill duplication/conflict**：Bank 增长后需要离线 lint/合并/冲突审查，但不要把它塞进 runtime retrieval PR。
9. **Prompt budget口径**：Skill 的 char budget 与整体 payload token budget分离。首版保留该边界并测 estimated tokens；是否把 System Prompt 纳入统一 global budget 是另一个更大的问题。
10. **Repository-specific skills**：当前 analyzer request 丢弃 repo identity。只有先定义 bank ownership（global、tenant、repo）后，repository-specific routing 才值得实现。

## Final recommendation

MergeWarden 现在需要的不是 Skill RAG，而是一个小型、确定性、可审计的 procedural-memory selector。先让 `active` 从“永久 Prompt 内容”变成“经过批准、可参与检索的经验”，再用真实 Golden PR 标签判断 deterministic metadata 是否足够。只有测到明确的 lexical miss，才升级 BM25；只有测到 harness 无法覆盖的长尾价值，才讨论动态 tool；当前没有任何证据支持 embedding/vector DB。
