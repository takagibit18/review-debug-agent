# CLI / Tools / Orchestrator 接口契约

> **目的**：固定 `cli.py`、`src/tools/`、`src/orchestrator/` 三层调用边界，作为实现与联调的强约束。  
> **状态**：生效中的基线契约；违背本契约的改动必须先改文档再改代码。

**相关文档**：[project_plan.md](./project_plan.md)、[architecture.md](./architecture.md)。

---

## 1. 目标与链路

端到端链路固定为：

`CLI 输入 → Orchestrator 请求模型 → 工具调度 / 模型推理 → 结构化输出 → CLI 展示`

本契约固定以下事项：

1. `cli.py` 只能调用编排层公开入口。
2. 编排层输入输出必须是 Pydantic 模型。
3. 工具层必须经 `ToolRegistry` 暴露给编排层。
4. Analyzer、Model、Security、可观测性边界必须与本文档一致。

---

## 2. 分层边界

### 2.1 CLI 层

**职责**：解析命令行参数；组装请求模型；调用编排层公开入口；渲染最终输出。

**禁止**：直接操作底层工具；直接拼 prompt；直接维护跨阶段状态。

### 2.2 Orchestrator 层

**职责**：创建 `ContextState`；驱动 5 阶段流程；调度工具与模型；聚合结构化响应。

**禁止**：解析 CLI 参数细节；实现具体工具逻辑。

### 2.3 Tools 层

**职责**：提供可注册、可枚举、可执行工具；统一暴露 `ToolSpec` 与 `execute(**kwargs)`；明确 `ToolSafety` 与并发能力。

**禁止**：决定全局调用顺序；决定编排终止条件。

### 2.4 当前 readonly 默认工具集（MVP）

当前默认 `ToolRegistry` 仅包含以下只读工具：

- `read_file`
- `glob_files`
- `grep_files`
- `list_dir`

说明：

- 上述 4 个工具构成当前 readonly MVP 默认能力包。
- `write` / `execute` 工具暂未进入默认实现集。
- 高风险工具仍按第 11 节策略处理：交互模式需确认，CI 默认拒绝。

---

## 3. 编排层公开入口（稳定 API）

`src/orchestrator/` 必须暴露如下入口类与方法签名：

```python
class AgentOrchestrator:
    async def run_review(self, request: ReviewRequest) -> ReviewResponse:
        ...

    async def run_debug(self, request: DebugRequest) -> DebugResponse:
        ...
```

CLI 只允许依赖 `run_review()` 与 `run_debug()`；禁止依赖内部 phase 方法。

---

## 4. 请求 / 响应模型放置位置（固定）

| 类型 | 固定路径 | 说明 |
|------|----------|------|
| `ReviewRequest` / `DebugRequest` / `ReviewResponse` / `DebugResponse` | [`src/analyzer/schemas.py`](../src/analyzer/schemas.py) | 供 CLI 与编排层共用，避免 `models` 反向依赖分析层 |
| `ReviewReport` / `ReviewIssue` | [`src/analyzer/output_formatter.py`](../src/analyzer/output_formatter.py) | 分析侧权威定义 |
| `ContextState` | [`src/analyzer/context_state.py`](../src/analyzer/context_state.py) | 会话状态权威定义 |

新增契约模型时必须走 Pydantic，禁止使用 CLI 私有裸字典作为跨层协议。

---

## 5. 请求模型（固定字段）

### 5.1 `ReviewRequest`

```python
class ReviewRequest(BaseModel):
    repo_path: str
    diff_mode: bool = False
    diff_text: str | None = None
    model_name: str | None = None
    verbose: bool = False
```

| 字段 | 说明 |
|------|------|
| `repo_path` | 目标仓库或目录路径 |
| `diff_mode` | 是否按 diff 模式运行 |
| `diff_text` | diff 正文；允许为空 |
| `model_name` | 对应 CLI `--model`，覆盖默认 `MODEL_NAME` |
| `verbose` | 是否输出详细运行信息 |

### 5.2 `DebugRequest`

```python
class DebugRequest(BaseModel):
    repo_path: str
    error_log_path: str | None = None
    error_log_text: str | None = None
    model_name: str | None = None
    verbose: bool = False
```

| 字段 | 说明 |
|------|------|
| `error_log_path` | 错误日志路径 |
| `error_log_text` | 错误日志正文；允许直接注入 |

---

## 6. 响应模型（固定字段）

### 6.1 `ReviewResponse`

```python
class ReviewResponse(BaseModel):
    run_id: str
    report: ReviewReport
    context: ContextState
```

