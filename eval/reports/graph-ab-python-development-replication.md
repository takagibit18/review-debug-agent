# Graph A/B Python development replication（30 measured attempts）

Run date: 2026-08-16
Variant: `A-agent-search` vs `B2-graph-hybrid-warm`
Policy: paired 3-sample matrix, no retry, warm priming excluded from measured attempts,
runtime invalid preserved as-is. 这是 exploratory development run，不是 formal benchmark。

## 1. Experiment scope

- 30 measured attempts：5 fixtures × 2 variants × 3 samples。
- 真实 Python Graph-sensitive 正样本（本轮核心）：`haystack_pr12257_reverse`（direct cross-file，gold=filters.py:105）、`haystack_pr12162_reverse`（multi-hop，gold=breakpoint.py:242）。
- synthetic graph-sensitive：`development_agent_search_cross_file`（复现 dev-28 的 +1 HIT signal）。
- Controls：`pytest9350`（local positive）、`requests_netrc`（negative）。
- Budgets intentionally remain relaxed to isolate quality behavior before cost optimization（沿用 dev-28 contract：deepseek-v4-flash / temp 0 / max_output 4096 / prompt 12000 / token 60000 soft + 80000 hard / final submit reserve 12000+4000+1200 / tool 64 / timeout 180+30+600 / Verifier & workflow enforcement=enforce）。
- **Review iterations：requested 3 / effective 2**。主干 `EVAL_REVIEW_MAX_ITERATIONS_CAP=2` 未改，observed 1–2。
- Reviewer / Graph Builder / Context Planner / Verifier / deterministic gate / Matcher / Golden / fixture / runtime workflow 均未改动。仅对 eval harness 做了 2 处最小改动以支持本轮 2-variant 矩阵（见 §10）。

## 2. Runtime completion

| item | value |
|---|---:|
| planned measured attempts | 30 |
| completed / recorded | 30 / 30 |
| valid | 23 |
| invalid（runtime workflow） | 7 |
| Warm priming（不计 measured） | 15 |
| Warm measured cache hit | 9/9 valid Warm 全部 `cache_hit=true` |
| Graph fallback | 0 |
| pairing / matrix errors | 0 |
| resume：reused / newly attempted | 18 / 12 |
| 历史 invalid 被重跑 | 0 |
| duplicate stable-key attempt | 0 |
| EventLog / Run Journal | 30 / 30 持久化 |

7 invalid = haystack-12257 Warm ×3 + haystack-12162 Warm ×3 + requests Agent s3 ×1。
前 6 个是真实观察到的 Graph Warm budget 失效（§4/§7）；requests Agent s3 为单个 workflow invalid（submit 后 schema/placeholder，非 Graph 相关）。

## 3. Fixture matrix

| Fixture | Type | Agent Search | Graph Warm |
|---|---|---:|---:|
| dev-xfile | synthetic graph-sensitive | 2/3 HIT | 2/3 HIT |
| Haystack 12257 | real direct-cross-file | 0/3 HIT（3 valid MISS） | 0/3 HIT（3 invalid） |
| Haystack 12162 | real multi-hop | 0/3 HIT（3 valid MISS） | 0/3 HIT（3 invalid） |
| pytest9350 | local positive control | 3/3 HIT | 2/3 HIT |
| requests netrc | negative control | 2 clean + 1 invalid | 2 clean + 1 FP |

## 4. Real Graph-sensitive result（headline）

只汇总 #12257 + #12162：

- Agent Search：**0/6 HIT**（6 valid MISS）
- Graph Warm：**0/6 HIT**（0 valid；6 个全部 runtime invalid）

Graph 在真实 Python cross-file / multi-hop 上没有产生任何额外 Gold HIT —— 但 Warm 侧不是以 quality MISS 形式失败，而是**以 workflow invalid 形式失效**：

