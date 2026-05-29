# MergeWarden Agent 岗技术面试 40 问参考回答

> 用途：作为投递 Agent 开发 / LLM 产品岗位时，围绕 MergeWarden 主项目准备技术面试。
> 口径：把项目讲成一个 advisory-only 的 AI PR gatekeeper，而不是自动合并或自动修复系统。
> 更新时间：2026-05-25

## 1. 你会如何用 2 分钟介绍 MergeWarden？

**参考回答：**

MergeWarden 是一个面向 PR 审查场景的 AI agent 项目，目标是补充传统 CI 难以覆盖的风险判断，例如代码变更的行为回归、测试缺口、边界条件和维护性问题。它默认是 advisory-only：输出 review 建议、soft check 和可复盘证据，不替代 GitHub CI 或 branch protection 的硬性合并裁决。

工程上它不是单次 LLM 调用，而是包含 CLI、FastAPI 薄层、五阶段 agent loop、工具调用、安全分级、结构化输出、运行日志、golden eval 和 GitHub Actions advisory workflow 的完整闭环。当前 MVP+ 的工程路径已经完成，GitHub advisory 发布也已经具备 dry-run 和 same-repo publish 的基础能力。

## 2. 这个项目为什么适合作为 Agent 开发岗位作品？

**参考回答：**

因为它覆盖了 agent 项目里比较关键的工程问题：任务编排、工具调用、上下文管理、输出 schema、权限边界、失败可观测性、评测闭环和产品集成。很多 demo 只展示“LLM 能生成一个回答”，但 MergeWarden 更关注“LLM 如何可靠地嵌进真实软件工程流程”。

它还能体现我对边界的判断：MergeWarden 不追求自动合并或自动修复，而是先做可信的审查和证据输出。这个取舍对产品岗位也重要，因为 PR 场景里错误地给出硬阻断或自动 patch 的代价很高。

## 3. 为什么项目定位为 advisory-only，而不是自动 merge gate？

**参考回答：**

主要是风险控制。LLM review 适合发现传统 CI 漏掉的语义风险，但它的稳定性、可解释性和误报率还不足以直接替代硬门禁。CI、类型检查、测试和 branch protection 仍然应该是硬合并 authority。

MergeWarden 的价值是提供 soft check、inline advisory comment 和 evidence，让人类 reviewer 更快定位风险。这样既能利用 LLM 的语义分析能力，又不会把模型误判直接升级为阻断合并的生产事故。

## 4. MergeWarden 的核心架构怎么分层？

**参考回答：**

可以分为六层：

1. 入口层：CLI 和 FastAPI，负责用户输入、JSON 输出和 HTTP 接口。
2. 编排层：五阶段 agent loop，处理上下文准备、模型分析、工具执行、结果处理和继续/终止判断。
3. 工具层：read-only、write、execute 工具，以及工具 schema 和安全分级。
4. 分析层：prompt、context builder、location normalization、result processor。
5. 集成层：GitHub advisory adapter / publisher，把 review 结果转换成 check run 和 review comment。
6. 评测与观测层：event log、run summary、golden fixtures、eval gate、diagnostics。

这个分层让 CLI、API、CI 和 GitHub workflow 可以复用同一套核心模型与输出契约。

## 5. 五阶段 Agent Loop 具体是什么？

**参考回答：**

五阶段是：prepare context -> model analysis -> tool execution -> result processing -> continue or stop。

第一阶段准备 diff、文件片段和项目结构；第二阶段让模型基于 prompt 和工具 schema 决定是否需要工具或是否提交最终 review；第三阶段执行工具调用；第四阶段把工具结果、模型输出和结构化结果合并；第五阶段根据是否有待执行工具、是否达到预算、是否已提交 final response 来决定继续或终止。

这个设计避免了“一次性把全仓库塞给模型”的粗暴方案，也让每轮工具结果可以反馈给下一轮模型调用。

## 6. 你为什么需要 tool_feedback？

**参考回答：**

