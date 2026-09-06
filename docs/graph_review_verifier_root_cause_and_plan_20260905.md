# Graph A/B 与 verifier 根因排查及实施方案

日期：2026-09-05。仓库：`takagibit18/MergeWarden`。审计版本：`7a988a8d6dd69161f9678a2d10d34beb7739c27a`，分支 `fix/verifier-p0-p1-closure`。

版本边界：本文审计对象为 `fix/verifier-p0-p1-closure` 分支的上述提交，比本 PR 的 `main` 基线 `7e5a3ec01b85f56e98a538f118846f55851620ac` 多 21 个近期 Graph/verifier/eval 调优提交。本 PR 同时上传这些实现提交、排查方案和文档索引；代码行号、复现结果与故障归因均对应审计版本，后续修复仍按第 5 节拆分推进。

## 1. 结论与最终选择

**本轮图模式缺少优势，主要是证据在“检索 → 模型可见 → 提交交接 → 确定性校验”之间没有统一生命周期。图找到了路径，却没有稳定地把对应源码和变更事实送到最终判断；verifier 又把证据表示、字段缺失和截断问题当成 finding 不可发布的原因。**

推荐实施一版方案：**建立统一的已观察证据账本，统一 finding 输入及预校验契约，保留提交阶段的最小变更证据包，再将图输出改为全局去重、包含实际源码的少量因果取证包，并在实际发送模型请求之前统一执行预算。** 图索引继续保留；发布策略选择 Agent Search 为默认、Graph 为可选模式，只有通过本文的独立验收门槛后才提升 Graph 的默认优先级。

实施顺序固定为六个 PR：实验契约与重放基线 → 证据账本和 verifier → finding 契约与修复 → 提交交接与请求预算 → 图取证包和缺口检索 → 质量评测与发布门槛。无需再做架构选型或由使用者选择方案。

这里的“verifier 拒答”在实际代码中主要指 `FindingIntegrityGuard` 删除候选 finding，最终得到空报告；这批运行使用的是确定性 guard，并非独立 LLM 对问题真实性作出否定判断。不能把 `all_candidates_rejected` 解释成“已证明没有 bug”。

## 2. 审计范围、证据来源与边界

审计了以下本地产物，并逐条关联 `run_id`：

- `eval/outputs/graph-ab-glm53-flash-post-optimization-20260904/raw.json` 与 `summary.json`。
- 同目录 `run_journals/*_journal.jsonl` 中的模型响应、`submit_review` 原始参数和工具返回。
- 同目录 `deferred_workspaces/<workspace>/repo/.mergewarden/logs/<run_id>.jsonl` 中的上下文选择、请求 token、过滤及完整性失败明细。
- 当前仓库的 planner、reviewer projection、上下文组装、推理、编排、证据绑定、verifier、draft validator 和评测 matcher。

实验是 3 个 fixture × A/B 各 3 次，共 18 条 measured run；A/B 各 9 条，报告记录配对错误为空。`formal_graph_ab=false`、`held_out_executed=false`，仅有正样本且只有三个独立任务。因此可以确认实现故障与本批成本差异，**不能据此宣称统计显著的通用质量收益或估计真实场景误报率**。

本次另做了使用真实提交参数的离线反事实复现，并运行四个相关测试文件：`18 passed in 2.11s`。未调用外部模型重新跑付费 A/B，未修改仓库业务代码。本文所有目标数字均是后续验收标准，不是已经实现的收益。

### 2.1 重新聚合的基线

| 指标 | A：Agent Search | B：Graph Hybrid warm | 解释 |
|---|---:|---:|---|
| measured runs | 9 | 9 | 三个 fixture 各三次 |
| 实际总 token | 256,459 | 456,195 | B 增加 77.9% |
| 实际 prompt token | 242,929 | 438,032 | B 增加 80.3% |
| explore 阶段 prompt token | 128,871 | 230,926 | 增量 102,055 |
| submit_only 阶段 prompt token | 114,058 | 207,106 | 增量 93,048 |
| provider 请求次数 | 22 | 25 | 含提交修复；增加 13.6% |
| 编排 review iterations 总数 | 18 | 18 | 所有 run 都是两轮 |
| 工具执行次数 | 39 | 39 | 含预取，两组完全相同 |
| 最终候选数（进入 guard） | 6 | 5 | 是 finding 数，不是 run 数 |
| guard 接受／拒绝 | 1／5 | 0／5 | 接受不等于语义命中 |
| 自动 matcher 命中 gold | 0/9 | 0/9 | 存在字段契约造成的漏计，见 §3.8 |
| 端到端耗时合计 | 536.62 秒 | 552.34 秒 | B 增加约 2.9%；不能从 token 直接推导延迟 |
| 单 run 耗时中位数 | 35.15 秒 | 39.42 秒 | B 增加约 12.1%，样本很小 |
| provider 报告缓存 token | 55,040 | 59,264 | 缓存的绝对值不等于缓存效率 |
| 缓存 token / prompt token | 22.66% | 13.53% | 实际 token 覆盖比例下降 |
| 有缓存命中的请求占比 | 12/22＝54.55% | 12/25＝48.00% | 与上一行是不同指标 |

Graph 首轮实际选中投影的估算合计为 **97,854 token**；内部 planner 的 `graph_reviewer_context_token_estimate` 合计则为 430,137。两者不可混用，后者不是已发送或已计费 token。

| fixture | A 总 token | B 总 token | 内容层面的实际结果 |
|---|---:|---:|---|
| `development_agent_search_cross_file` | 45,480 | 57,982 | A/B 三次最终提交都描述了重复折扣核心缺陷；A 一次接受、一次预过滤、一次 guard 拒绝；B 三次均 guard 拒绝 |
| `golden_deepset-ai_haystack_pr12257_reverse` | 112,555 | 260,921 | 多数提交聚焦 API 参数兼容或 datetime 行为变化，未完整建立 gold 要求的相等／成员判断与排序判断策略不一致 |
| `golden_deepset-ai_haystack_pr12162_reverse` | 98,424 | 137,292 | 多跳因果链未完整形成；六份最终摘要均声称缺少 diff 或变更依据 |

“描述了重复折扣核心缺陷”是本次对原始提交的人工语义判断，不表示每条描述全部正确。例如 A 的一次接受结果把少收金额写成了 `overcharges`；这也证明 guard 接受不是语义正确性的充分条件。

## 3. 根因与证据链

### 3.1 P0：已观察证据的来源集合与 verifier 接收集合不一致

**已确认。** 模型能看到自动加载的文件和成功 grep 的具体代码行，但 verifier 只捕获 `read_file`、`get_changed_context`、`find_symbol_context` 等白名单工具；`grep_files` 不在集合中。自动加载的 `file_contents` 也没有作为同一份已观察证据交给 guard。

