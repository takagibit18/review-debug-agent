# MVP+ 路线图与相对当前 MVP 的优化项

> 本文档记录：**当前 MVP 已覆盖什么**、**MVP+ 阶段希望加强什么**（相对现状的增量），便于排期与对齐口径。  
> 与 `[project_plan.md](project_plan.md)` 中的路线互补：前者偏整体规划，本文偏 **MVP 之后的具体优化清单**。

---

## 1. 当前 MVP（基线）

与仓库现状一致，大致包括：

- CLI + 五阶段编排；Review / Debug 结构化输出（`severity`、`location`、`evidence`、`suggestion` 等）。
- 只读工具与工作区约束；可复现的最小运行路径与基础工程骨架。
- 评测与 Golden 管线（`eval/`）作为 **质量与回归的配套能力**，而非产品本体。
- 成功标准侧重：**可跑通、可复现、行为与契约一致**（见 `project_plan.md` §1.4）；评测集用于约束迭代，不单独定义「产品完成度」。

---

## 2. MVP+ 指什么

**MVP+**：在 MVP 可演示、可交付的基础上，系统性提升 **可靠性、工程化、可运维性、体验与安全边界**；评测与指标是其中 **支撑迭代与门禁** 的一环，与编排、工具、契约、部署等 **并列**，不作为 MVP+ 的唯一叙事主线。

---

## 3. 相对当前 MVP 的优化项（待办/方向）

下列为增量清单，**不承诺一次性做完**，实施时拆 Issue。条目按 **能力域** 归纳，力求各模块篇幅与深度大致相当；**与其他文档的对照**见 §3.2，**上下文实现摘要**见 §3.3。

### 3.1 各能力域总览


| 能力域                | MVP+ 增量方向（摘要）                                                                                                                                                               |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **编排与资源**          | Token 预算与迭代轮次；`stop:*` 与 `errors` 的展示优先级（例如与 `budget_exhausted` 同时成立时）可优化，避免误导；**输入侧**长上下文已由 Analyzer 侧预算 + 混合摘要承接（见 §3.3），编排层仍可关注展示与终止语义一致性。                                                                        |
| **Analyzer / 上下文** | **已落地**：优先级截断 + **溢出块** LLM 摘要（`ContextCompressor`、`truncate_with_summary`）；环境变量 `CONTEXT_SUMMARY_ENABLED`、`SUMMARY_MAX_TOKENS_PER_PART`；用户 JSON 中 `truncated.summarized` 与摘要内容 `[SUMMARIZED]` 前缀。**仍待办**：`ContextBuilder.load_diff` 与「工作区全量 diff」语义对齐；连续工具失败 / 空结果的降级路径与 `analyzer_dev_plan` 一致细化。              |
| **生产路径与提示**        | 非稀疏场景下：`AgentOrchestrator.analyze` 接入 `**project_structure`、按需 `file_contents`**；工具目标不存在时的回退提示；`SYSTEM_PROMPT_REVIEW` 与沙箱 `repo_path`、工具路径语义一致；生产侧「先探明目录」等约束与评测侧已部分缓解的策略对齐。 |
| **路径与沙箱**          | 相对路径相对 workspace 解析、上下文写明工作区根（已部分实现）；稀疏沙箱与完整仓库行为对齐，避免「评测能过、生产行为漂移」。                                                                                                         |
| **工具与安全**          | 执行型工具的沙箱、超时、`cwd`/工作区与确认策略持续与架构契约一致；高危操作门控与可观测性。                                                                                                                            |
| **契约与输出**          | Review 的 `location` 语义清晰化；`submit_review` / `ReviewReport` 校验失败可观测（日志与降级）；协议演进时同步 CLI 与配套文档。                                                                                |
| **观测与调试**          | 结构化日志、失败原因（如 `submit_review` 校验失败）更易排查；事件日志 phase 粒度可按需补全。                                                                                                                  |
| **交付与 CI**         | Docker 一键 demo；CI 与本地流水线一致（与 `project_plan` 中 W2/W3 等里程碑对齐）。                                                                                                                |
| **可选服务层**          | FastAPI 薄层（`project_plan` §2 方案 B），与现有 CLI 能力等价、可观测性不降级。                                                                                                                    |
| **评测与回归**          | 维持可复现的主指标与报告形态；Golden/爬虫侧标注质量与「可证伪缺陷」约束；**指标扩展、辅助维度与人工复核流程** 见 `eval/README.md`，本路线图不展开细则。                                                                                  |


### 3.2 与其他文档的对照（简要）

以下从各专项文档梳理，**与 §3.1 可能重叠**，仅作索引；具体条款以各文档为准。


| 文档                                             | 与 MVP+ 相关的典型章节 / 备注                      |
| ---------------------------------------------- | ---------------------------------------- |
| `[project_plan.md](project_plan.md)`           | §2 路线（FastAPI、Docker）、§3.4 评测策略、§4.2 里程碑 |
| `[analyzer_dev_plan.md](analyzer_dev_plan.md)` | §1.2 差距项、§2.3 上下文截断与摘要、§2.4 终止与降级        |
| `[error_log.md](error_log.md)`                 | 稀疏沙箱、review pipeline、生产路径待办              |
| `[architecture.md](architecture.md)`           | 分层、工具安全、可观测性、上下文预算                       |
| `[eval/README.md](../eval/README.md)`          | 指标定义、黄金集策略、人工可接受度                        |
| `[shared_contracts.md](shared_contracts.md)`   | Review/Debug 字段与配置；协议变更时联动实现与文档          |

### 3.3 上下文窗口管理（当前实现摘要）

相对纯优先级截断，上下文已升级为 **两层混合策略**（详见 `[analyzer_dev_plan.md](analyzer_dev_plan.md)` §2.3）：

| 层级 | 行为 | 说明 |
|------|------|------|
| 第一层 | `ContextBuilder.truncate_context` | 按 `context_priority` 全序贪心装入，预算由 `PROMPT_INPUT_TOKEN_BUDGET`（`Settings.prompt_input_token_budget`）约束可截断块。 |
| 第二层 | `truncate_with_summary` + `ContextCompressor` | 仅当第一层溢出、存在被丢弃块时，对丢弃块调用与主分析 **同一模型** 生成摘要，再二次装入预算；可通过 `CONTEXT_SUMMARY_ENABLED=false` 关闭，仅保留截断。 |
| 可观测性 | 用户 JSON `truncated` | 除 `any` / `diff_hunks` / `files` / `error_log` / `structure` 外，增加 **`summarized`**（被摘要的原始块标识）；摘要正文带 **`[SUMMARIZED]`** 前缀。 |

**未纳入本阶段**（仍属 MVP+ 可选增量）：多轮 `tool` 反馈链路的「微压缩」式占位替换、413 应急压缩等；与对话型产品不同，当前以单次 prepare 侧预算为主。

---

## 4. 评测说明

评测用于 **回归与门禁**：主指标（如基于 `location_pattern` 与 severity 的 `hit_rate`）保持简单、可自动化；若需更细粒度语义一致性，可采用辅助指标或人工抽样，**口径与候选方案以 `[eval/README.md](../eval/README.md)` 为准**。本路线图将评测视为质量配套，不在此重复指标表与实现细节。

---

## 5. 维护

- 本文档随 MVP+ 讨论更新；重大口径变更时同步更新 `eval/README.md` 与 `shared_contracts.md`（若涉及）。**文档交叉索引**见 §3.2；**上下文实现细节**见 §3.3 与 `analyzer_dev_plan.md` §2.3。