- 每个 Warm run：priming 正常（cold build 39–53s，graph ready，~10k nodes / 111–461 paths，无 fallback）→ measured `cache_hit=true` → 但首轮 prompt 的 graph manifest context 高达 **candidate_context_tokens=55,278（#12257）/ 31,954（#12162）** → 首轮即 `budget_hard_capped`（soft 60k 被打穿）→ 无 submit → placeholder → invalid。
- 因此：**该 treatment 在这两个 fixture 上无法形成合法 review 终态**，真实 Graph 是否改善 recall 在有效样本层面无法被回答。这不是 retrieval 层面的结论，而是 budget/workflow 层面的失效。

## 5. All Graph-sensitive result

加 synthetic（dev-xfile）：

- Agent：2/9 HIT（dev-xfile 2/3 + haystack 0/6）
- Warm：2/9 HIT（dev-xfile 2/3；haystack 0 valid）

dev-28 观察到的 synthetic +1 signal（Warm 3/3 vs Agent 2/3）**本轮未复现**：两边都是 2/3，且各含 1 次 wrong-finding MISS（matched=0, FP=1）。

## 6. Controls

- pytest9350（local positive）：Agent **3/3 HIT**，Warm **2/3 HIT**（Warm s2 有效 MISS，submit 空 finding）。Graph 对 local positive 有 1/3 recall 波动（与 dev-28 的 2/3 vs 3/3 一致，方向未变）。
- requests（negative）：Agent 2 clean + 1 invalid（无 FP）；Warm 2 clean + **1 FP（s1）**——Warm 在 negative 上引入了 1 个 unmatched finding。本轮没有观察到 Graph 降低 FP。

## 7. Cost observation（development observation，非 production 结论）

| 子集 | n | mean/median E2E | mean tokens | mean tools | mean read / grep |
|---|---:|---:|---:|---:|---:|
| Agent（全部 valid） | 14 | 88.3 / 89.1s | 41,607 | 4.50 | 3.21 / 0.79 |
| Warm（全部 valid） | 9 | 82.2 / 87.7s | 27,125 | 2.78 | 2.11 / 0.00 |
| **Agent（REAL graph-sensitive，valid）** | 6 | 111.4 / 110.3s | **60,241** | 5.33 | 4.17 / 1.17 |
| **Warm（REAL graph-sensitive）** | 0 valid | — | 44.8k（invalid runs） | 3–7（invalid runs） | — |

注意：全 valid 的 Warm 均值显著低于 Agent，**是因为 6 个高成本 haystack Warm run 全部 invalid 被排除**，而不是 Graph 更便宜。REAL 子集里 Agent 已是 60k tokens 量级；Warm 若成功，首轮 prompt 即含 ~32–55k manifest tokens，只会更高。15 次 Warm priming（cache build）总 321.7s：dev-xfile 1.2s / pytest 14.6s / requests 2.8s / haystack 12162 42.0s / haystack 12257 46.6s（mean）。

## 8. Search behavior（两个 real Graph-sensitive case）

- Graph 没有减少 Agent exploration：6 个 Warm haystack run 在 cap 前仍做了 3–6 次 read/grep 工具调用（`read_file_calls` 3–6，`grep_calls` 0–4），与 Agent（read 3–5 / grep 0–2）量级相当——**Graph context 与 Agent Search 同时叠加**，而非替代。
- 没有任何 run（Agent 或 Warm）"更早触达 Gold-related symbol"：Agent 全部 valid 但 0 HIT；Warm 从未形成 submit。
- 重复读取 manifest 已提供的 source 无法从现有 telemetry 直接量化，但 Warm run 的 read 次数并未低于 Agent。
- 结论：本轮数据**不支持**"Graph 减少探索/更早触达 gold"的假设；反而显示 Graph context 以 ~30–55k token 的代价叠加在探索之上，直接挤爆预算。

## 9. MISS funnel（只做已有数据归类）