代码：`verifier_context.py:15–21,55–89`；`agent_loop.py:1124–1134,449–456`；`evidence_binding.py:98–165`。

实际日志证据：

- B `632ae562…` 的 journal 第 3 行明确返回 `test/document_stores/test_in_memory.py:106` 和 `test/components/routers/test_metadata_router.py:98` 的源码。事件日志第 49 行却以 `evidence_not_observed` 拒绝相同相关位置。
- A `bd4aecd2…` 的 journal 第 3 行返回 release note 第 4 行和 `test_in_memory.py:127`，guard 仍将这些引用标为未观察。
- A `e29a21ba…` 首轮上下文日志第 10 行显示 `metadata_router.py` 前 163 行已进入文件上下文，预取窗口却是 19–98 行。第 26 行 guard 拒绝 `metadata_router.py:18`：自动文件上下文已经展示的行没有进入 verifier 的证据集合。

这不应通过要求模型“再读一次”解决，否则图和自动预取节省的检索会被校验要求抵消。应登记实际展示过的精确源码行。grep 命中只证明已返回的行，不能顺带证明整个函数、邻接行，或证明没有其他匹配。

反方向也有结构性风险：guard 目前接收完整内部 manifests，而 prompt 投影可能只展示 header 或删去了部分 span。内部索引存在某源码并不证明模型见过它。证据账本需要区分 `indexed`、`selected`、`delivered`，只有真正展示过的正文才能用于已观察证据校验。

### 3.2 P0：verifier 用有损展示片段承担事实索引职责，长 diff 裁剪后行号解析失败

**已通过真实数据离线复现。** 目前 verifier 先按 finding 平分默认 12,000 字符，再对每个 `text/content` 独立截到 3,500 字符。diff 引用采用整个 hunk 填装。hunk 被截断后，保留范围检查却调用仅识别 `数字:源码` 的 `_numbered_read_lines`，而统一 diff 的正文是 `+/-/空格` 开头。

代码链：

1. `verifier_context.py:92–105`：预算为 `max(800, max_chars // len(candidates))`。
2. `verifier_context.py:451–484`：按完整 hunk 保留 `text`，没有先生成引用范围的行映射。
3. `verifier_context.py:22,804–845,1799–1820`：单条 3,500 字符裁剪与 envelope 预算叠加。
4. `verifier_context.py:1735–1765,754–764`：截断后的 diff 进入带数字行号的源码解析器。
5. `finding_integrity.py:360–385`：不可保留转为 `verifier_context_budget_exhausted`，随后 finding 被删除。

复现使用 B `69653d17…` 的真实最终候选，引用 `haystack/utils/filters.py:15`。原始 diff 已包含该行，绑定为 `git_diff`；在 `max_chars=12,000` 和 `120,000` 下，结果都是 `available=true, retained=false`。hunk 是 `@@ -12,59 +12,41 @@`，记录状态为 `added + clipped=true`，而不是总 envelope 放不下。

因此，**单纯提高总预算不能修复这个问题**。目前“预算耗尽”错误码还把单条截断、行号恢复失败和总量不足混在一起。上一轮预算归因修复让故障更可见，但没有消除造成故障的表示方式。

另外，binder 优先绑定 diff，即使同一位置有可用的 read 表示；绑定后 context builder 又只按该来源取证。这会让一个本可通过另一种已观察表示验证的位置，被迫走较差的 diff 表示。应以稳定证据 ID 和等价来源别名消除这种单一路径依赖，显式错误 hash 仍须拒绝。

### 3.3 P0：四套 finding 契约不一致，修复只修到“能解析”，没有修到“能验证”

**已通过反事实复现。**

| 环节 | 实际要求 |
|---|---|
| tool schema | `schema_version=2.0`，并要求因果、四角色证据等字段，`tool_schemas.py:199–295` |
| parser | 显式检查 `issues/confidence` 等；Pydantic 允许大量新字段缺省，`inference_engine.py:984–1010` |
| `ReviewIssue` | 版本默认 `1.0`；新证据列表默认空，`output_formatter.py:55–94` |
| integrity guard | 仅 `schema_version==2.0` 的风险 finding 强制 cause/contract；trigger/impact 有文本就再要求对应列表非空，`finding_integrity.py:519–535` |

B `fd550d5c…` 最终提交缺少 `causal_mechanism`、`finding_id`、`trigger_evidence`、`impact_evidence`。这些字段在 tool schema 是 required，但本地 payload 校验返回空错误，`ReviewReport` 可解析；随后 guard 以缺少 trigger/impact evidence 拒绝。

离线只改 `schema_version`，不改证据或源码：

| 原始提交 | 设置 1.0 | 设置 2.0 |
|---|---|---|
| B `fd550d5c…` | guard 通过 | 缺 trigger/impact evidence，被拒 |
| A `8a7ca5c9…` | guard 通过 | 同样被拒 |

这同时造成误拒和校验绕行。解决方式是统一内部契约及迁移入口，不能靠删除版本、降低严重级别或清空 trigger/impact 文本来“提高通过率”。

还有一个闭环缺口：`ValidateReviewDraftTool` 输入只包含旧展示字段及 `cause_evidence`，返回的 `submit_allowed` 是策略过滤检查，并未覆盖最终 guard 的完整证据契约（`review_draft_validator_tool.py:33–47,100–142`）。guard 在模型循环结束后运行，失败没有作为结构化可修复缺口反馈给原候选；同一角色列表中的坏引用也可能在已经有有效支持时导致整个候选失败。

本批拒绝原因按 candidate 去重：A 的 5 个拒绝候选中，4 个出现 `evidence_incomplete`、4 个出现 budget 问题；B 的 5 个拒绝候选中，4 个出现 `evidence_incomplete`、2 个出现 budget 问题。原因有重叠，不能相加当成候选总数。按明细计数为 A：10/4/9/2，B：7/3/2/0，依次对应 incomplete/budget/not-observed/path-missing。

### 3.4 P0：提交阶段清掉了 diff 与 Graph，且没有 draft 就不保留图证据

**代码与整批日志一致。** `inference_engine.py:161–169` 在任何 submit-only review 中清空 manifests、diff、file contents、project structure。`_manifest_evidence_for_drafts` 在没有 drafts 时立即返回空（1366–1372）。模型会话保存 assistant/tool turns，不包含此前的 system/user payload（`conversation.py:33–134`），所以“首轮发过图和 diff”不会自动让提交轮继续看见。

这批数据：

- 18/18 运行只有 explore 和 submit_only 两轮；首轮隐藏 submit，末轮强制 submit。
- draft 工具调用为 0；validator 工具调用为 0；提交交接中的 `manifest_span_count` 为 0。
- 多跳 B `fad3b7b9…`：首轮第 24 行明确选中了 **16 个 diff parts、28 条 graph paths**；提交轮第 31 行 manifest 数为 0，图投影为 0，交接仅有工具结果。最终 journal 第 4 行却说没有具体 diff/changed-line evidence。
- 三次 A 和三次 B 的多跳最终摘要都出现缺少 diff 的描述。原 fixture 确实有 diff，不能归因于测试输入原本为空。

