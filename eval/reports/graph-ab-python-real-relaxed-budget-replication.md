# Corrected replication：真实 Python Graph-sensitive × 宽松 runtime contract

Experiment: `graph-ab-python-real-relaxed-budget-replication`
Run date: 2026-08-16
Scope: 2 fixtures（#12257 direct-cross-file、#12162 multi-hop）× 2 variants × 3 samples = 12 measured attempts
Purpose: 修复 runtime contract propagation bug 后，在真正宽松 budget 下重测 Graph Warm 能否从"retrieval 已接近 Gold"转化为有效 review completion / HIT。

## 1. Runtime contract verification（先决条件）

| 项 | Declared | Observed | Matched |
|---|---|---|---|
| token soft budget | 60000 | 60000（budget controller `_token_budget`） | ✅ |
| token hard budget | 80000 | 80000（budget controller `_token_hard_budget`） | ✅ |
| prompt input budget | 12000 | **12000**（真实 run context_telemetry，修复前为 32000） | ✅ |
| max output tokens | 4096 | 4096（ModelClient settings） | ✅ |
| request timeout | 180s | 180s（ModelClient settings） | ✅ |
| run timeout | 600s | 600s | ✅ |
| requested / effective iterations | 3 / 2 | 3 / 2 | ✅ |

**Contract matched: YES**（上一轮默认 contract 下 token 为 30000/36000、prompt 32000、timeout 90s，已确认全部失效；本轮 12 个 run 均读到修正后的值，且 run 总 token 达 55k–85k——在旧 36k hard 下不可能发生，进一步印证放宽 budget 真实生效）。

**Root cause 结论**：`src/config.py::get_settings()` 每次调用新建 Settings 实例（非单例），`_apply_runtime_contract` 修改的是临时实例后被丢弃；AgentOrchestrator / GraphHybridContextStrategy / InferenceEngine / ModelClient 都各自重新 `get_settings()` → 拿到 env 默认值。

**最小 harness fix**：`_apply_runtime_contract` 改为把 contract 值写入 `os.environ`（`TOKEN_BUDGET`/`TOKEN_HARD_BUDGET`/`PROMPT_INPUT_TOKEN_BUDGET`/`MODEL_MAX_TOKENS`/`MODEL_REQUEST_TIMEOUT_SECONDS`/`AGENT_RUN_TIMEOUT_SECONDS`/`FINAL_SUBMIT_*`/`AGENT_MAX_TOOL_CALLS`/`AGENT_TOOL_TIMEOUT_SECONDS`/`REVIEW_MAX_ITERATIONS`/`MODEL_NAME`），使每次新建 Settings 都读到 contract。仅改 `eval/graph_ab_pilot.py`（+`import os`），production 未动。

**Regression test**：`tests/test_graph_ab_runtime_contract.py`（3 tests），锁定"config 写 60k/80k 但实际跑 30k/36k"不再复发。相关套件 36 passed。

## 2. 结果

| fixture | Agent Search | Graph Warm |
|---|---|---|
| #12257 direct-cross-file | 0/3 HIT（3 valid MISS，0 FP） | 0/3 HIT（3 valid MISS，0 FP） |
| #12162 multi-hop | 0/3 HIT（3 valid MISS，1 FP） | 0/3 HIT（3 valid MISS，**4 FP**） |
| **Real graph-sensitive aggregate** | **0/6 HIT** | **0/6 HIT** |

补充指标（real 子集）：

| 指标 | Agent | Warm |
|---|---|---|
| valid / invalid | 6 / 0 | 6 / 0 |
| hard-cap count | 1 | 2 |
| submit count | 6 | 6 |
| mean/median tokens | 59.8k / — | 70.0k / — |
| mean/median latency | 116s / 115s | 132s / 122s |
| mean tools | 5.3 | 4.8 |
| candidate_context_tokens | — | 55,278（#12257）/ 31,954（#12162） |
| Graph cache hit | — | 6/6 true |
| Graph fallback | — | 0 |

上一轮对照（默认 36k budget）：Warm 6/6 **invalid**（manifest 首轮打穿预算→无 submit）。本轮 Warm 6/6 **valid**，全部完成 2 轮迭代 + 正常 submit。