- **Retrieval/discovery + evidence-contract attrition（#12257 Agent，3/3）**：Reviewer 提交了 2 个 findings（raw verifier `accepted` 2/2），随后 evidence-bound verifier 拒 2/2、deterministic gate 拒 2/2（event log 明确出现 `evidence_context_missing` / `deterministic_evidence_invalid`）→ 0 final。这是**第 11 节预警的 post-discovery evidence-contract attrition**：通过工具获得的合法 evidence 未被 deterministic gate 承认。Reviewer 实际已发现有效问题，但最终 0 输出。
- **Matcher / wrong-finding（#12162 Agent，3/3）**：提交了 findings（final 2/1/1）但全部 unmatched（FP 2/1/1），gold 未被命中。
- **Workflow invalid / budget（haystack Warm 6/6）**：manifest 过大 → hard cap → 无 submit。
- **Workflow invalid（requests Agent s3）**：submit 后 schema/placeholder。
- **pytest Warm s2**：submit 空 finding（0 输出）。
- 没有单独环节能解释全部 MISS；真实 case 上 Agent 的主要 blocker 是 evidence gate 与 wrong-finding，Warm 的主要 blocker 是 budget。

## 10. 报告必须披露的 harness 变更

本轮 config 只声明 A + B2 两个 variant（不跑 Cold）。现有 `graph_ab_pilot.py` 的 `_variants` 要求恰好 3 个 variant、且 `variant_order` 输出按 3-variant 集合过滤，直接运行会 KeyError。为执行用户指定的 2-variant 矩阵，做了 2 处最小 harness 改动（均不影响 review/graph/verifier/matcher 语义，dev-28/supplement config 行为不变）：

1. `_variants`：`runtime_contract_source: current` 的 development config 允许声明 variant 子集（frozen phase-two config 仍强制 3 variant）。
2. `run_pilot` 的 sample order 过滤增加 `variant_id in sample_counts` 判断。

## 11. 结论

Q1：真实 Python Graph-sensitive case 上，Graph Warm 是否提高 Gold Recall？
→ **无法以有效样本回答，且没有任何正向证据**。Warm 6/6 全部 workflow invalid（manifest 32–55k tokens 首轮打穿 80k hard cap → 无 submit），Agent 6/6 valid 但 0 HIT。

Q2：Graph 是否降低 FP / wrong findings？
→ **无证据**。valid 集合上 Warm FP=2（dev-xfile 1 + requests negative 1）、Agent FP=6，但 valid 集合构成不同（Warm 缺全部 haystack），不可比；同 fixture 内（dev-xfile）两边各 1 FP，requests negative 上 Warm 反而新增 1 FP。

Q3：Graph 是否真的减少 Agent exploration？
→ **没有**。Warm 在 cap 前仍做与 Agent 同量级的 read/grep；且 Graph context（32–55k token）叠加在探索之上直接耗尽预算。本轮表现正是"Graph context + Agent Search 同时叠加"，且叠加成本在这个 contract 下是致命的。

整体判定：本轮数据更接近用户预设的 case D/C 混合——Graph 在真实 Python case 上没有证明 recall 或 precision 边际价值，反而因 manifest 体积在统一 budget 下使 Warm 无法完成 review；在 local/negative control 上也没有系统性改善（pytest 2/3 vs 3/3，requests 新增 1 FP）。

**下一步（只允许一个最高优先级动作）**：先修 budget/workflow 交互——给 Graph Warm 首轮 prompt 的 graph context 设置硬性 token 上限（例如 manifest 注入预算 ≤ final_submit reserve 之前的可用 headroom），并让 planner 在超限时以「相关 graph path + 关键 relation 摘要」降级注入，而不是整份 manifest 进 prompt。这是让"Graph 是否改善真实 recall"这一问题在有效样本上可回答的前置条件。其余观察（evidence-contract attrition、negative FP）列为后续独立排查项，不在同一改动里处理。