因为多轮 agent 的关键不是工具能不能调用，而是工具结果能不能改变下一轮推理。`tool_feedback` 把上一轮工具调用的结果、错误、摘要和迭代信息带回模型上下文，让模型知道哪些证据已经查过、哪些工具失败、哪些信息可复用。

这能减少重复工具调用，也能让模型从“猜测”转向“基于已观察结果继续分析”。对 PR review 来说，这尤其重要，因为模型经常需要先读 diff，再读相邻上下文，再决定是否形成 finding。

## 7. 为什么要区分 readonly、write、execute 三类工具？

**参考回答：**

这是权限边界。review 场景默认应该以只读工具为主，例如读文件、grep、glob、list dir。写工具和执行工具风险更高，可能修改仓库或运行不可信命令，所以必须有单独的安全级别、串行执行策略和确认/拒绝路径。

这个设计体现了 agent 工程里的最小权限原则。LLM 不能因为“想知道测试结果”就随便执行任意 shell 命令，尤其是在 CI、用户仓库和 fork PR 场景中。

## 8. execute 工具最主要的安全风险是什么？你怎么控制？

**参考回答：**

主要风险包括命令注入、运行高危命令、越界访问文件系统、泄露环境变量、长时间占用资源和在 CI 中执行不可信代码。

MergeWarden 通过几类方式控制：命令 argv 解析、first-token allowlist、`shell=False`、cwd 约束、环境变量清理、timeout、输出截断、CI 默认拒绝 execute、以及可选 Docker backend。核心思路是让 execute 成为受控能力，而不是任意 shell。

## 9. 为什么输出必须使用 Pydantic schema？

**参考回答：**

因为 MergeWarden 的输出不是给人随便看的聊天文本，而是要被 CLI、FastAPI、eval、GitHub adapter 和 publisher 消费的结构化数据。Pydantic schema 能让字段、类型、必填项和错误处理稳定下来。

例如 `ReviewResponse` 里包含 `run_id`、`summary`、`issues`、`context` 等字段；每个 issue 又有 severity、location、evidence、suggestion、confidence。这样后续才能做 changed-line filtering、fingerprint、eval matching 和 GitHub inline comment。

**追问：结合一两个实例说明一下在哪些模块用 Pydantic，实现约束的基本原理是什么？**

**参考回答：**

第一个例子是 `src/analyzer/schemas.py`。这里定义了 `ReviewRequest`、`DebugRequest`、`ReviewIssue`、`ReviewResponse`、`DebugResponse` 等核心数据模型。模型输出不是直接当字符串使用，而是必须能被解析成这些 Pydantic model。例如 `submit_review` 的参数最终要满足 `ReviewResponse` / `ReviewIssue` 的字段约束：`issues` 必须是列表，每个 issue 必须有 severity、location、evidence、suggestion、confidence 等结构化字段。如果模型漏字段、字段类型错、或者 severity 不在允许范围内，解析就会失败，系统会记录 validation error，而不是把不可信 JSON 当成有效 review。

第二个例子是 `src/integrations/github_publisher.py`。GitHub 发布层使用 `GitHubPublishRequest`、`GitHubPublishResult`、`CommentLifecyclePlan`、`PendingReviewComment`、`CommentUpdate` 等 Pydantic model 来约束发布输入和输出。这样 `changed_lines`、`head_sha`、`dry_run`、fingerprint、comment lifecycle 这些字段都有明确结构。adapter 先把 `ReviewResponse` 转成候选 comment，publisher 再根据这些 model 生成 dry-run plan 或真实 GitHub API 调用，避免网络层直接消费松散 dict。

基本原理是：Pydantic model 把 Python 类型标注、必填字段、默认值、嵌套对象和可选字段变成运行时校验规则。外部 JSON、LLM tool-call arguments、CLI 读入文件或 API request 进入系统时，先通过 `model_validate` / 构造函数解析；解析成功才进入后续业务逻辑，解析失败就抛出 `ValidationError` 或被记录到 event log / run summary。它本质上是在 LLM 非确定性输出和工程确定性接口之间加了一层 typed boundary。

