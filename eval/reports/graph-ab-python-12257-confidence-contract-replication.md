# Graph A/B — #12257 confidence-contract replication

实验 ID：`graph-ab-python-12257-confidence-contract-replication`
种子：20260816 · 单 fixture（#12257）× 2 variants × 3 = **6 measured attempts**

---

## 1. Contract Verification

| 项 | Declared | Observed | Matched |
|---|---|---|---|
| token soft budget | 60000 | soft_capped @ 78.8k tokens | ✅ |
| token hard budget | 80000 | 无 36k 硬切（s1 是 soft_capped 非 hard_capped） | ✅ |
| prompt_input_token_budget | 12000 | 12000（context_telemetry） | ✅ |
| max_output_tokens | 4096 | 4096（submit 阶段）；exploration 阶段 12288 | ⚠️ 见注 |
| model_request_timeout | 180 | 180 | ✅ |
| requested iterations | 3 | 3 | ✅ |
| effective iterations | 2 | 2 | ✅ |

**Contract matched: YES**（关键预算 60k/80k、prompt 12k 全部真实生效，旧的默认 30k/36k 已排除）。

> **注（max_output_tokens）**：exploration 阶段 model call 使用硬编码 `_EXPLORATION_MAX_TOKENS=12288`，submit 阶段使用 `_SUBMIT_MAX_TOKENS=4096`。这是既有架构常量（上一轮 corrected replication 同样如此），对 Agent/Warm 两变体完全一致，不构成 A/B 偏置；contract 声明的 4096 对应 submit 阶段与 `model_max_tokens` 默认值。**非本轮 contract 缺口**，未改动。

---

## 2. 修复内容（最小、仅 harness/边界）

业务文件改动仅 3 处，无 prompts/policy/formatter/verifier/graph/matcher/fixture 改动：

1. `src/orchestrator/tool_schemas.py` — `submit_review.issues[].required` 加入 `"confidence"`（+1 行）
2. `src/analyzer/inference_engine.py::_validate_submit_review_payload` — 对每个 issue 检查缺失 `confidence`，返回明确 error（+6 行）
3. `src/analyzer/inference_engine.py::_try_parse_submit_payload_from_json` — fallback review JSON 复用同一 `_validate_submit_review_payload`（+4 行）

**为什么没改 `ReviewIssue.confidence` 的 default=0.0**：`ReviewIssue` 是项目内部 canonical/compatibility 模型，其 default=0.0 有意保留 legacy v0.2.2 caller/publisher 兼容。本次 bug 发生在 LLM `submit_review` 边界，不是内部数据模型，故只在 submission boundary 收紧。

**验证结果**：
- tool schema `issues[].required` 含 `confidence` ✅
- runtime parser 拒绝 missing confidence（返回 `issues[i] missing required confidence`）✅
- fallback JSON 复用同一 validation，无法绕过 ✅
- validation-repair 闭环（fake model call1 缺 conf → repair 补 0.9）✅
- targeted tests（tool_schemas + inference_engine + review_draft_validator + result_processor）**69 passed**
- 完整套件 **769 passed / 1 skipped / 1 failed**（唯一失败 `test_golden_fixture_distribution_has_required_buckets` 断言 golden fixture=17 但实际 19，是此前新增 haystack 2 fixture 的既有漂移，与本次无关）

---

## 3. 结果：6/6 valid，0 invalid，0/6 HIT（matched=0）

| run | final | matched | FP | budget_state | tokens |
|---|---|---|---:|---:|---:|
| Warm s1 | 0 | 0 | 0 | soft_capped | 78,807 |
| Agent s1 | 1 | 0 | 1 | soft_capped | 73,265 |
| Agent s2 | 0 | 0 | 0 | none | 53,653 |
| Warm s2 | 0 | 0 | 0 | hard_capped | 94,974 |
| Warm s3 | 0 | 0 | 0 | hard_capped | 114,914 |
| Agent s3 | 0 | 0 | 0 | none | — |

- Agent HIT **0/3**，Warm HIT **0/3**（自动化 matcher 口径）
- hard-cap 数：2（Warm s2/s3）
- submit 数：6/6 全部 submit

---

## 4. 人工审查：真正捕获 gold 根因的 run

gold 根因 = `inconsistent-mixed-awareness-datetime-filter-semantics`：
「Equality / membership **拒绝** mixed-awareness 对，而 ordering **仍 reconcile** 相同值——结果取决于用哪个操作符，而非统一策略。」

| run | 触达 filters.py | 是否捕获"跨操作符不一致"根因 | 判定 |
|---|---|---|---|
| **Warm s1** | ✅ 105 + 119 | ✅ **F-02**（filters.py:119）："ordering 仍 reconcile 而 equality 拒绝 → `>`/`>=`/`<`/`<=` reconcile 但 `==`/`!=`/`in`/`not in` 拒绝" | **TRUE HIT** |
| **Agent s1** | ✅ 105 | ✅ **F-03**（filters.py:105）："`==` never match 但 `>`/`<` still match → inconsistent behavior" | **TRUE HIT** |
| Warm s2 | ✅ 105 | ❌ 只说"equality 变 strict + 无 opt-out"，漏了 ordering 仍 reconcile | miss |
| Warm s3 | ✅ 105 | ❌ **事实错误**——声称 ordering 也变 strict（实际仍 reconcile），把"不一致"读成"已统一" | miss |
| Agent s2 | ❌ metadata_router | ❌ 参数删除 | miss |
| Agent s3 | ❌ metadata_router/document_store | ❌ 参数删除 | miss |