## 3. 关键观察（分层失败，逐步定位）

1. **Contract 是第一层 blocker（已修）**：80k 下 Warm 从 invalid 全部转为 valid，完成完整 review workflow（2 iterations、3–7 次工具调用、submit）。这证明上一轮 Warm 的 0/6 invalid 不是 Graph 无价值，而是错误 contract 的产物。

2. **第二层 = finding filter 的 confidence 门 + reverse-fixture 对冲**：以 #12257 Warm s1 为例——Graph 已把 Reviewer 引导到 **gold 精确位置 `filters.py:105`**，且 evidence 描述正确（"`_dates_are_equal` 的 mixed-awareness 检查现在无条件执行，naive vs aware 永远不匹配"）。但提交的 finding **缺失 `confidence` 字段 → 解析为 0.0**，被 policy filter 以 `warning_confidence_below_standard_threshold`（阈值 0.85/0.7）拒绝；同时 suggestion 是"if intentional, document it"式的对冲表述。→ 3 submitted → 3 policy rejected → 0 final → MISS。

3. **Agent 仍是 discovery/reasoning miss**：#12257 Agent 3 次都提交 metadata_router.py:59 + document_store.py:75（wrong-file），未触达 filters.py；#12162 Agent 提交了 pipeline/breakpoint 附近的 finding 但根因错位（s1 曾命中 breakpoint.py:242 但描述成"旧快照兼容回归"）。

4. **FP 差异**：#12162 Warm 的 FP（4）高于 Agent（1）——Warm 提交了更多（unmatched）finding，说明 Graph 提供的更多候选被当成了 risk 提交，但没有一个命中 gold 的根因表述。

## 4. 结论（对应 case B）

Warm 变 valid、但仍 MISS——**budget 只是第一层 blocker**。修好 contract 后暴露的第二层是：**模型在 reverse-fixture 上产出正确位置、正确 evidence 的 finding，但 (a) 不写 confidence（→0.0 被 policy filter 杀），(b) 用"可能是刻意变更"的对冲表述而非确定 bug**。这既不是 Graph 检索失败（检索已到 gold），也不是 verifier 拒绝（根本没进 verifier，卡在 policy filter 的 confidence 门）。

因此"Graph 是否改善真实 recall"仍无法从 HIT 上回答，但方向明确：Graph 的检索贡献已在真实样本上被证明（Warm 触达 gold 位置而 Agent 没有）；剩余 gap 集中在 **finding 的 confidence 表达 + reverse-fixture 的确定性问题**，这属于 Reviewer/verifier 上游的表述层，不在本轮 scope。

## 5. 未做（按指令冻结）

未修改：Graph Builder / Context Planner / manifest / Reviewer / Prompt / runtime contract / Verifier / deterministic gate / Matcher / Golden / fixture / workspace snapshot / Graph cache lifecycle。本轮仅 2 处 harness 改动（contract propagation + teardown deferral），均在 review workflow 之外。

## 6. teardown 优化披露

前 4 条 attempts 用原 synchronous `TemporaryDirectory` teardown；诊断发现 Windows rmtree 删除完整 Haystack worktree（~10k files / 110MB）产生 22–32min 非测量后处理开销。之后仅改 development harness：`--defer-workspace-cleanup` 使每个 attempt 的 workspace 保留到 `eval/outputs/<experiment>/deferred_workspaces/<hash>/`，实验结束后统一清理。该变化发生在 model/review workflow 已结束之后，不改变 input snapshot / variant / contract / Graph lifecycle / Reviewer / Verifier / Matcher / Gold，不属于 A/B treatment。

## 7. 产物

- raw / summary / checkpoint：`eval/outputs/graph-ab-python-real-relaxed-budget-replication/`
- EventLog：`eval/outputs/event_logs/`
- run journals：`eval/outputs/graph-ab-python-real-relaxed-budget-replication/run_journals/`
- deferred workspaces：`eval/outputs/graph-ab-python-real-relaxed-budget-replication/deferred_workspaces/`（见 cleanup manifest）