## 10. `submit_review` 伪工具解决了什么问题？

**参考回答：**

它把“最终答案”也变成工具调用，而不是让模型自由输出 JSON 文本。这样可以利用 OpenAI-compatible tool calling 的结构化参数能力，把最终 review 结果直接解析成 `ReviewResponse`。

这比“让模型输出一段 JSON 然后正则解析”更可靠，也便于记录 `submit_review_seen`、schema validation error 和 force submit 行为。对 eval 来说，这些事件能帮助判断失败是模型没提交、提交格式错，还是确实没有发现问题。

## 11. force submit 为什么必要？

**参考回答：**

多轮 agent 有时会一直请求工具，或者在接近迭代/预算上限时仍没有提交最终 review。force submit 是兜底机制：当系统判断已经接近结束但没有结构化结果时，只暴露 `submit_review`，要求模型基于已有信息给出最终结论。

它不是为了强迫模型产生问题，而是为了避免因为流程没有闭合导致 placeholder summary 或空结果。真实 review 场景里，“没有问题”也应该是一个明确的结构化结论。

## 12. 你如何设计上下文管理？

**参考回答：**

MergeWarden 采用 diff-first 策略。初始上下文优先包含 PR diff、changed files 和项目结构，而不是整个仓库。未变更文件主要作为证据上下文，需要模型通过只读工具按需读取。

结合 agent loop 看，准备阶段会先构造 `ContextState`，记录 goal、constraints、decisions、errors 等审计状态；如果是 review diff 模式，会加载 `diff_text`、项目结构、变更文件内容片段，并把 `diff_mode` 加入约束。随后 prompt 组装阶段会把系统提示词、用户任务 payload 和可用 tool schema 一起传给模型：系统提示词要求它作为 senior code reviewer，必须基于具体 diff evidence 输出结构化 finding；user input 则包含 repo path、diff、file contents、project structure、context state、selected/truncated context parts 等。

多轮循环里，模型第一轮可以直接 `submit_review`，也可以请求只读工具读取相邻文件、测试或接口定义。工具执行后不会简单拼成一大段聊天历史，而是进入 `_tool_feedback`：下一轮 prompt 会追加 `tool_feedback` message，告诉模型某一轮执行了什么工具、参数是什么、结果摘要是什么、是否失败或被截断。这样模型能基于实际观察继续推理，也能避免重复读取同一个路径。

上下文保留采用“当前任务状态 + 最近有效工具结果 + 折叠后的旧工具摘要”的策略。完整的 `ContextState` 会跟随 response 返回用于审计；prompt 侧优先保留 diff、changed file 片段、项目结构和最近工具结果。旧轮次工具结果如果太多，会折叠成 `prior_tool_results_summary`，提醒模型这些工具已经执行过，完整结果不再放入上下文，但不要用同样参数重复请求。

如果上下文接近或超过预算，项目里有两层处理：第一层是按优先级和 token 预算做 greedy truncation，只保留最重要的 context parts；第二层是在启用 summary compaction 时，把被丢弃的部分交给 `ContextCompressor` 生成摘要，再尝试重新塞回预算内。运行时还有 token soft/hard budget、pre-budget submit 和 force submit：软超预算会尽量让模型基于已有证据收口，硬超预算则返回 partial result 并在 run summary 里记录 budget state。

这个项目目前不需要跨 PR 的长期记忆。PR review 的可信边界应该来自“本次 diff + 当前仓库快照 + 本轮工具观察 + 可复盘日志”，否则历史记忆可能把旧项目事实、旧接口或过期约束带入当前审查，造成污染。MergeWarden 做的是 run-scoped memory：`ContextState`、event log、tool feedback、run summary 和 eval artifacts 用来复盘单次运行；跨运行层面只保留评测报告、fixture 结果、diagnostics 和趋势统计，用于改进系统，而不是在 review 时直接作为模型长期记忆注入。

**追问 12.1：准备阶段具体塞入哪些提示词和用户输入？**

**参考回答：**