这条链路足以解释“图可见路径增加，但最终因果判断没有提升”的重要部分。能确定丢失事实，不能在未做消融前量化它贡献了多少召回损失。把提交 prompt 的文字写得更强不会恢复被删掉的证据。

### 3.5 P0：搜索会搜到自己的运行日志，且大结果在提交和修复时反复发送

**有直接日志证据，且显著影响最大成本异常。** `grep_tool.py` 对工作目录做 `glob('**/*')`，没有排除 `.mergewarden` 的运行状态目录；journal 在工具执行前已落盘，因此根目录搜索能搜回自己的请求和图日志。

B `69653d17…` 的 journal 第 3 行：31 个 grep matches 中 6 个来自 `.mergewarden/logs` 或 `.mergewarden/runs`；这些条目的 `line_text` 共 **44,927 字符**，全部 match 正文为 46,757 字符，运行日志占 **96.1%**。另外三次 A 运行也搜到了自己的 journal。

该 B run 的五次 provider prompt token 为：38,265 → 22,214 → 23,970 → 24,754 → 25,899。后四次都是提交或提交修复，修复前的会话包含这份大型 grep 结果。其总 token 为 **139,583**。

同一 run 的前三次 submit 都缺少 `suggestion`，第一次还缺 `severity`。模型修复、外层强制提交、再修复叠加，使一个报告花了四次提交请求。`inference_engine.py:632–683` 拼回完整会话后继续发送，没有统一总提交次数限制和最终请求尺寸门槛。

应先排除 agent 运行产物、缩小 source 搜索结果，再做局部 schema 修复。不能把这个异常全部归因于图本身，也不能只归因于 provider 缓存。

### 3.6 P0/P1：实验声明与有效运行参数偏离，“3 轮”实际上只运行 2 轮

**偏离已确认；历史启动环境的完整来源未被日志保留。** YAML 的 `shared.max_iterations=3`，18 条运行的 `decision/continue.max_iterations` 均为 2。`graph_ab_pilot.py:454–458` 将配置再次交给 `runner._effective_review_max_iterations`；后者受 `eval_review_max_iterations_cap` 和最低工具轮次约束（`runner.py:1011–1018`）。

Graph reviewer 预算也不在本次 YAML 显式声明：日志值是 16,000，而当前源代码默认值是 900（`config.py:347–355`）。不应把本批 16k 宽图模式称为代码默认设置。现有日志不足以唯一恢复当时环境变量、启动脚本及 cap 覆盖的全部来源。

结果是：所有 run 都只有一次模型取证机会，之后立即进入缺少 diff 的提交阶段；validator 也没有机会完成预校验。与此同时，首轮隐藏 submit、固定执行预取，给两组共同设置了轮次和工具调用下限。即使 Graph 已经回答问题，也不能在当前流程直接兑现“一次调用完成”的收益。

该实验仍可用于诊断“两轮实际配置下”的行为，但不符合 YAML 声称的三轮契约。`valid=true`、无配对错误不能替代有效运行参数审计。`runner_readiness=false` 也不能单独用来说明所有结果无效；本次没有运行 B1 cold，完整 readiness 与这个诊断子集的目标不同。

### 3.7 P1：图投影主要是昂贵的导航元数据，未形成可直接判断的源码证据包

**已确认输出结构和成本；改善幅度需消融验证。** 当前采用逐 anchor planner、逐 manifest 路径保留。`select_graph_prompt_parts` 为有路径的候选保留无正文 header，并优先继续填路径；已有路径的候选不会再升级成完整源码 manifest（`context_priority.py:369–450`）。header 包含长 symbol ID、span/hash/source 等字段，却没有 `content`（`reviewer_projection.py:33–42,100–135`）。

| 代表运行首轮 | 可选路径 → 选中 | 图 token 估算 | header token | path token | 完整 manifest / header |
|---|---:|---:|---:|---:|---:|
| 重复折扣 B `fd550d5c…` | 2 → 2 | 717 | 331 | 386 | 0 / 1 |
| datetime B `632ae562…` | 223 → 20 | 15,945 | 10,223（64.1%） | 5,722 | 0 / 18 |
| snapshot B `fad3b7b9…` | 325 → 28 | 15,956 | 7,251（45.4%） | 8,705 | 0 / 8 |

图投影同时叠加在已存在的 diff、自动加载文件、结构树和预取结果上。datetime 的非图部分已包含 6,023 个 diff token、6,698 个文件 token、2,203 个目录 token；snapshot 分别为 3,536、6,000、2,199。图在首轮增加近 16k，却没有替换这些成本。

重复折扣示例尤其清楚：Graph 提供路径/header 后，模型仍和 A 一样读取 `discounts.py`、`test_checkout.py`，因为 header 的位置标记不足以证明折扣语义。**这里很多补读是合理的取证，不应只靠 prompt 禁止。**

已有优化确实去除了部分重复路径，首轮日志中重复折扣/datetime/snapshot 分别记了 1/43/37 条语义重复路径；不能说完全没有去重。但去重主要在单 manifest 内，planner 仍为多个 anchor 重建重叠的邻域，路径优先级看角色、测试惩罚和 hop 数，没有直接优化“这一组源码是否足以回答某个行为问题”。production path、role coverage 和 hop 指标不能证明 gold 因果链完整。

源码还显示 `_select_context_manifests` 用 `{''}` 初始化 requested IDs，导致无显式 ID 时的 anchor fallback 不可达（`verifier_context.py:1600–1615`）；本次已用构造输入复现。它是应一并修复的确定性缺陷，但没有将本批所有 Graph 拒绝归到这一点。

### 3.8 P1：质量与成本指标存在口径陷阱

1. **自动 matcher 会把语义描述正确但字段缺失的 finding 判成未命中。** A `8a7ca5c9…` 已描述重复折扣并被 guard 接受，但 `causal_mechanism` 为空；matcher 专门用这个字段匹配 `mechanism_pattern`，因此被计为 false positive（`runner.py:1714–1732`）。需要同时报告原始语义发现、规范化候选和最终发布，不应删掉 gold 机制约束以制造成绩。
2. **context component token 不是互斥分账。** `review_payload` 已包含 manifest，`graph_manifest_projection` 又包含其中路径；后面仍单独统计 graph paths 和 tool feedback（`inference_engine.py:1803–1842`）。把这些 component 相加会重复计数，不能据此断言同一图在请求里出现三份。
3. **“最终提交预算 8k”没有覆盖整个发送请求。** 预算约束局部 payload 和证据摘要，随后又加入 system、tool schema、完整会话、修复消息。本批按请求估算，A 有 8 次、B 有 10 次 submit-only 请求超过 8k。高额成本必须按每次实际 provider attempt 统计。
4. **缓存三种口径必须拆开。** Graph warm index 9/9 命中只说明复用了本地索引；相邻请求公共前缀是本地估算；provider cache tokens 才是服务端报告。本批没有足够信息确定服务端缓存键、保留期限或跨请求复用策略，不能把公共前缀变化直接解释成 KV cache 故障。

