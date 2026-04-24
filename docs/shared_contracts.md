# 共同确认的接口与协议

本文档列出 **Analyzer Agent** 与 **Integration Agent**（及编排层）在实现前或变更时需要 **对齐并共同维护** 的契约。重大变更应先更新本文档或关联 Issue，再改代码。

**相关文档**：[README](../README.md)、[CONTRIBUTING](../CONTRIBUTING.md)、[architecture](./architecture.md)、项目规划 [project_plan.md](./project_plan.md)、execute 工具专项 [execute_tools_design.md](./execute_tools_design.md)、编排与安全契约 [cli_tools_orchestrator_contract.md](./cli_tools_orchestrator_contract.md)、Agent 约束 [agent.md](../agent.md)。

---

## 1. 分层责任边界

| 区域 | 主要职责 | 典型路径 |
|------|----------|----------|
| 入口 | 解析用户输入、展示结果 | `cli.py` |
| 编排 | 5 阶段循环、串联状态与工具 | `src/orchestrator/` |
| 分析 | 推理、格式化报告、状态模型定义 | `src/analyzer/` |
| 工具 | 注册、Schema、具体读写与执行 | `src/tools/`、`src/security/` |
| 模型 | LLM 调用与 Provider 抽象 | `src/models/` |
| 配置 | 环境变量与全局设置 | `src/config.py` |

**共同原则**：模块间交互以 **Pydantic 模型** 与 **明确的工具契约** 为准；避免在未协商的情况下修改对方依赖的字段名或语义。

---

## 2. 工具层协议（Tool Calling）

实现参考：`src/tools/base.py`。

### 2.1 安全分级 `ToolSafety`

| 取值 | 含义 | 执行策略（约定） |
|------|------|------------------|
| `readonly` | 无副作用读操作 | 可并发；无需用户确认 |
| `write` | 写文件或改仓库 | 串行；需确认或隔离策略 |
| `execute` | 运行命令 | 沙箱、超时、工作目录限制；见 `src/security/` |

新增工具时必须选定一级，并在 PR 中说明理由。

**Execute 工具清单与可见性**：当前实现了 `run_command`（通用，首词白名单 + `shlex` argv 化 + `shell=False`）与 `run_tests`（`pytest`/`unittest` 便捷封装）。两者均走同一 `src/security/exec_policy.py` + `src/security/backends.py` 管道。**仅 Debug 模式**通过 `create_default_registry(include_execute=True)` 暴露给模型；Review 模式不暴露 execute 工具。策略违规统一抛 `CommandNotAllowedError(ToolError)`，经高危门控拒绝/未通过 policy 时均在 `ContextState.errors` 中以 `category="security"` 记录。

### 2.2 工具规格 `ToolSpec`

- `name`：全局唯一，与注册表键一致。
- `description`：供模型与用户理解的说明。
- `parameters`：JSON-Schema 风格字典，描述调用参数（与 `execute(**kwargs)` 对齐）。
- `safety`：上述 `ToolSafety`。

### 2.3 抽象基类 `BaseTool`

- `spec() -> ToolSpec`：必须实现。
- `async execute(**kwargs) -> Any`：必须实现；返回值应可被序列化进日志/状态（避免不可 JSON 的对象除非约定）。
- `is_enabled() -> bool`：默认 `True`；环境不满足时可禁用。
- `is_concurrency_safe() -> bool`：默认与 `safety == READONLY` 一致；若只读工具仍不可并发，可覆盖并说明。

### 2.4 `ToolRegistry`

- `register` / `get` / `list_specs` 为编排层与推理侧获取「当前可用工具列表」的 **唯一推荐入口**。
- 新增或重命名工具时，需同步更新评测与文档中的工具清单（如有）。

---

## 3. 会话状态协议（Context State）

实现参考：`src/analyzer/context_state.py`。

### 3.1 `ContextState`

单次运行（review 或 debug）共享一份实例，由编排层创建并传入各阶段。

| 字段 | 说明 |
|------|------|
| `goal` | 当前任务目标 |
| `constraints` | 活跃约束（如「仅看 diff」「禁止写盘」） |
| `decisions` | `DecisionStep` 列表，决策历史 |
| `current_files` | 当前关注文件路径 |
| `errors` | `ErrorDetail` 列表 |

### 3.2 `DecisionStep`