准备阶段不是直接把用户一句话转给模型。`prepare_context` 先建立本次运行的结构化状态，然后 review 模式会加载 diff、项目结构和变更文件片段。进入模型前，`InferenceEngine` 会组装三类内容。

第一类是 system prompt，例如 review 模式的核心要求是“你是 senior code reviewer，要分析 diff/files，最后必须通过 `submit_review` 返回结构化结果”。其中还包含约束：evidence 必须引用具体 changed diff lines 或 hunk；summary 不能提到没有对应 structured issue 的具体 bug；不要把没有 diff 证据的问题强行标成 critical。

第二类是 user payload，也就是本次任务输入：repo path、diff mode、diff text、changed file snippets、project structure、context state、constraints、selected context parts、哪些部分被截断等。它承担“本轮要审什么”的职责。

第三类是 tool schemas，包括只读工具和 `submit_review` 伪工具。工具 schema 让模型知道它可以按需读取上下文，也让最终输出必须走结构化工具调用，而不是自由文本。

**追问 12.2：tool response 是怎么进入下一轮 prompt 的？**

**参考回答：**

工具执行后，结果会被转换成 `tool_feedback` 条目，而不是无控制地拼接到对话末尾。每个条目会带上 iteration、tool name、arguments 摘要、result payload、是否 synthetic context、是否失败、是否 truncated 等信息。

下一轮模型调用时，`InferenceEngine._build_tool_feedback_messages()` 会把这些条目转成额外 message，例如“iteration 0 的 read_file 返回了某文件内容”或“prefetched_tool_context 包含某 changed file 的上下文”。如果工具失败，还会生成 failure guidance，提醒模型不要假设工具成功，而要基于失败信息降级推理。

这样做的价值是可控：模型看到的是结构化、可压缩、可审计的工具观察，而不是无限增长的自然语言聊天历史。

**追问 12.3：多轮循环中前一轮上下文保留哪些，哪些会被压缩？**

**参考回答：**

保留优先级大致是：任务目标和约束、diff、changed file context、项目结构、最近几轮有效工具结果、submit validation 状态和预算状态。因为这些直接影响最终 finding 是否有证据、是否能落到 changed line、是否还能继续请求工具。

较旧的工具结果不会一直全文保留。项目里有 folded feedback summary：旧轮次工具结果会被折叠成 `prior_tool_results_summary`，保留“执行过什么、结果大意是什么、不要重复请求同样参数”这些信息。这样既避免模型重复查同一文件，也避免上下文被旧工具输出挤爆。

另外，文件级上下文也会按优先级截断。diff 和变更文件片段优先，低优先级的大文件、目录结构尾部、过长工具输出会被截断或摘要化。

**追问 12.4：上下文爆了怎么办？**

**参考回答：**

我会分输入预算和运行预算两层回答。

输入预算上，`ContextBuilder` 会估算 context parts 的 token 数，按优先级做 `truncate_context`。如果启用了 summary compaction，会对溢出的 parts 调用 `ContextCompressor` 生成短摘要，再尝试把摘要放回 prompt。工具输出也会做预览、截断和 folded summary，避免一次 `read_file` 或 grep 结果把窗口吃满。

运行预算上，系统累计模型 token 使用量，区分 soft cap 和 hard cap。接近预算时可以触发 pre-budget submit，要求模型基于已有证据尽快结构化收口；如果循环结束仍没有 draft review，会走 force submit；如果 hard cap 已经触发，就返回 partial result，并在 event log / run summary 记录 `budget_state`、stop reason 和 placeholder 状态。这样失败也是可解释的。

**追问 12.5：这个项目需不需要长期记忆？现在是怎么做的？**

**参考回答：**

对 MergeWarden 的 review 主链路，我不会引入跨 PR 的自由长期记忆。原因是 PR review 必须以当前 diff 和当前 checkout 为准，长期记忆很容易带入过期架构、旧 bug、旧路径或旧团队偏好，反而污染判断。