## 4. 选定的实现设计

### 4.1 统一证据账本：事实保存与模型展示分离

新增 `src/analyzer/evidence_ledger.py`，用有类型的模型保存每个已观察 artifact。最小字段为：`artifact_id`、snapshot/revision、规范化路径、diff side、完整行映射、内容 hash、来源类型、源工具调用 ID，以及各次请求实际展示的行区间。未展示的图节点仅是索引条目。

证据入账入口覆盖：原始 diff 的已发送片段、自动加载且已发送的 file parts、图取证包中的实际代码、成功工具结果。grep 以逐命中行入账，保留其 `truncated/scope`；不把“命中了路径”升级成看过整文件。模型正文、draft 文本和 agent 日志均不是源码证据。

引用绑定到 `artifact_id + side + 完整范围`。同一份源码被 diff/read/grep 多次观察可以具有来源别名；只有内容和行范围一致时才合并。必须完整覆盖引用范围，不再以任意 overlap 当作证明。显式错误 hash、越界路径、跨 revision 引用保持失败。

guard 根据账本查证，不再把用于模型展示的 12k 字符 JSON 当成唯一事实库。保留小片段用于解释与日志，但片段裁剪不能使已经入账且覆盖完整的合法引用变成 `not_observed`。若确实需要后续模型判断，再按候选引用提取最小完整行片段；这是展示预算，不是事实真伪开关。

guard 结果采用三态：

| 状态 | 含义 | 后续动作 |
|---|---|---|
| `verified` | 结构及证据引用满足契约 | 保留候选，后续语义/发布策略继续处理 |
| `needs_repair` | 缺字段、缺支撑、证据交接不全或需要补读 | 返回明确字段和 evidence gap，进入有预算的修复 |
| `invalid` | 伪造 ID/hash、错误 revision、不可接受路径等不可直接接受的问题 | 不发布该候选，记录原始原因 |

`verified` 表示完整性通过，不表示已证明业务判断正确。对语义反例仍由 reviewer 的因果判断和质量评测约束。预算不足无法补齐时，输出 `incomplete` 状态及未完成候选，不能冒充“没有问题”的空 review。

三态是内部 guard 契约；对外在 `ReviewResponse` 增加兼容字段 `completion_status=complete|incomplete` 和结构化未完成原因，保持既有 `ReviewIssue` 发布格式。CLI、reporter、publisher 和 eval 同步处理：可以保留已经验证的部分结果，但不得把 incomplete 渲染成“完整审查且无问题”。不得把未验证的候选直接放进公开风险 findings。

同时修复 `_select_context_manifests` 的空 ID 集合；保留精确 ID/hash 约束，禁止借 fallback 把显式错 ID 绑定到任意 manifest。

### 4.2 一套 finding 契约，旧展示字段由系统派生

新增内部 `FindingDraft`/`ClaimSupport` 模型，作为 prompt schema、本地 parser、draft preflight 和最终 guard 的共同输入来源。核心字段是严重级别、置信度、主位置、观察、因果机制、不变量、修复意图，以及包含 `role + statement + evidence_refs` 的支撑关系。

同一个证据 ID 可以支持多个角色；trigger/impact 可以是基于已观察源码的静态推导，但必须明确引用前提及推导说明。不要要求模型把相同代码复制四遍，也不要把普通自然语言自动当成已经观察到的证据。

输出给现有 publisher 的 `ReviewIssue` 保持兼容，由一个 adapter 生成旧 `location/evidence/suggestion` 字段和既有角色列表。允许的无模型修复范围固定为：

- 从有效 `primary_anchor` 规范化 `location`。
- 从明确的 `repair_intent.action/targets/boundary` 生成展示用 `suggestion`。
- 从已经有角色、statement 和合法引用的证据生成展示用 `evidence`。
- 规范化枚举、字段别名，去除精确重复引用；把同一已声明的引用用于其明确声明的多个角色。

不从任意段落猜造因果机制、不补置信度、不凭空添加 trigger/impact 证据。无法无损映射时，返回一次局部修复任务，要求补缺失 claim/ref；仍不满足则保留 `needs_repair/incomplete`。

活动 reviewer 的契约由运行时固定，不能由模型缺省 `schema_version` 决定校验强度。旧 v1 仅通过显式兼容入口解析历史报告；迁移前后的同一活跃候选必须走同一套完整性规则。纯文本 legacy 内容无法可靠升级时明确标记缺口，禁止静默视为已完整验证。

`ValidateReviewDraftTool` 与最终提交复用同一 preflight。它返回候选内容 hash、evidence ledger revision 和契约版本；内容或证据变化后旧的 `submit_allowed` 失效。候选已经通过确定性检查时，直接组装最终报告，不为搬运旧展示字段再增加模型调用。

### 4.3 提交交接保留最小变更事实，阶段转换由缺口决定

用 `ReviewHandoff` 替换“清空首轮上下文，再从最近工具结果拼摘要”的方式。它至少包含：

- 每个待判断变更的 before/after 行及有效 side，必要的 changed-anchor ID。
- 候选观察、因果机制、不变量、修复意图，已验证证据引用。
- 图或工具取得的必要跨文件支撑源码、未解决的 evidence gaps。
- 每个变更的处理状态：已审查、已形成候选、仍缺证据，及其依据。

不论有没有 `record_draft_finding`，变更事实都必须保留。draft 是模型提出的怀疑，不是系统保存 diff 的前置条件。无 draft 时从变更覆盖记录和已观察证据构造 handoff，不自动替模型生成“有 bug”的假设。

允许首轮在证据足够时直接提交；取消强制的最低工具调用次数，改成覆盖/支撑检查。有 material gap 且仍有工具轮次时继续精确取证；没有 gap 则确定性预校验并结束。默认不增加独立 LLM verifier，也不固定要求每个任务三次调用。

在诊断重放中先固定三轮有效上限，给多跳样本真实的第二次取证机会；最终轮上限作为上限，不作为必须消耗的配额。不要用“首轮禁止提交 + 第二轮强制提交”伪装自适应停止。

### 4.4 从逐 anchor 路径堆积改为少量含源码的图取证包

保留静态索引及 AST 能力，改变 planner 与模型间的输出单位。推荐一个请求默认 **4,000 个序列化估算 token 的 Graph 总配额**，必要时通过明确缺口扩到 **6,000**；每次最多三个正在处理的取证包。配置显式写入实验契约，不使用未声明的 16k 环境覆盖。