- `report` 使用 [`output_formatter.py`](../src/analyzer/output_formatter.py) 定义。
- `context` 必须返回，用于审计、复盘与调试。

### 6.2 `DebugResponse`（与 `ReviewIssue` 语义对齐）

```python
class DebugStep(BaseModel):
    title: str
    detail: str
    location: str = ""
    evidence: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class SuggestedCommand(BaseModel):
    command: str
    rationale: str
    risk: Literal["low", "medium", "high"] = "medium"


class DebugResponse(BaseModel):
    run_id: str
    summary: str
    hypotheses: list[str]
    steps: list[DebugStep]
    suggested_commands: list[SuggestedCommand] = Field(default_factory=list)
    suggested_patch: str | None = None
    context: ContextState
```

约束：

- `DebugStep.location` / `evidence` / `confidence` 语义与 `ReviewIssue` 对齐。
- `suggested_commands` 仅表示建议执行命令，不代表已执行。
- 本节契约立即生效；如代码暂未实现全部字段，按“契约已生效，代码待实现”管理里程碑。

---

## 7. CLI 到编排层调用约定（强制）

- CLI 禁止直接访问 `ToolRegistry`。
- CLI 禁止直接构造 `ContextState`。
- CLI 渲染只依赖 `ReviewResponse` / `DebugResponse`。

固定调用模式：

- `review`：构造 `ReviewRequest` 后调用 `await orchestrator.run_review(request)`。
- `debug`：构造 `DebugRequest` 后调用 `await orchestrator.run_debug(request)`。

---

## 8. Orchestrator 到 Tools 的调用约定（强制）

编排层只能通过 `ToolRegistry` 获取工具并执行：

```python
tool = registry.get(tool_name)
result = await tool.execute(**tool_args)
```

配套强约束：

- 工具发现唯一来源：`registry.list_specs()`。
- 并发判定唯一依据：`tool.is_concurrency_safe()`。
- `write` / `execute` 工具必须走第 11 节安全策略。
- 路径权限根目录由编排层按本次请求 `repo_path` 注入；工具层不得回退到用户可控路径根。
- readonly 工具采用“保守并发”：仅连续的并发安全只读调用批量并发，遇到非并发安全或高风险工具即切回串行。
- 返回结果顺序必须与 `tool_calls` 原始顺序一致。

### 8.1 工具结果统一信封（固定）

所有工具返回值必须可序列化，并统一封装为：

```python
class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: str | None = None
```

`ok=False` 时 `error` 必须非空；`ok=True` 时 `data` 应承载主结果。

补充语义约束：

- 若工具返回 `truncated` 字段，其值仅在“存在未返回结果”时为 `true`。

---

## 9. Orchestrator 与 Analyzer / Model 边界（固定）

### 9.1 与 Analyzer

- 输入必须包含：`ContextState`、`ReviewRequest`/`DebugRequest`、`list[ToolSpec]`。
- 输出必须为结构化计划对象 `AnalysisPlan`（Pydantic）：

```python
class AnalysisPlan(BaseModel):
    needs_tools: bool
    tool_calls: list[dict[str, Any]]
    draft_review: ReviewReport | None = None
    draft_debug: DebugResponse | None = None
```

- 最终展示结构由 analyzer 产出，编排层负责聚合并封装响应。

### 9.2 与 Model（`ModelClient`）

- 编排层必须通过 [`ModelClient.chat`](../src/models/client.py) 调用模型。
- 模型消息与配置必须使用 [`src/models/schemas.py`](../src/models/schemas.py) 的 `Message`、`ModelConfig`、`ModelResponse`。
- 工具列表转换必须由编排层固定函数完成，并在 `src/orchestrator/` 实现与维护。
- **实现锚点**：[`src/orchestrator/tool_schemas.py`](../src/orchestrator/tool_schemas.py) 提供 `build_tool_schemas()` 与 `build_submit_tool_schemas()`；[`AgentOrchestrator`](../src/orchestrator/agent_loop.py) 在 `analyze` 阶段组装后传入 `InferenceEngine`。
- 模型异常必须转为 `ContextState.errors` 或结构化降级结果，禁止裸抛到 CLI。

---

## 10. 五阶段职责与命名（固定）

`run_review()` / `run_debug()` 内部必须执行以下 5 个阶段：

1. `prepare_context(request) -> ContextState`
2. `analyze(state, request, tool_specs) -> AnalysisPlan`
3. `execute_tools(plan, registry, state) -> list[ToolResult]`
4. `format_result(state, tool_results) -> ReviewResponse | DebugResponse`
5. `should_continue(state, response) -> bool`