当前设计更接近 run-scoped memory 和 eval memory。run-scoped memory 包括 `ContextState`、`tool_feedback`、event log 和 run summary，用来支撑本次多轮推理和事后复盘；eval memory 包括 golden fixtures、reports、diagnostics、trend，用来改进系统质量，但不直接作为某次 review 的事实输入。

如果未来要做长期记忆，我会把它限制成显式、可版本化、可审计的项目规则库，例如团队 coding standards、风险清单、已知模块边界，而不是让模型自动记住历史对话。并且每条长期记忆都要有来源、更新时间和适用范围，进入 prompt 前还要和当前 repo/version 做匹配。

## 13. 为什么 inline comment 只限制在 changed lines？

**参考回答：**

GitHub review comment 通常需要挂在 PR diff 的变更行上。如果模型把 comment 定位到未变更文件或旧行，很容易发布失败，或者造成 reviewer 困惑。

所以 MergeWarden 把 changed-line metadata 作为发布前过滤条件：能落到变更行的 finding 生成 inline comment；不能落到变更行但仍有价值的 finding 放到 soft-check summary。这样既保留信息，又避免错误定位。

## 14. `github_adapter.py` 和 `github_publisher.py` 为什么分开？

**参考回答：**

这是纯转换层和副作用层的分离。`github_adapter.py` 把 `ReviewResponse` 加 changed-line metadata 转成 advisory payload、inline candidates、summary-only issues 和 fingerprint，不做网络调用。

`github_publisher.py` 才负责 GitHub API 交互，例如创建 check run、创建/更新 review comment、处理 stale comment。这样测试更容易，dry-run 更可信，也避免业务转换逻辑和网络副作用耦合。

## 15. fingerprint 在评论生命周期里做什么？

**参考回答：**

fingerprint 是针对 finding 的稳定标识，用来判断同一个问题在后续运行里是否仍然存在。它通常基于 path、line、severity、evidence/suggestion 等稳定信息生成。

有了 fingerprint，MergeWarden 可以更新已有的 bot comment，而不是每次重复发新评论；也可以把本轮不存在的旧 finding 标记为 stale。这样不会污染 PR 讨论区，也不会误改人类 reviewer 的评论。

## 16. 为什么 GitHub publish 默认 dry-run？

**参考回答：**

因为 GitHub 发布是有外部副作用的操作。默认 dry-run 可以先生成完整发布计划和 payload，让开发者检查将要创建哪些 check、哪些 comment、哪些 stale update，再决定是否 `--publish`。

这也让 CI 和本地调试更安全。尤其是在 token 权限、fork PR、changed-line mapping 还不确定时，dry-run 能提供证据而不是直接写入 GitHub。

## 17. GitHub Actions workflow 的完整路径是什么？

**参考回答：**

路径是：checkout PR head -> fetch base -> 计算 merge base -> 生成 `pr.diff` -> 生成 `changed_lines.json` -> 运行 `cli.py review . --diff --output-json --summary-json` -> dry-run publish -> same-repo PR 上执行真实 publish -> 上传 artifacts。

关键 artifact 包括 `review_response.json`、`run_summary.json`、`changed_lines.json`、`pr.diff`、`publish_dry_run.json`、`publish_result.json` 和 `.mergewarden/logs/*.jsonl`。

## 18. fork PR 为什么要特殊处理？

**参考回答：**

fork PR 通常没有同等写权限，GitHub token 权限也更受限。对不可信外部代码直接执行高权限 publish 或 execute 工具也有安全风险。

所以 MergeWarden 的 workflow 对 fork PR 保留 dry-run artifact，不直接发布 review comments。same-repo PR 才在 `GITHUB_TOKEN` 有 `checks: write` 和 `pull-requests: write` 时执行 publish。

## 19. 为什么需要 `run_summary`？

**参考回答：**

event log 很细，但面试官、开发者或 CI 排障时不一定想直接读 JSONL。`run_summary` 是观测性聚合层，提取 event-log status、model/token、tool-call count、budget/stop state、submit validation errors、artifact paths 和 publish status。

它让一次 agent run 从“黑盒调用”变成可复盘的运行记录。出现空输出、placeholder summary、超预算、provider 失败时，可以先看 summary 再决定是否深入 JSONL。

