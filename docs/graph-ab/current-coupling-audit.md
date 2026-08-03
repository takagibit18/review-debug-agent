# Graph A/B 第一阶段耦合审计

## 审计范围与结论

审计基线为 `main` 的 `eac439c2f3ddf88b8874b9dcbfcdc156bb4d7da0`。审计确认：原实现具备完整的关系图、Change-Centered Context Planning、Finding Verifier 和 Root-Cause Consolidator，但 Graph 同时侵入 Orchestrator、Reviewer Prompt、结构化证据校验和 Eval 运行入口，直接关闭 `RELATION_GRAPH_ENABLED` 并不能形成公平、可冻结的 Agent 基线。

主要不公平点有两个：

1. Orchestrator 在统一审查主流程内直接构建或复用 `RelationGraphIndex`，随后直接生成 Candidate Context Manifest；Context Strategy 不是独立边界。
2. Reviewer Prompt 和 schema-v2 确定性 Verifier 强制每条结构化 Finding/Evidence 带 `context_manifest_id`、`context_hash`、`symbol_id`、`resolver`，因此无 Manifest 的 Agent Search 会天然被拒绝。

## 改造前真实执行链路

```text
AgentOrchestrator.run_review
  -> ContextBuilder.prepare_context
  -> AgentOrchestrator._prepare_relation_context
      -> RelationGraphIndex.build
      -> extract_changed_anchors / attach_changed_hunks
      -> ChangeCenteredContextPlanner.plan
      -> candidate_context_manifests
      -> graph/index/planner event telemetry
      -> 失败时 fallback_v022_tool_context
  -> Reviewer（统一 loop + 只读工具）
  -> severity review / FindingCandidate
  -> FindingVerifier
      -> diff + successful tool ledger + Manifest 组装 candidate_context
      -> schema-v2 确定性校验强制 Manifest provenance
  -> Finding Blocking / Finding Causality Graph
  -> RootCauseConsolidator / ConsolidationVerifier
  -> ReviewResponse
```

原 `RELATION_GRAPH_ENABLED=false` 会跳过 `RelationGraphIndex.build`，不会创建 SQLite 图索引；但 Prompt 和 schema-v2 Verifier 仍强制 Manifest，因此该路径不是语义等价的纯 Agent Search。Graph 构建失败 fallback 同样保留这一不一致，且原状态名 `fallback_v022` 不能清楚区分正常 Graph、Graph 失败后工具上下文和显式 Agent 模式。

## 耦合点、A/B 影响与本阶段改造