### 10.1 `DecisionStep.phase` 固定枚举值

`ContextState.decisions[].phase` 仅允许以下值：

- `prepare`
- `analyze`
- `execute_tools`
- `format`
- `continue`

---

## 11. Security 与高危工具（固定策略）

权限模式补充：

- `default`：按本节策略执行高危工具门控。
- `plan`：禁止执行任何工具（含 readonly/write/execute），仅允许规划与结构化输出阶段。

`write` / `execute` 工具执行策略矩阵：

| 运行模式 | write | execute |
|---------|-------|---------|
| 交互模式（CLI） | 必须用户确认后执行 | 必须用户确认后执行 |
| 非交互模式（CI） | 默认拒绝 | 默认拒绝 |

补充约束：

- CLI 禁止绕过编排层直接调用任何高危工具。
- 被拒绝的高危调用必须写入 `ContextState.errors`，`category` 设为 `security`。

**实现说明**：`AgentOrchestrator` 支持可选参数 `confirm_high_risk`（回调），在交互模式下对 `write` / `execute` 工具在回调返回允许时方可执行；**未提供回调时默认拒绝**高危工具（避免误将「交互模式」理解为自动放行）。环境变量 `CI` 为真时强制拒绝，与上表「非交互模式默认拒绝」一致。

---

## 12. 单次运行与可观测性（固定）

| 项目 | 固定约束 |
|------|----------|
| `run_id` | 必须使用 UUID4，写入 `ReviewResponse`/`DebugResponse` 与日志 |
| 记录内容 | 必须记录工具调用序列、关键中间结果、耗时、token 用量 |
| 输入快照 | 默认不落盘；仅在显式开启配置时落盘且必须脱敏 |

`RunMetadata` 作为未来扩展字段，不影响本契约的当前必填字段。

---

## 13. 错误处理与异常类型（固定）

分层职责：

- **CLI**：负责用户可读错误信息与退出码。
- **Orchestrator**：负责聚合为 `ContextState.errors`。
- **Tools**：必须抛出自定义异常，禁止裸抛 `Exception`。

模型侧异常必须使用 [`src/models/exceptions.py`](../src/models/exceptions.py) 体系；工具侧异常统一放在 `src/tools/exceptions.py`。

---

## 14. 非功能性约束（固定）

| 主题 | 固定约束 |
|------|----------|
| 异步 | 编排入口与 `BaseTool.execute` 必须是 `async`；CLI 必须以异步方式驱动 |
| 超时 | 模型超时由 `ModelConfig.timeout` 控制；工具超时必须在工具或 security 层显式配置 |
| 取消 | 接收到取消信号时必须抛 `asyncio.CancelledError` 并记录到 `ContextState.errors` |

---

## 15. 确定性约束清单（验收用）

1. 公开编排入口固定为 `run_review()` / `run_debug()`。
2. 跨层请求与响应模型固定在 `src/analyzer/schemas.py`，`ReviewReport` 由 analyzer 维护。
3. `ReviewResponse` 固定为 `run_id + report + context`。
4. `DebugResponse` 固定包含 `location/evidence/confidence` 与 `suggested_commands`。
5. CLI 禁止直接调用 `ToolRegistry` 与高危工具。
6. 工具返回固定为 `ToolResult` 信封。
7. `DecisionStep.phase` 仅允许 5 个固定值。
8. 高危工具执行固定为“交互确认、CI 默认拒绝”。

---

## 16. 一句话结论

- `cli.py` 只负责组装请求并调用 `run_review()` / `run_debug()`。
- `src/orchestrator/` 负责完整 5 阶段编排与安全门控。
- `src/tools/` 通过 `ToolRegistry + BaseTool + ToolResult` 提供标准能力。
- 全部跨层协议以本文件为准。

---

## 修订记录

| 日期 | 说明 |
|------|------|
| 2026-04-09 | 初稿入库：CLI/编排/工具边界、请求响应、Analyzer/Model/Security/观测补充、协作前清单 |
| 2026-04-09 | 收敛为确定性约束：定稿 Debug 对齐字段、`suggested_commands`、高危工具执行矩阵与固定验收清单 |
| 2026-04-12 | §9.2 补充 `tool_schemas.py` 实现锚点；§11 补充 `confirm_high_risk` 与默认拒绝行为 |
| 2026-04-13 | 补充 readonly 默认工具集说明；收口 readonly MVP 的测试、异常与默认注册契约 |
