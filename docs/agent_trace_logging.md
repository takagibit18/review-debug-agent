# Agent 流水线分阶段日志：设计思路与落地说明

本文用通俗语言说明：**为什么要加这套日志、记什么、怎么开、去哪看、和评测怎么衔接**。实现代码分布在 `src/analyzer/trace.py`、`src/analyzer/inference_engine.py`、`src/orchestrator/agent_loop.py`、`src/analyzer/event_log.py` 与 `src/config.py`。

---

## 1. 要解决什么问题？

Review/Debug 跑完后，有时只看到最终结果（例如 `issues` 为空、summary 是占位文案），但**不知道中间哪一步出了问题**：

- 模型有没有调用 `submit_review`？调了但 JSON 校验失败？
- 是不是一直要执行工具，循环在 `max_iterations` 处被截断？
- 某次工具调用是否失败或被安全策略拦住？

**设计目标**：在同一次运行（同一个 `run_id`）里，用**一条时间线**把「模型说了什么、计划了什么、工具执行了什么、最终怎么组装的」串起来，方便复盘，而不是只看最终响应。

---

## 2. 总体思路（一句话）

继续沿用原来的 **JSONL 事件日志**（每条一行 JSON），在**不替换现有粗粒度事件**的前提下，增加几类**更细的事件类型**；用**开关**控制是否写入详细内容，避免默认把大段文本写进磁盘。

---

## 3. 日志写在哪里？

- 目录：由环境变量 `EVENT_LOG_DIR` 决定，默认 `.cr-debug-agent/logs`（相对路径会落在**本次运行的工作区/repo 根目录**下）。
- 文件：`{run_id}.jsonl`，一行一个事件，按时间追加。

编排器在每次 run 开始时生成 `run_id`，与最终 `ReviewResponse` / `DebugResponse` 里的 `run_id` 一致，便于和评测、人工排查对齐。

---

## 4. 分阶段记什么？（心智模型）

可以把一次循环想成四步：**分析 → 执行工具 → 组装结果 → 决定是否再来一轮**。

| 阶段 | 通俗含义 | 新增/增强的观测点 |
|------|----------|-------------------|
| **analyze** | 模型看完上下文后，决定「要不要调工具、有没有提交结构化结果」 | `model_response_detail`：助手正文预览、模型名、`finish_reason`、用量等；`plan_parsed`：是否解析出 `draft_review`/`draft_debug`、`submit_*` 是否出现、校验错误摘要、是否走了正文里的 fallback JSON 等 |
| **execute_tools** | 真正执行读文件、搜索等 | `tool_io`：工具名、是否成功、错误信息、参数与结果的 **摘要/digest**（不是默认全文 dump） |
| **format** | 把 plan 转成对外返回的报告 | `format_result`：是否有 draft、issue 数量、是否落回「占位 summary」等 |
| **continue** | 要不要进入下一轮 | 原有 `decision` 事件增强：当前迭代、最大迭代、是否还有待执行工具等 |

**「思考」在日志里指什么？**  
以 API 返回的 **assistant 文本 `content`** 的预览为主（可截断）。若将来模型提供单独 reasoning 字段，可以再扩展 schema。

---

## 5. 配置项（怎么开、怎么控体积）

| 环境变量 | 含义 | 典型用法 |
|----------|------|----------|
| `AGENT_TRACE_DETAIL` | `off` / `compact` / `full` | 默认 `off`：不写详细 trace 事件，行为与加功能前接近；排查问题时设为 `compact` 或 `full` |
| `AGENT_TRACE_MAX_CHARS` | 单段文本预览最大字符数 | 防止日志爆炸，默认有下限校验 |
| `AGENT_TRACE_LOG_TOOL_BODY` | 是否在 `full` 模式下尽量记录工具结果预览 | 涉及代码内容时仍需谨慎，建议仅本地调试 |

**脱敏**：对参数/结果里疑似敏感键名（如含 `token`、`password` 等）会做键级遮蔽，减少误写密钥。

---

## 6. 落地流程（代码上怎么接起来的）

1. **`TraceRecorder`**（`src/analyzer/trace.py`）  
   统一做：截断、digest、脱敏、是否允许写详细事件。

2. **`InferenceEngine`**（`src/analyzer/inference_engine.py`）  
   在每次 `chat` 返回后：先解析 tool_calls 与可选 fallback JSON，再调用 trace 写入 `MODEL_RESPONSE_DETAIL` 与 `PLAN_PARSED`（需 orchestrator 注入写事件回调，且 detail 非 `off`）。

3. **`AgentOrchestrator`**（`src/orchestrator/agent_loop.py`）  
   - 把当前循环 **`iteration`** 传给 analyze，保证多轮日志可对齐。  
   - 在工具执行后写 `TOOL_IO`；在 format 后写 `FORMAT_RESULT`；在 continue 的 decision 里带上迭代与 pending 信息。  
   - 保留原有 `model_call`、`tool_call` 等事件，并在 payload 里补充 `iteration` 等字段，便于和旧脚本兼容。

4. **事件类型枚举**（`src/analyzer/event_log.py`）  
   新增：`model_response_detail`、`plan_parsed`、`tool_io`、`format_result`。

---

## 7. 和评测（eval）怎么配合？

`eval/runner.py` 在成功跑完一条 fixture 后，若磁盘上存在对应 JSONL，会在 **`EvalResult.event_log_path`** 里写入**绝对路径字符串**，这样从评测报告 JSON 可以直接跳到同一次 `run_id` 的完整时间线。

评测里统计 token 时仍主要依赖原有的 `model_call` 事件（与之前逻辑一致）。

---

## 8. 建议的排查顺序（实操）

1. 打开 `AGENT_TRACE_DETAIL=compact`（或 `full`），复现一次失败 run。  
2. 打开 `{event_log_dir}/{run_id}.jsonl`，按时间从下往上或从上往下看。  
3. 重点看：最后一轮 `plan_parsed` 里是否有 `submit_review`、`submit_review_validation_error` 是否非空；`continue` 是否 `stop:max_iterations`；`format_result` 里 `used_placeholder_summary` 是否为 true。  
4. 若需要核对工具层，看同迭代的 `tool_io` 与 `tool_call`。

---

## 9. 风险与注意点

- **日志体积**：生产环境建议默认 `off` 或 `compact`；`full` 仅本地。  
- **代码泄露**：即使用 digest，也要避免在不受控环境长期保留含业务代码的日志文件。  
- **兼容性**：旧消费者只认 `event_type` 字符串即可；新增类型可忽略或按需解析。

---

如需把环境变量同步进对外契约文档，可再在 `docs/shared_contracts.md` 中增加一节引用本文。