## 20. 如果 CI 里出现 Avg Tokens = 0 和 placeholder summary，你怎么排查？

**参考回答：**

我不会先调 prompt 或降低 eval threshold，而是先判断 provider 调用是否真的发生。`Avg Tokens = 0` 加 placeholder summary 往往说明模型请求在进入有效推理前就失败了，比如 `auth_failed` 或 401。

排查顺序是：看 `.mergewarden/logs/*.jsonl`、`ci_report_diagnostics.json`、`ci_report_run_summaries.json`，确认 `OPENAI_API_KEY`、`OPENAI_BASE_URL`、`MODEL_NAME` 是否匹配。只有确认模型产生了有效输出后，才讨论质量或评分问题。

## 21. 你如何评估 MergeWarden 的 review 质量？

**参考回答：**

我会拆成几类指标：schema validity、positive fixture hit rate、negative fixture false-positive rate、location accuracy、evidence quality、suggestion usefulness、confidence calibration 和 human acceptability。

当前 MVP+ 数值 gate 是 schema validity 1.0、hit rate >= 0.6、false-positive rate <= 0.5。最新记录的 R14 baseline 是 hit rate 0.75、false-positive 0.0，但我会明确说明 golden suite 只有 4 个正样本和 2 个负样本，不能过度外推。

## 22. 为什么不能只看 hit rate？

**参考回答：**

因为 review agent 很容易通过“多报问题”提高 hit rate，但这会伤害真实使用体验。PR review 场景下误报成本很高，会增加 reviewer 噪音，降低团队信任。

所以必须同时看 false positive、location 是否能落到 changed lines、evidence 是否具体、suggestion 是否可执行，以及是否有明确的“不确定/证据不足”表达。一个好的 review agent 应该宁可保守，也不能为了命中率制造噪音。

## 23. golden fixtures 为什么要用 PR diff + repo snapshot？

**参考回答：**

只保存 diff 不够，因为很多 review 需要读取未变更的上下文，例如函数定义、调用链、测试约束和相邻模块。如果没有 repo snapshot，模型可能缺少必要证据。

但只保存 repo snapshot 也不够，因为 PR review 的目标是变更本身。因此 golden fixture 应该是 PR diff 加对应 checkout sha 的 workspace snapshot。MergeWarden 还做了 diff added lines 和 restored workspace 的一致性校验，避免 stale fixture 污染评测。

## 24. 你如何解释 `debug` 能力的边界？

**参考回答：**

`debug` 不是自动修复 agent，而是 failure triage：解释失败原因、收集证据、给出验证步骤和最小修复建议。它可以建议命令或 patch，但不应该把 patch success 作为唯一核心指标。

这和 PR review 主路径不同。Review 是核心价值路径，debug 是辅助能力，用来帮助理解 CI 失败或运行错误。产品上我会避免让 debug 漂移成另一个大型 coding agent。

## 25. 为什么 FastAPI 只是同步薄层？

**参考回答：**

MVP+ 阶段最重要的是复用 CLI 的核心契约，而不是过早引入数据库、队列、worker 和多租户平台。同步 FastAPI 薄层只负责 request validation、JSON error、orchestrator dispatch 和 response model。

这样能快速提供 HTTP 集成面，同时不改变核心 agent loop。真正需要 webhook、长任务和持久化状态时，再进入 later phase 引入 async job queue 和 storage。

## 26. 你如何处理模型输出格式不稳定？

**参考回答：**

首先通过 tool calling 和 Pydantic schema 降低格式漂移，而不是依赖自由文本。其次记录 `submit_review_validation_error`，在必要时触发 validation repair retry，让模型修正不合法 payload。

如果最终仍不合法，系统应该降级为结构化错误或 placeholder，并在 event log / run summary 里记录原因。关键是不能静默吞掉错误，让用户误以为“没有发现问题”。

## 27. 你如何控制 token budget？

**参考回答：**