每包应回答一个可验证的行为问题，包含“变更点 → 写入/转换 → 读取/调用 → 约束或反例”的最小实际源码链。路径是找代码的索引；显示源码才能让模型判断。组织原则：

1. 按共享状态/数据对象和变更行为将重叠 anchor 分组，不把每个 hunk 都当成独立完整邻域；分组用于共享上下文，不自动合并 findings。
2. 全请求复用 `evidence_id → 源码` 表，跨 manifest 去重、合并重叠区间；图边使用短 ID 引用该表。hash、resolver、审计说明留在账本，模型侧只展示判断必需信息。
3. 以“新增可回答问题／新增源码覆盖／token 成本”选择包；对写读配对、调用入参与下游消费保留闭合链。每种角色各有一条路径不等于语义闭合。
4. 新取证包替换重复的整文件和预取片段；保留精确 diff。有必要保留的源码优先于目录树、长 symbol ID 和重复路径说明。
5. 查询由具体 gap 驱动。可见源码已覆盖时复用；缺正文时补目标区间；缺跨文件消费方时查询图或符号关系。禁止仅因为路径已在图中就阻止必要补读。
6. 某变更不值得建图或图证据包无法在配额内闭合时，退回该任务的 targeted Agent Search，记录路由原因。路由仅依赖变更/图特征，不读取 gold 标签或 fixture 名。

两个验收方向示例：datetime 要比较不同操作符对同一类输入的处理；snapshot 要对齐存储前的结构、序列化后的结构、恢复后的输入身份与后续消费。它们用于回归验收，生产实现不得硬编码 Haystack 的函数名、PR 号或 gold 文案。

### 4.5 最终请求预算与重试由同一组件管理

在 provider 调用前引入 `RequestAssembler`，统一计算完整的 system、实际 tools、payload、必要会话、handoff 和 repair feedback；任何修复或强制提交都必须经过同一入口。

提交输入上限选择 8,000 个完整请求估算 token。先保证最小变更事实和已验证支撑，再删重复历史、无关检索结果、目录树和冗余文字。使用当前 provider 的计数能力；若只能用代理 tokenizer，则明确报告估算方法并预留误差余量。不能承诺不同 tokenizer 的 provider 账单刚好等于估算值。

一份报告最多一次需要模型参与的局部格式/契约修复；本地无损修复不耗模型请求。transport retry 与业务修复分别计数，但共享总 token 和提交 attempt 额度。外层 forced finalize 不能重新初始化同一报告的额度。

工具消息若要求调用/结果配对，保留完整的最小配对，不截断成孤立 tool result。若选择新会话 handoff，则不携带遗留未完成的 tool calls。`minimal_submit_only` 的触发条件不能继续依赖一份只检查旧字段的 validator 结果。

稳定公共 policy、工具 schema 的排序、snapshot 内证据短 ID 可以稳定前缀；这是辅助优化。当前首要目标是减少无用正文和请求次数，不把 provider cache 命中作为优化成功的必要前提。

## 5. PR 与 commit 拆解

六个 PR 按下面顺序合入。每个 PR 保持可独立审查的主题，测试、协议文档和迁移说明与对应行为同提交，不将生产修改和必要测试人为分成可破坏主线的提交。涉及接口变更时同步 `docs/shared_contracts.md`、相关设计文档；按仓库规范执行 lint/type/test。

### PR 01 — 固定实验契约与失败重放基线

分支：`fix/eval-effective-runtime-contract`。

| commit | 内容 |
|---|---|
| `test(eval): add sanitized graph and verifier replay cases` | 将本次提交、工具结果、diff、失败原因做最小脱敏 fixtures；为版本差异、长 diff 裁剪、grep 可见却被拒、无 draft handoff、自搜日志建立重放入口。保留原运行 ID 到快照的映射 |
| `fix(eval): freeze and validate effective runtime settings` | 明确 configured/effective 参数，去除三轮被隐式 cap 成两轮的静默行为；冲突直接使运行 invalid，或在启动前显式解析为一个冻结契约。Graph budgets、最低工具轮次、provider policy、模型/工具版本全部参与 hash |
| `feat(obs): record request and evidence lifecycle provenance` | 增加阶段、全局 attempt 序号、schema repair 原因、选中/展示/保留的 evidence IDs、真实上下文版本；标明 configured/effective reasoning 设置。将原始/规范化/过滤后 finding 阶段关联起来 |

主要文件：`eval/graph_ab_pilot.py`、`eval/runner.py`、`eval/graph_ab_checkpoint.py`、`src/orchestrator/agent_loop.py`、`src/analyzer/inference_engine.py`、相关 eval schemas/tests。

验收：

- 18 条旧记录可以逐 run 重算本文 token/call/funnel 总数；旧报告以“历史有效设置未完全冻结”标识，原始结果不改写。
- 配置 3、有效 2 的构造用例不能再记为 valid；显式 3 轮时编排日志上限为 3。
- 修改 Graph 配额、cap、prompt/schema 或代码版本后 checkpoint 不得复用旧结果；无改变可安全 resume。
- 初始记录测试可以描述现有缺陷；对应修复 PR 合入时切换为正确行为的回归断言。不得长期将这些缺陷标记 xfail 后宣布验收。

### PR 02 — 统一已观察证据，修复 verifier 表示错误

分支：`fix/verifier-observed-evidence-ledger`；依赖 PR 01。

| commit | 内容 |
|---|---|
| `feat(analyzer): add observed evidence ledger and range identities` | 引入有类型的证据账本、精确行映射、revision/side/hash；在实际请求组装处登记已展示源码；为 diff/file/graph/read/grep 建 adapter |
| `fix(tools): exclude runtime artifacts from repository search` | 从默认源代码搜索排除运行日志和缓存目录；保留用户显式调试运行产物的专门路径，但它们不能作为 review 的源码证据；为长行设置明确截断标记和结果总量上限 |
| `fix(verifier): validate citations against retained source identities` | guard 改查账本；统一 diff 新旧行号映射；移除单条 3,500 字符展示裁剪对事实验证的影响；范围必须全覆盖，来源别名只在内容一致时合并 |
| `fix(verifier): separate repairable evidence gaps from invalid citations` | 引入三态结果；修复空 manifest ID fallback；相同失败不因剔除证据再产生误导性的二次 incomplete；可选附属引用缺失不压掉已有完整支撑的核心候选 |

主要文件：新增 `evidence_ledger.py`；修改 `evidence_binding.py`、`verifier_context.py`、`finding_integrity.py`、`context_priority.py`、`inference_engine.py`、`grep_tool.py`、`agent_loop.py`，以及 response schemas、CLI/report/publisher 的完成状态处理。

验收：