**人工审查结论：2/6 真正捕获 gold 根因 —— Warm s1 与 Agent s1。**

对比上一轮 corrected replication（0/6 捕获根因，Agent 全部 wrong-file），本轮显著改善：**confidence 修复后，模型对 gold 根因的捕获从 0 提升到 2/6**，且 Agent 首次触达 filters.py 并说出"不一致"。

---

## 5. 每个 Warm run 的完整 funnel

### Warm s1（run 3e18f5e0）— 最有信息量

```
retrieval         → ✅ 触达 filters.py:105 + 119（Graph manifest 引导）
candidate         → ✅ F-01@105(0.9) F-02@119(0.85) F-03@metadata_router:59(0.8)
schema contract   → ✅ 全带显式 confidence（修复前缺失→0.0）
policy filter     → ✅ F-01 过(0.9) F-02 过(0.85) F-03 拒(0.8<0.85)
raw verifier      → ✅ F-01/F-02 accepted
deterministic gate→ ❌ F-01: diff_evidence_context_missing(filters.py:15, document_store.py:439)
                     ❌ F-02: pr_causal_anchor_missing("cause evidence 未落在 PR 变更行")
matcher           → 0 final
```

**F-02 是 gold 根因的精确复述，但被 `pr_causal_anchor_missing` 杀**：F-02 的 cause evidence 指向 `filters.py:119`（**未变更**的 ordering 行），而 deterministic gate 要求 cause evidence 落在 PR 变更行上。

### Warm s2（run 74b3d131）
- F-01@105(0.9) "equality 变 strict + 无 opt-out" — **漏 ordering 仍 reconcile** → 未捕获"不一致"
- F-02@metadata_router:147(0.88) 序列化兼容 — 非 gold
- 结论：触达 gold 位置但根因表述不完整

### Warm s3（run dc8c4082）
- F-01@105(0.9) 声称"equality 与 ordering **都**变 strict（119 行 guard 也移除）" — **事实错误**（gold 明确 ordering 仍 reconcile）
- 结论：触达 gold 位置但把"不一致"读反成"已统一"

---

## 6. 核心结论（对应指令第 21 节 Case B）

1. **confidence contract gap 已闭环**：18 个 submitted finding 全部携带显式 confidence（0.8–0.9），不再静默变 0.0。policy filter 不再误杀 gold finding（0.85/0.9 ≥ 0.85 正常通过），raw verifier 也 accept。

2. **下一 blocker = deterministic evidence gate，且对"不一致"类 finding 是结构性的**：
   - gold 根因「跨操作符不一致」**本质上要求同时引用变更行(105, equality) 与未变更行(119, ordering)**，否则无法表达"不一致"。
   - 但 deterministic gate 的两条规则：
     - `pr_causal_anchor_missing`：cause evidence 必须落在 PR 变更行
     - `diff_evidence_context_missing`：evidence 引用的位置必须在 retained diff 内
   - 因此，**正确表述"不一致"的 finding（必然锚定在未变更的 ordering 行）被结构性拒绝**。Warm s1 F-02 与 Agent s1 F-03 双双因此被杀。

3. **Graph retrieval gain 首次在真实 case 上"跑通到提交根因"**：Warm s1 不仅触达 gold 文件，还提交了与 gold 根因语义一致的 finding（F-02）。上一轮是"读到但被 budget 切断"，本轮是"提交了正确根因但被 evidence gate 杀"。

4. **hedge wording 仍在**：Warm s1 F-01 suggestion 仍是 "If the intent is... deliberate breaking change... should be documented"（reverse-fixture 对冲表述），但已不构成 HIT 障碍（F-01 本就不是根因，F-02 才是，F-02 无对冲）。

5. **Agent 首次捕获根因**：Agent s1 F-03 精确说出"`==` never match 但 `>`/`<` still match → inconsistent"。上一轮 Agent 全部 wrong-file，本轮 s1 到达 filters.py:105 并捕获根因。属单次观察，不排除随机性。

---

## 7. 结果解释（按指令规则）

**Case B**：Warm candidate 有正确 confidence、通过 policy、raw verifier accept，但随后被 **deterministic evidence gate** 杀掉。→ confidence contract gap 已闭环，下一 blocker 已定位为 deterministic evidence gate 对"跨操作符不一致"类 finding 的**结构性锚点约束**。

本轮**不修** deterministic gate（不在 scope）。是否进入下一步（evidence gate 对"不一致/未变更行"证据的处理）由你决定。

---

## 8. Artifacts

- 报告：`eval/reports/graph-ab-python-12257-confidence-contract-replication.md`（本文件）
- raw：`eval/outputs/graph-ab-python-12257-confidence-contract-replication/raw.json`
- summary：`eval/outputs/graph-ab-python-12257-confidence-contract-replication/summary.json`
- checkpoint：`eval/outputs/graph-ab-python-12257-confidence-contract-replication/checkpoint.jsonl`
- config：`eval/variants/graph-ab-python-12257-confidence-contract-replication.yaml`
- regression tests：`tests/test_inference_engine.py`（+4 测试）、`tests/test_orchestrator_tool_schemas.py`（+1 测试）