| 字段 | 说明 |
|------|------|
| `phase` | 产生该记录的 agent 阶段标识 |
| `action` | 决定或执行的内容摘要 |
| `result` | 结果或观察 |

**约定**：`phase` 的取值集合应与 5 阶段命名一致（或维护枚举），便于日志与评测解析。

### 3.3 `ErrorDetail`

| 字段 | 说明 |
|------|------|
| `file` | 相关文件路径，可为空 |
| `line` | 行号，可选 |
| `message` | 错误描述 |
| `category` | 如 `syntax` \| `runtime` \| `logic` \| `style` \| `security` \| `unknown` |

扩展 `category` 枚举时需双方同意，并更新评测期望（如有）。

---

## 4. Review 结构化输出协议

实现参考：`src/analyzer/output_formatter.py`。

### 4.1 `Severity`

`critical` | `warning` | `info` | `style`（枚举值与 JSON 序列化一致）。

### 4.2 `ReviewIssue`（单条发现）

| 字段 | 类型 | 说明 |
|------|------|------|
| `severity` | `Severity` | 严重级别 |
| `location` | `str` | 如 `file:line` 或 diff hunk 引用 |
| `evidence` | `str` | 代码片段或观察依据 |
| `suggestion` | `str` | 修复或行动建议 |
| `confidence` | `float` | `0.0`–`1.0`，模型置信度 |

### 4.3 `ReviewReport`（单次 review 汇总）

| 字段 | 类型 | 说明 |
|------|------|------|
| `issues` | `list[ReviewIssue]` | 问题列表 |
| `summary` | `str` | 可选总述 |

CLI、未来 API 与 CI 校验应只依赖上述稳定字段；**增删字段** 需走共同评审。

---

## 5. Debug 结构化输出协议（已定稿并已落地）

实现参考：`src/analyzer/schemas.py`（`DebugStep`、`SuggestedCommand`、`DebugResponse`）。

产品目标见 [README](../README.md)（假设 → 验证步骤 → 建议补丁等）。字段与 [cli_tools_orchestrator_contract.md](./cli_tools_orchestrator_contract.md) §6 一致。

- `DebugStep`：`title`、`detail`、`location`、`evidence`、`confidence`（与 `ReviewIssue` 在 location / evidence / confidence 语义上对齐）。
- `SuggestedCommand`：`command`、`rationale`、`risk`（`low` | `medium` | `high`）；仅表示建议，不代表已执行。
- `DebugResponse`：`run_id`、`summary`、`hypotheses`、`steps`、`suggested_commands`、`suggested_patch`、`context`。

**变更约定**：增删字段需双方评审并同步契约文档。

---

## 6. 配置与环境变量协议

实现参考：`src/config.py`。