- `filters.py:15` 长 hunk 在 12k/120k 展示配额下引用结论一致；首部、尾部、增删行、空白行、多 hunk 都有真实行号测试。
- 同一合法候选的引用结果不随无关候选数量变化；1/2/20 个候选的公共引用保持相同结果。
- B `632ae562…` 的 grep 第 106/98 行、A `bd4aecd2…` 的 release note/第 127 行、A `e29a21ba…` 的自动加载第 18 行被登记为已观察；不把相邻未返回的行一并放行。
- `.mergewarden`、自己的 draft/journal、尚未展示的 graph body 不能成为已观察源码；默认根目录搜索不返回 agent 日志。
- 伪造 hash、跨 revision、范围部分相交、只有文件名、被截掉的尾行等负例保持失败。消除的是上述表示性错误，不是保证这些 finding 在语义上全部正确或全部发布。

### PR 03 — 统一 finding、预校验和局部修复

分支：`fix/analyzer-canonical-finding-contract`；依赖 PR 02。

| commit | 内容 |
|---|---|
| `feat(analyzer): define canonical finding and claim support models` | 同一类型源生成 reviewer schema 与解析规则；角色通过引用复用证据；活动运行的 schema 版本由系统控制，旧报告走显式兼容 adapter |
| `fix(analyzer): derive display fields from supported finding data` | 实现 location/suggestion/evidence 的无损派生和规范化；一次性返回全部不可派生缺口，不逐字段制造重试 |
| `fix(tools): share finding preflight with final verification` | draft validator 与最终 guard 复用规则及账本；validation receipt 绑定候选 hash/证据版本，候选变化必须重验 |
| `fix(orchestrator): repair canonical candidates without losing supported findings` | 修复反馈携带精确字段/ref 缺口；只允许一次模型局部修复；候选身份稳定，失败不会被静默转换成无问题报告 |

验收：

- active reviewer 不能再因漏写 `schema_version` 进入宽松 v1 校验；同一规范化候选的 guard 结果一致。
- 新 schema 合法输出与本地 parser/preflight 的结构要求一致；现有 B `fd550d5c…` 不能再“parser 合法但未提示缺角色”。
- 以真实已观察源码构成完整支撑后，重复折扣三次 B 候选可通过完整性检查；原始缺支撑的产物先明确要求修复，而非无条件放行。
- `suggestion` 缺失但有明确 `repair_intent`、`location` 缺失但有有效 anchor 的重放，不产生 provider 请求。缺严重级别或真实证据时不捏造默认值。
- 通过预校验且内容/账本未改变的候选，不因另一套格式规则被最终 guard 再拒；无关附属引用被剔除时保留审计记录和核心语义边界。

### PR 04 — 修复提交交接、阶段推进与完整请求预算

分支：`fix/evidence-preserving-submit`；依赖 PR 03。

| commit | 内容 |
|---|---|
| `feat(analyzer): preserve change evidence in review handoff` | 引入 `ReviewHandoff`；无 draft 也保存 before/after 和已观察支撑；清理基于 raw tool preview 的候选证据拼接 |
| `fix(orchestrator): advance review stages by evidence gaps` | 用缺口与覆盖状态控制 explore/preflight/submit；取消强制最低工具轮次，允许证据足够的首轮提交；记录预算截止与自然完成的不同原因 |
| `fix(models): enforce complete request and report repair budgets` | 统一所有 provider 入口的完整序列化预算；一个报告最多一次模型格式修复；外层 finalize 共享额度；保留合法最小 tool-call 配对 |
| `test(orchestrator): replay no-draft and oversized finalization flows` | 覆盖 18-run 中的无 draft、无 validator、大 grep 历史、四次 submit、预算临界和工具未配对恢复路径 |

验收：

- 无 draft 的 snapshot 案例中，最终请求有真实 changed diff 与必要跨文件支撑；不再因实现清空而出现 `diff_text=null` 且没有替代的变更证据包。
- 原 B `69653d17…` 重放在本地能修的字段不调用模型；需要模型修复时总提交请求最多两次，外层不能再启动第二套修复额度。
- 所有 submit/repair/forced-finalize 的完整请求估算均 ≤8k；不能靠删除唯一 changed anchor、伪装已审查或省略必需证据满足预算。
- 必要证据确实放不下时返回明确 `incomplete`，不发布未经验证风险项，也不宣告“没有问题”。
- 缺口已闭合的简单 fixture 可一轮模型调用完成；存在跨文件缺口的 fixture 仍能使用剩余轮次进行读取和预校验。

### PR 05 — 图输出改为全局去重的因果取证包

分支：`perf/graph-causal-evidence-packs`；依赖 PR 02–04。

| commit | 内容 |
|---|---|
| `perf(graph): share source spans across changed anchors` | 对重叠 anchor 的源码、节点、路径建立请求级去重；span 正文只序列化一次，header 改短 ID；保留全部内部 provenance |
| `feat(graph): select closed evidence packs under a global budget` | 以实际源码和问题闭合选包，替换“header 后持续填 paths”；显式 4k 初始、6k 最大配额及最多三活动包 |
| `perf(analyzer): reuse visible evidence before targeted retrieval` | 图与普通文件/预取采用同一覆盖表；已有正文复用，未覆盖 gap 定向补读；加入可解释的 Graph/Agent Search 任务路由 |
| `test(graph): verify causal pack closure beyond path coverage` | 用 datetime、snapshot 及不同名称/仓库结构的同类任务测试因果链正文覆盖、去重、预算和降级 |

验收：

- 每个展示的图取证包均有可读取的实际源码和 changed anchor；不能只凭 `selected_production_path_count>0` 通过。
- reviewer Graph 初始序列化内容 ≤4k，定向扩展后 ≤6k；metadata/header 占 Graph token ≤20%；相同 evidence ID 正文在一个请求内只出现一次。
- 重复折扣 Graph 包包含 helper 和测试的具体源码；在工具 fake 验证中不再因为只有 header 而强制补读相同区域。
- datetime 的包能显示相等/成员与排序各自处理同一输入的差异；snapshot 的包能显示输入身份在保存、恢复、后续消费之间的传递。验证基于代码片段和引用关系，不能以 gold 字符串包含判断选包正确。
- 减少源片段不得丢失完整性负例防线；跨文件支撑不可用时明确 gap 或降级，不伪造“完整链”。实际模型质量收益留到 PR 06 判断。

### PR 06 — 分层质量评测、消融与发布门槛

分支：`feat/eval-stagewise-quality-gates`；依赖前五个 PR。