从输入侧控制：diff-first、按需读取文件、上下文优先级、截断和摘要。运行侧控制：max iterations、prompt input budget、token budget、工具输出截断。输出侧控制：结构化 issue 列表，不让模型生成无限长解释。

预算控制不只是省钱，也影响稳定性。上下文越杂，模型越容易被无关代码干扰；工具输出越长，越容易挤掉真正重要的 diff 和 evidence。

## 28. 你如何设计 severity？

**参考回答：**

severity 应该和合并风险挂钩，而不是和模型语气挂钩。critical 表示可能导致严重正确性、安全或数据损坏问题；warning 表示真实风险但需要人工判断；info/style 更适合低优先级建议。

实际运行中，severity 不是靠后处理随便改出来的，而是先在 `ReviewIssue` 的 schema 和 `submit_review` 工具 schema 里限定可选枚举，再通过 review prompt 要求模型按“影响面 + 可证据化风险 + 合并后果”分类。模型提交结构化结果后，系统会用 Pydantic 校验 severity 是否在允许集合内；如果不合法，会记录 validation error 或触发修复路径，而不是把任意字符串当成有效等级。

分类依据主要来自 issue 的 evidence 和 suggestion：如果证据指向明确的正确性回归、安全问题、数据损坏或会直接破坏主流程，才应该是 critical；如果是有真实代码依据但还需要人工确认的行为风险、兼容性风险或测试缺口，通常是 warning；如果只是可维护性、可读性、边界说明或风格建议，就应降到 info/style。eval 侧也会把 severity 作为匹配和误报判断的一部分，避免模型用过高等级包装低价值建议。

## 29. 如果模型发现的问题不在 changed line 上，怎么办？

**参考回答：**

如果问题的根因或证据在未变更代码，但触发点来自 PR diff，可以把 changed line 作为 comment location，把未变更代码作为 evidence。若完全无法落到 changed line，就不要发 inline comment，而是放到 summary-only。

这保证 GitHub comment 可发布，也保持 review 语义清晰：PR comment 应该围绕这次变更，而不是把全仓库历史问题都拿出来评论。

## 30. 你会如何向面试官证明项目不是 prompt demo？

**参考回答：**

我会展示四类证据：

1. 工程结构：CLI、FastAPI、orchestrator、tools、security、models、eval、integrations 分层明确。
2. 行为闭环：多轮 tool feedback、submit_review、force submit、run summary。
3. 安全边界：ToolSafety、execute allowlist、sandbox、CI execute refusal。
4. 质量闭环：golden fixtures、eval gate、diagnostics、GitHub advisory artifacts。

这些都是 prompt 之外的 agent runtime 和产品化工程。

## 31. 你如何设计回归测试？

**参考回答：**

我会分三层：

单元测试覆盖 schema、location parsing、tool specs、GitHub adapter、publisher lifecycle。集成测试覆盖 CLI/API、agent loop、run summary、GitHub advisory workflow YAML。评测测试覆盖 golden fixtures、match logic、false-positive logic、fixture/workspace 一致性。

此外，LLM 行为本身不能完全靠普通单元测试稳定复现，所以需要固定 fixtures、保存 reports、做 trend comparison，并把 provider/auth failure 和模型质量 failure 区分开。

## 32. eval gate 为什么后来不放在主 CI 里？

**参考回答：**

主 CI 应该稳定、快速、低成本，适合跑 ruff、mypy、pytest。golden eval 依赖模型 provider、API key、网络和成本，容易因为外部环境导致 CI 不稳定。

所以更合理的方式是把 golden eval 作为定期或手动质量验证，或者在 release 前运行，而不是每个 PR 的硬 CI。这样不会因为 provider 401 或模型波动阻塞普通代码变更。

## 33. 如果要把它从 MVP+ 推到生产，你的前三个优先级是什么？

**参考回答：**

第一，扩大 reviewed golden suite，到至少 20-30 个真实 PR，覆盖不同语言、变更类型和负样本。第二，做 GitHub Actions 实战验证，积累真实 artifact、权限、fork PR、comment lifecycle 和 provider failure 数据。第三，增加 durable storage / job tracking，让 run history、重跑、审计和团队级观测可用。