| 耦合点 | 原行为 | 对公平 A/B 的影响 | 第一阶段改造位置 |
|---|---|---|---|
| 模式配置 | 仅 `relation_graph_enabled: bool` | 无法表达 Agent、Graph Cold、Graph Warm 的实验身份 | `src/config.py`, `src/analyzer/context_mode.py`：增加 `ReviewContextMode`，旧 Boolean 仅作兼容入口 |
| Orchestrator | 主流程直接建图和规划 Manifest | Agent 路径难以证明没有部分建图 | `src/analyzer/context_strategy.py`, `src/orchestrator/agent_loop.py`：主流程只消费统一 `ReviewContext` |
| Graph fallback | 记录 `fallback_v022` | 容易把 Graph 失败误当正常非 Graph | `GraphHybridContextStrategy`：显式 `fallback_agent_search`、`fallback_reason`，模式仍记为 `graph_hybrid` |
| Reviewer Prompt | Common Prompt 强制 Manifest ID/hash | Agent Finding 天然无法满足 | `src/analyzer/prompts.py`：拆为 `COMMON_REVIEW_PROMPT`、`AGENT_SEARCH_POLICY`、`GRAPH_CONTEXT_POLICY` |
| Submit Schema | Evidence 的 Manifest/hash/symbol/resolver 全部 required | Agent Tool/Diff Evidence 无法正常提交 | `src/orchestrator/tool_schemas.py`：Graph 专属字段改为可选，核心 Finding Schema 保持唯一 |
| Finding Schema | `ReviewIssue` 已统一，但 Manifest 用空字符串表达 | Schema 类型共享，字段契约仍偏 Graph | `src/analyzer/output_formatter.py`：保留兼容空值并增加可选 `context_hash`；不创建第二套 Finding 类型 |
| Verifier Prompt | 要求跨文件证据必须在 Manifest | 排除真实成功的只读工具证据 | `src/analyzer/finding_verifier.py`：共享语义规则，按模式追加来源策略 |
| 确定性 Evidence 校验 | `provenance_in_candidate_context` 只接受 Manifest span/hash | Agent schema-v2 Finding 必然 fail closed | `src/analyzer/evidence_policy.py`, `src/analyzer/verifier_context.py`：统一核心校验，分别接受 Diff、Tool、Manifest 来源 |
| Tool Evidence | 已有独立成功调用 ledger，但未成为 schema-v2 合法来源 | 工具调查无法支持最终 accepted Finding | 保留 `capture_verifier_tool_evidence`，按真实成功调用、文件和 span 校验 Tool provenance |
| Consolidator | 接收 Manifest union；无 Manifest 时可运行 | 本身不要求 Graph，但上游无法送入 Agent Finding | 不改核心聚类；Agent 传空 manifests，Graph 继续传真实 manifests |
| Eval Runner | 每次固定构造 Orchestrator 与稳定 Graph index path | 无显式 Variant，运行身份不可冻结 | `eval/schemas.py`, `eval/runner.py`, `eval/run.py`：增加 `EvalVariant` 并直接注入 mode/cache contract |
| Eval Matcher | 主要按位置与 severity 匹配 | 没有 Graph 加分，但语义维度不足 | matcher `semantic-v2`：位置之外可声明 mechanism、invariant、affected paths；不读取 Variant/Manifest |
| Telemetry | Graph 指标较全，Agent/Graph 模式身份不统一 | 无法证明 Agent 未建图或区分 cold/warm | Context Strategy 与 `review_complete` 事件记录公共指标、Graph 状态/cache/fallback；Agent Graph 成本为 not applicable |

## 改造后统一执行结构

```text
AgentOrchestrator.run_review
  -> ContextStrategy.prepare
      -> AgentSearchContextStrategy（diff + safe read-only tools，无 Graph/Manifest）
      -> GraphHybridContextStrategy（Graph + planner + Manifest，失败显式 fallback）
  -> COMMON_REVIEW_PROMPT + mode policy
  -> 同一 Reviewer loop / submit schema / ReviewIssue
  -> 同一 severity gate / Finding Blocking / Finding Causality Graph
  -> 同一 FindingVerifier 语义门控 + EvidencePolicy 来源契约
  -> 同一 RootCauseConsolidator / ConsolidationVerifier
  -> 同一 Golden Dataset / semantic-v2 matcher / quality metrics
  -> Variant 和 mode-aware telemetry
```

## 已确认无需改动的模块

- `src/analyzer/review_policy.py` 与 severity review：不读取 Graph 或 Manifest。
- Finding Blocking、Finding Causality Graph 的机制/不变量/修复单元判断：不依赖 Code Relation Graph。
- `RootCauseConsolidator` 聚类核心：无 Manifest 时使用已验证 Finding 的证据并正常执行；仅在 evidence 声明 Manifest 时做 union/manifest 校验。
- `ConsolidationVerifier` 的共同机制、共同不变量、最小修复、反事实和 changed-line 主锚点规则。
- GitHub/CLI publisher 的 v0.2.2 兼容输出：继续消费统一 `ReviewIssue` 的 legacy 字段。
- Golden Findings 内容：第一阶段未根据运行结果修改任何 golden fixture 的正确答案。

## 尚未解决但不阻塞第一阶段的问题

- Provider 的 prompt/completion token 拆分并非所有兼容后端都返回；遥测对不可得值记录 `null`，总 token 保持真实值，不伪造拆分。
- Graph Cold/Warm 的正式统计比较和 held-out 运行属于第二阶段；第一阶段仅保证 Variant 配置、稳定 warm index 与隔离 cold index 路径可用。
- Repository Complexity Router、Graph Need Score、Lazy Graph、community detection、向量搜索和新 CFG/data-flow/taint 引擎均不在本阶段范围。
- 本文及基线报告只证明第一阶段 Agent 基线闭环；尚未执行、也不宣称完成正式 Graph A/B。