| commit | 内容 |
|---|---|
| `feat(eval): separate discovery normalization and publication quality` | 分别计 raw semantic discovery、规范化 root-cause match、policy/guard 删除和最终发布；固定 matcher 版本，保留错误原因与可追溯人工标注 |
| `fix(obs): reconcile exclusive request costs with provider attempts` | component 标记 parent/scope，不再把子视图相加；逐 attempt、阶段、工具计数；区分本地 index cache、服务端命中请求率和缓存 token 比例 |
| `test(eval): add paired ablations and negative regression cases` | 固定消融矩阵与新 held-out 集，加入 no-bug/相似但无 bug/错误引用/旧 API 合法迁移等负例；A/B1/B2 使用相同公共契约 |
| `chore(eval): publish graph promotion decision and rollback criteria` | 发布原始数据、分层报告、置信区间、成本和门槛判定；只有全部达标才更新默认路由，否则保留可选 Graph |

验收详见下一节。该 PR 的算法与 matcher 在 held-out 执行前冻结；不能看完最终测试再调阈值、改 gold 或改变分组。

## 6. 分阶段验收与最终发布标准

### 6.1 G0：可信基线（PR 01 后）

18 条旧日志汇总可复算，配置差异可检出，checkpoint hash 包含全部有效处理参数。新的“有效运行”必须满足代码/snapshot/model/prompt/schema/settings 可追溯；pairing 只是其中一项。保留本批作为缺陷定位集，不能再当未见测试集。

### 6.2 G1：证据正确性（PR 02–03 后）

以真实故障重放和新负例为主要门槛：

- 已观察完整源码因为 whitelist、预算展示片段或行号表示而被拒：**0**。
- 伪造、未展示、部分覆盖或错 revision 的引用被当作已观察证据放行：**0**。
- schema 缺省造成绕行，以及 parser 与 preflight 未说明的分歧：**0**。
- 缺真实支持的候选进入 `needs_repair`；不能将三态结果简化成“全部接受”来冲高召回。

定义 `representation_false_reject_rate` 的分母为经账本和重放证实完整可观察的引用/候选，分子为仅因表示/交接/裁剪而失败的数量；不要把所有 rejected finding 都计作误拒，也不要因一个候选有三条错误而计三次候选失败。

### 6.3 G2：交接与预算（PR 04 后）

无 draft 也能在提交请求中保留变更事实；最贵样本不再搜回 agent 日志；所有真实发送路径共享预算；同一报告最多一次模型契约修复；已支持候选经过压缩与阶段转换后保持证据 ID 和结论身份。检查“最终实际请求”及其 token，而不是仅测 helper 返回字符串长度。

### 6.4 G3：图结构与源码覆盖（PR 05 后）

4k/6k 总配额、header 比例、跨 anchor 正文去重、取证包源码闭合通过。普通局部变更不被强制建大图；数据流或状态流任务使用图，相关代码不可达时回退目标搜索。这一阶段只证明上下文实现有效，不能单凭路径数宣布 review 质量有优势。

### 6.5 G4：模型行为、成本和通用性（PR 06 后）

先执行开发集上的逐项消融，公共修复同时应用 A/B，Graph 专属修复只作用于 B：

| 消融组 | 相对上一组的唯一新增变化 | 要回答的问题 |
|---|---|---|
| R0 | 本批历史输出及缺陷重放 | 已确认的故障能否稳定复现 |
| R1 | 冻结有效三轮配置与完整日志 | 排除原先的轮次/环境混杂 |
| R2 | 统一证据账本和 finding 契约、排除运行日志 | 误拒和本地可修 schema 请求是否消失 |
| R3 | 保留 handoff、缺口驱动阶段、完整请求预算 | 丢失 diff、重复提交和上下文膨胀是否消失 |
| R4 | Graph 因果取证包与缺口检索 | 在公共流程相同的前提下，图本身还提供多少增量 |

R1–R4 使用同一代码快照中的功能开关、fixture、模型设置和预算；不比较一组已修 verifier 与另一组未修 verifier。每组报告 raw discovery → normalized candidate → final publication 的漏斗，并记录每个 graph-only gain/loss 的证据。

随后冻结配置，在 **60 个独立、未用于本轮调试的 held-out fixture** 上各重复 3 次：20 个局部正例、20 个图敏感正例（直接跨文件/多跳各 10）、20 个负例。至少覆盖三个独立仓库，纳入实际支持语言；同一 bug 的多个切片不能算独立样本。A/B1 cold/B2 warm 各运行，warm priming 单列，不混入 measured token。

以 fixture 为配对统计单位，先聚合其三次重复，再计算按 fixture 重采样的配对区间；不要把 180 次相关运行当成 180 个独立问题。报告点估计和 95% 区间。三个旧 fixture 只作为机制回归，不参与正式优势结论。

**推荐的 Graph 默认启用门槛，全部满足才提升：**

| 维度 | 预先冻结的标准 |
|---|---|
| 图敏感任务质量 | 最终 root-cause recall 相比同版 A 提升至少 10 个百分点，配对差值 95% 区间下界 >0 |
| 整体质量 | 整体最终 recall 与 precision 的配对差值区间下界均不低于 -2 个百分点；单列负例误报数和率 |
| 表示性误拒 | G1 回归集为 0；held-out 出现的新表示性误拒逐条查明，不以“提高 confidence”掩盖 |
| Warm 成本 | B2 每 fixture 平均总 provider token / A ≤1.00；provider attempts / A ≤1.00；另报 graph 子集和全体，防止样本构成掩盖成本 |
| 延迟 | B2 warm 的 p95 端到端延迟不高于 A 的 1.10 倍；冷构建/首次使用延迟单列 |
| 请求可靠性 | 所有实际发送入口满足完整请求预算；无重复 finalize 额度重置；必要证据不足不输出假成功 |
| 覆盖与一致性 | A/B 配对、有效参数、请求/事件/账单 token 对账无未解释缺口；内部 graph path 成功不能代替最终 quality |

60 个任务可能仍不足以让区间满足严格门槛；若区间不确定，则结论就是“尚未证明优势”，保持 Graph 可选，不事后放宽标准。后续扩样须另行冻结新样本和规则，不能反复查看同一 held-out 后调整实现。

cold index 成本另计：同一 snapshot 的首次建图与后续复用分别报告，并按预先声明的复用次数给出摊销。provider token 缓存使用真实返回数据；没有 token 单价/账单时报告 token 和延迟，不虚构美元成本。

## 7. 验证方法、回滚与落地边界

### 7.1 本次已经执行的验证

- 聚合 18 份 measured record，并与逐 attempt 的事件日志、原始模型 journal 对齐。
- 对两份真实重复折扣提交做版本反事实：1.0 通过、2.0 缺 trigger/impact evidence 被拒。
- 对真实 `filters.py:15` 引用把 verifier 展示总预算从 12,000 提至 120,000，仍复现裁剪/行映射失败。
- 复现无 draft 时 manifest 交接为空，以及无显式 manifest ID 时 fallback 返回空。
- 复现 tool schema required 字段缺失但 parser/Pydantic 仍通过。
- 运行 `test_verifier_context_union.py`、`test_graph_hybrid_token_optimization.py`、`test_graph_ab_runtime_contract.py`、`test_graph_reviewer_path_reservation.py`：**18 passed**。这说明已有局部合同测试未覆盖上述真实 producer→consumer 组合，不能用局部测试通过替代闭环回归。