我不会优先做 UI 或自动修复，因为当前最大风险是质量证据和运营稳定性，而不是展示层。

## 34. 你会如何做 provider matrix？

**参考回答：**

我会把同一批 golden fixtures 在不同模型、base_url、temperature、iteration budget 下运行，记录 schema validity、hit rate、false-positive、latency、tokens 和 per-fixture miss history。

目标不是简单选最高分模型，而是找到稳定、成本、速度和误报之间的平衡。对 PR review 来说，一个稍慢但低误报、定位稳定的模型可能比高召回但噪音大的模型更适合。

## 35. 为什么要保留 Docker execute backend？

**参考回答：**

Docker backend 提供更强的隔离路径，适合本地 smoke test 或未来更复杂的 debug 场景。即使 MVP+ 默认不在 CI 中跑 Docker execute，它仍然是 execute 类工具安全演进的重要基础。

它也体现了系统设计的可插拔性：execute 不直接绑定 subprocess，而是通过 backend 抽象实现。后续可以接入更严格的容器策略、资源限制或远程 sandbox。

## 36. 你如何处理 reviewer 对“LLM 幻觉”的质疑？

**参考回答：**

我会承认 LLM review 有幻觉风险，所以系统设计不能只依赖模型自信。MergeWarden 用 diff-first、工具读上下文、evidence 字段、changed-line 限制、confidence、schema validation、eval false-positive gate 和 advisory-only 产品边界来降低风险。

更重要的是，输出必须可复盘。每个 finding 应该能指向具体代码证据和建议；没有证据的结论不应该升级为高 severity，更不应该作为硬合并门禁。

## 37. 这个项目里最能体现产品判断的地方是什么？

**参考回答：**

最重要的是没有把 MergeWarden 做成“自动合并裁判”。PR review 是高信任场景，模型适合做辅助判断而不是最终裁决。因此产品边界选择了 advisory-only、soft check、summary-only fallback 和人类 reviewer 可审计证据。

另一个产品判断是 Phase 2 顺序：先做本地观测和 GitHub advisory，再考虑 comment lifecycle、async storage、webhook。这个顺序优先解决可信闭环，而不是过早平台化。

## 38. 如果面试官问“这个项目最大的不足是什么”，你怎么回答？

**参考回答：**

我会直接说：当前最大不足是生产级质量证据不足，而不是核心功能缺失。MVP+ 的工程闭环和小规模 golden eval 已经完成，但 reviewed fixtures 数量仍小，真实 GitHub 长期运行样本有限，provider matrix 和成本/稳定性数据还不够。

这也是下一阶段我会优先补的方向：扩大评测集、做真实 PR workflow 验证、完善 human acceptability 和运行历史，而不是盲目增加自动修复功能。

## 39. 如果让你重构一个模块，你会先动哪里？

**参考回答：**

我会优先重构 eval / diagnostics 的报告组织，而不是 agent loop。agent loop 是核心路径，已经有较多测试，贸然重构风险高。eval diagnostics 更适合整理成更清晰的 report model、provider matrix summary 和 per-fixture history。

这样能提高项目可信度，也能帮助后续 prompt、model 和 policy 调优。对 agent 项目来说，评测和观测能力往往比继续堆功能更能提升长期质量。

## 40. 你希望面试官从这个项目里看到你哪些能力？

**参考回答：**

我希望面试官看到三类能力：

第一是 agent runtime 工程能力：能设计多轮编排、工具调用、权限边界、结构化输出和失败恢复。第二是 LLM 产品判断：知道什么时候 advisory、什么时候 hard gate，知道如何控制误报和用户信任。第三是质量闭环意识：不是只写 prompt，而是用 eval、run summary、artifact、CI 和 GitHub integration 去证明系统行为。

如果岗位偏 Agent 开发，我会重点讲 tool calling、安全和 orchestration；如果岗位偏 LLM 产品，我会重点讲 PR review 场景、advisory-only 边界、评测指标和 rollout 策略。