| 变量 / 字段 | 含义 | 备注 |
|-------------|------|------|
| `OPENAI_API_KEY` | API 密钥 | 勿提交仓库 |
| `OPENAI_BASE_URL` | 兼容 API 基地址 | 默认 OpenAI 官方 |
| `MODEL_NAME` | 默认模型名 | 变更时评测基线可能需重跑 |
| `LOG_LEVEL` | 日志级别 | 与可观测性约定一致 |
| `REVIEW_MAX_ITERATIONS` | Review 模式最大循环轮次 | 默认 `1`，对应 `Settings.review_max_iterations` |
| `DEBUG_MAX_ITERATIONS` | Debug 模式最大循环轮次 | 默认 `3`，对应 `Settings.debug_max_iterations` |
| `TOKEN_BUDGET` | 单次运行累计 token 用量上限（用于终止判定） | 默认 `12000`，对应 `Settings.token_budget` |
| `PROMPT_INPUT_TOKEN_BUDGET` | 首轮用户消息中 **可截断上下文块**（meta、diff hunk、文件、结构等）的估算 token 上限 | 默认 `32000`，对应 `Settings.prompt_input_token_budget`；与 `TOKEN_BUDGET` 语义分离，见 [analyzer_dev_plan.md](./analyzer_dev_plan.md) §2.3 |
| `EVENT_LOG_DIR` | 事件 JSONL 日志目录 | 默认 `.cr-debug-agent/logs`；相对路径时相对于 `repo_path` 解析，见编排层实现 |
| `PERMISSION_MODE` | 权限模式（`default` \| `plan`） | 默认 `default`；`plan` 模式禁止执行工具，仅生成计划与结构化输出 |
| `CI` | 常见 CI 环境变量 | 设为 `true`/`1`/`yes` 时，编排层对 `write`/`execute` 工具默认拒绝（与 [cli_tools_orchestrator_contract.md](./cli_tools_orchestrator_contract.md) §11 一致） |
| `EXECUTE_ENABLED` | execute 类工具全局开关 | 默认 `true`；置 `false` 时即便 Debug 模式也不注册 `run_command` / `run_tests` |
| `EXECUTE_BACKEND` | execute 工具后端实现 | `subprocess`（默认）/ `docker`（通过 `docker run --rm` 在容器内执行） |
| `EXECUTE_ALLOWED_COMMANDS` | `run_command` 首词白名单 | 逗号分隔；默认 `python,pytest,pip,node,npm,ruff,mypy,git`；`git` 子命令再限于 `status/diff/log/show/rev-parse` |
| `EXECUTE_DEFAULT_TIMEOUT_MS` | execute 工具默认超时 | 默认 `30000`，可由工具入参覆盖 |
| `EXECUTE_MAX_OUTPUT_BYTES` | stdout/stderr 各自字节上限 | 默认 `65536`；超限时截断并置 `SandboxResult.*_truncated=True` |
| `EXECUTE_DOCKER_IMAGE` | Docker execute 后端镜像 | 默认 `python:3.11-slim`；建议按项目依赖改成预构建镜像 |
| `EXECUTE_DOCKER_WORKDIR` | Docker execute 后端容器工作目录 | 默认 `/workspace`；宿主机当前 `cwd` 会 bind mount 到这里 |
| `EXECUTE_DOCKER_NETWORK_DISABLED` | Docker execute 后端是否禁网 | 默认 `true`；为 true 时追加 `--network none` |

新增全局配置项时，应更新 `Settings`、`.env.example`（如有）及本文档或 README。

---

## 7. 单次运行（Run）与可观测性

与 [architecture](./architecture.md) 中 Observability 一致，**建议在实现编排层时** 共同确认：

| 项目 | 约定 |
|------|------|
| `run_id` | 每次 CLI/调用生成的唯一标识，写入日志与可选 artifact |
| 记录内容 | 工具调用序列、关键中间结果、耗时、token 用量 |
| 输入快照 | 是否落盘脱敏后的 prompt/输出以便复盘（路径与保留策略） |

具体字段若在代码中以 `RunContext` 等模型出现，应在该类型旁或本文档交叉引用。

---

## 8. Prompt 与 JSON Schema

以下由 **双方共同设计、变更需评审**：

- 系统提示词与分析/调试任务模板；
- 面向模型的 **工具列表** 与 **输出格式** 说明（须与第 2、4、5 节一致）；
- 任何「强制 JSON」或 function-calling 的 schema 版本。

避免仅在一侧仓库私密修改导致线上与本地行为分叉。

---

## 9. 异常与降级

- 工具超时、命令失败、模型错误时，应能返回 **结构化错误信息**（可进入 `ContextState.errors` 或统一错误模型），并尽可能输出 **部分有用结论**（见规划文档中的 Graceful Degradation）。
- 自定义异常类命名与继承层次宜在 `src/` 内集中约定，避免裸抛 `Exception`（见 CONTRIBUTING）。

---

## 10. 变更流程（建议）

1. 在 Issue 中说明动机与兼容性影响。  
2. 更新本文档或 `architecture.md` 中的契约说明。  
3. 实现代码与测试；涉及输出 schema 时同步 `eval/fixtures/` 或评测脚本。  
4. PR 中 `@` 对方角色 review，合并前至少一人审阅契约相关改动。

---

## 修订记录

| 日期 | 说明 |
|------|------|
| 2026-04-09 | 初稿：工具、状态、Review 输出、配置、观测与协作流程 |
| 2026-04-12 | Debug 输出协议定稿落地；补充编排相关环境变量（轮次、token、事件日志、CI 与高危工具） |
| 2026-04-17 | execute 工具硬化：argv + 首词白名单、pluggable backend、输出截断、Review 模式不暴露 execute 工具；新增 `EXECUTE_*` 环境变量 |
| 2026-04-23 | Docker execute 后端落地：`docker run --rm` + bind mount `cwd` + 可配置镜像/工作目录/禁网 |