### 7.2 每个实现 PR 的基本检查

修改范围的单元和集成测试首先通过，再执行仓库规定的 `ruff check .`、`ruff format --check .`、`mypy src/`；合入公共接口和最终验收前运行全量 pytest。对发布/报告兼容性保留 `ReviewIssue/ReviewReport` 的旧输出测试。

关键集成测试要穿过真实边界：工具/自动文件输入 → 实际消息选择 → evidence ledger → parser/adapter → preflight/guard → 最终报告。用 fake provider 记录真实请求来测重试与预算；不要只构造一份手工填全的 evidence 列表去证明 validator 可以接受。

### 7.3 回滚与停止条件

- PR 01 的审计和契约记录可持续保留，不因功能回滚删除原始事实。
- PR 02–03 属于两种模式共同的正确性修复。兼容性故障回滚入口 adapter 或输出映射，不恢复“未展示也当证据”的规则。
- PR 04 用 handoff/request-assembler 开关隔离；若 provider 对会话配对存在兼容问题，回退到经过预算核算的配对历史，保留 diff 和证据账本。
- PR 05 独立 Graph 取证包开关。质量或成本未达标时默认回 Agent Search，证据/契约修复仍保留。
- 出现错误 hash/未展示源码被放行、已有已支持 finding 丢失、或预算机制导致正常报告被静默清空，立即停止提升默认 Graph，修复后重跑相应门槛。

本次交付是根因与实施设计。后续代码实施应按上述 PR 顺序推进；不需要再就“扩大图、换模型、降低 verifier 阈值”做一轮选择。

## 8. 可追溯证据索引

以下使用审计版本内的仓库相对路径及行号定位。审阅代码时需使用开头指定的 commit；原始 `eval/outputs/` 运行日志保存在本地，未随本次文档提交。JSONL 的行号是物理行，journal 的行号与本批 `seq` 一致。后续 PR 01 应将脱敏的最小重放输入、预期结果及有效运行契约纳入版本管理，使新增回归不依赖这些本地完整日志。

### 8.1 原始实验

- 实验 YAML：`eval/variants/graph-ab-glm53-flash-post-optimization-20260904.yaml`
- raw.json：`eval/outputs/graph-ab-glm53-flash-post-optimization-20260904/raw.json`
- summary.json：`eval/outputs/graph-ab-glm53-flash-post-optimization-20260904/summary.json`

### 8.2 代表性运行

| run_id | 模式/样本 | 查证位置 |
|---|---|---|
| `fd550d5c-da54-43ff-a728-87a5783cd26e` | B 重复折扣 1 | journal 4/5：缺字段及修复后提交；事件 29：缺 trigger/impact evidence |
| `8a7ca5c9-b49b-4717-bcb8-aa54fb057fc9` | A 重复折扣 2 | journal 5：未声明 2.0；事件 24：guard 接受；raw matcher 未命中 |
| `632ae562-3a56-4d43-a58f-74e08840ca09` | B datetime 1 | journal 3：grep 实际源码；事件 32：16k 图首轮；49：未观察/预算拒绝 |
| `e29a21ba-aaed-45bc-8cb3-023a974c6573` | A datetime 3 | 事件 10：文件前 163 行已加载；26：第 18 行未观察拒绝；journal 2：grep 证据 |
| `bd4aecd2-286c-40f0-a6f7-953bd419e21d` | A datetime 2 | journal 3：release note 与测试行确实返回 |
| `69653d17-78da-4457-97a3-213de8e91720` | B datetime 3 | journal 3：自搜运行日志；4–7：四次提交；事件 38：实际上限两轮；40/41/46/47：提交计费 |
| `fad3b7b9-3401-4cee-ae25-e125f613908a` | B snapshot 1 | 事件 24：有 diff/图；31：提交时已清空；journal 4：声称没有变更依据 |

- B 重复折扣最终提交：`eval/outputs/graph-ab-glm53-flash-post-optimization-20260904/run_journals/development_agent_search_cross_file_B2-graph-hybrid-warm_fd550d5c-da54-43ff-a728-87a5783cd26e_journal.jsonl:5`
- B 重复折扣 guard 明细：`eval/outputs/graph-ab-glm53-flash-post-optimization-20260904/deferred_workspaces/11303f15/repo/.mergewarden/logs/fd550d5c-da54-43ff-a728-87a5783cd26e.jsonl:29`
- B datetime 的 grep 结果：`eval/outputs/graph-ab-glm53-flash-post-optimization-20260904/run_journals/golden_deepset-ai_haystack_pr12257_reverse_B2-graph-hybrid-warm_632ae562-3a56-4d43-a58f-74e08840ca09_journal.jsonl:3`
- B 最大异常中的自搜日志：`eval/outputs/graph-ab-glm53-flash-post-optimization-20260904/run_journals/golden_deepset-ai_haystack_pr12257_reverse_B2-graph-hybrid-warm_69653d17-78da-4457-97a3-213de8e91720_journal.jsonl:3`
- B snapshot 最终空结果：`eval/outputs/graph-ab-glm53-flash-post-optimization-20260904/run_journals/golden_deepset-ai_haystack_pr12162_reverse_B2-graph-hybrid-warm_fad3b7b9-3401-4cee-ae25-e125f613908a_journal.jsonl:4`

### 8.3 主要代码入口

- 证据捕获白名单与 verifier 上下文：`src/analyzer/verifier_context.py:15`
- 长 diff 填装：`src/analyzer/verifier_context.py:451`
- 截断后的行号验证：`src/analyzer/verifier_context.py:1735`
- 证据来源绑定优先级：`src/analyzer/evidence_binding.py:168`
- 结构化 finding 必需角色：`src/analyzer/finding_integrity.py:519`
- 缺省 v1 与结构化判定：`src/analyzer/output_formatter.py:55`
- 模型 tool schema：`src/orchestrator/tool_schemas.py:275`
- 提交阶段清空上下文：`src/analyzer/inference_engine.py:161`
- 无 draft 的 manifest 交接：`src/analyzer/inference_engine.py:1366`
- 模型格式修复请求：`src/analyzer/inference_engine.py:632`
- 只给路径候选保留 header：`src/analyzer/context_priority.py:369`
- Graph header 投影：`src/analyzer/reviewer_projection.py:100`
- 源代码搜索的目录遍历：`src/tools/grep_tool.py:77`
- 实际 eval 轮数计算：`eval/runner.py:1011`
- 语义 matcher 对因果字段的匹配：`eval/runner.py:1714`
