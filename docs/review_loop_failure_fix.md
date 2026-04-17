# Review Loop 失败根因与修复记录

- 关联计划：`.cursor/plans/review_loop_failure_analysis_1556ec02.plan.md`
- Fixture：`golden_astral-sh_ruff_pr24648`
- 失败 trace：`eval/outputs/event_logs/golden_astral-sh_ruff_pr24648_124aed86-e2e6-44b0-b38f-acb77b203bb2.jsonl`
- 修复后 trace：`eval/outputs/event_logs/golden_astral-sh_ruff_pr24648_bc615b04-081a-4770-a70a-bded11a6c795.jsonl`
- 修复后 report：`eval/outputs/qualified4_after_context_fix.json`

## 1. 错误现象（修复前）

`REVIEW_MAX_ITERATIONS=4` 下，fixture 跑完 3 轮仍没有 `submit_review`，最后被 token budget 掐断，返回 placeholder，`issues_count=0`。而同一 fixture 在 MVP 时期（2 轮就能 submit）却能成功。

关键证据：iter 0 读过文件 A 的 `args_digest.sha256=992d06dd…`，iter 2 再次读同一文件，sha256 完全相同——模型在第 3 轮已经"忘了"自己 iter 0 干过什么。

## 2. 原因分析（三条主因）

| 层 | 问题 | 代码位置 |
|---|---|---|
| 数据流 | `tool_feedback` 每轮被**整体覆盖**，iter 2 的 prompt 里只剩 iter 1 的结果，iter 0 的读文件凭空消失 | `agent_loop.py: self._tool_feedback = executed_feedback` |
| 预算 | `TOKEN_BUDGET=12000` 偏紧且只有硬阈值；iter 2 累计就超标、直接 stop，此时 draft 尚未生成 | `config.py` + `result_processor.is_budget_exhausted` |
| 兜底 | stop 后 `draft_review is None` → placeholder，却依然 `schema_valid=True`，失败被伪装成合规输出 | `result_processor.format_review` |

另有两条辅助因素：`execute_tools` 无"相同 args 已执行过"缓存；prompt 到末轮不会收口。

根因链一句话：**忘（窗口=1）+ 截断（budget 紧）+ 空兜底（placeholder 仍 schema_valid）**，三者叠加把 MVP 时代"2 轮就够"的隐含前提击穿。

## 3. 改进策略

按投入产出顺序：

### P0（直接闭环失败）

- **P0.1 tool_feedback 窗口 + digest 索引**  
  最近 N 轮（默认 3）条目原样注入 prompt 并加 `[iter=N]` 前缀；窗口外条目折叠成 `prior_tool_results_summary` user 消息（保留 `iter / name / args_preview / result_preview`），让模型知道"这些已经读过，不要重复"。
- **P0.2 Force-submit 兜底**  
  loop break 后如果 `draft is None` 且非 hard_capped，追加一次 `analyze(force_submit=True)`：只暴露 submit 工具 schema，prompt 追加"FINAL CALL"提示，强制模型给出最终 JSON。
- **P0.3 Token budget 加大 + 软硬双阈值**  
  默认 `TOKEN_BUDGET` 12000 → 24000；soft_capped=1x（允许走 finalize），hard_capped=2x（才真正硬停）。

### P1（流水线健壮性）

- **P1.4 重复工具调用去重**：READONLY 工具以 `sha256(name+args)` 做 run 级缓存，命中直接回"already executed, synthesize now"。
- **P1.5 失败画像**：`EvalResult` 新增 `placeholder_summary / submit_*_seen_any / budget_exhausted / budget_state / finish_reasons`，并把 placeholder 从 `schema_validity_rate` 分子里剔除——假成功无所遁形。
- **P1.6 decision 事件**：`reason` 枚举（`model_completed | max_iterations | budget_soft_capped | budget_hard_capped`）+ 累计 `submit_*_seen_any`。

### P2（软引导）

- **P2.7 末轮 prompt 收口**：`iteration == max-1` 时追加"最后一轮、优先 submit"软提示；P0.1 的 `prior_tool_results_summary` 天然完成了"不要再重复读"的提醒职责。

## 4. 落地改进结果

### 4.1 运行参数

- `REVIEW_MAX_ITERATIONS=4`，`TOKEN_BUDGET=24000`（新默认），`FEEDBACK_WINDOW_ITERATIONS=3`。
- 同一 fixture `golden_astral-sh_ruff_pr24648`，run_id `bc615b04-081a-4770-a70a-bded11a6c795`。

### 4.2 逐轮行为（修复后）

| iter | 动作 | 累计 tokens | budget_state | 有无重复 digest |
|---|---|---|---|---|
| 0 | read_file(async_function_with_timeout.rs) + read_file(ASYNC109_0.py) | 3847 | none | — |
| 1 | grep_files(abstractmethod) + grep_files(is_override) | 11064 | none | 无 |
| 2 | grep_files(is_abstract) | 19133 | none | 无 |
| 3 | **submit_review**（3 issues） | 28449 | soft_capped | 无 |

- 所有 5 次工具调用 `args_digest` **两两不同**——"忘+重读"症状彻底消失。
- 第 3 轮达 soft_cap（28449 > 24000）但模型已自主 submit，不需要触发 force-submit 兜底。
- `decision` 事件 reason=`model_completed`，`submit_review_seen_any=true`，`placeholder_summary=false`。

### 4.3 指标对比

| 指标 | 修复前（124aed86） | 修复后（bc615b04） |
|---|---|---|
| draft_review | ❌ 无，placeholder | ✅ 3 issues |
| matched / expected | 0 / 1 | **1 / 1** |
| pass@k hit_rate | 0.0 | **1.0** |
| schema_validity_rate | 1.0（假成功） | 1.0（真成功，已排除 placeholder） |
| false_positive_rate | — | 0.67（2 条 doc/snapshot 建议，非回归，属模型风格问题） |
| total_tokens | 15380（iter 2 就 budget_exhausted） | 28449（到 soft_cap 但 draft 已交付） |
| budget_state | exhausted | soft_capped（不再致命） |

（数据来源：`eval/outputs/qualified4_after_context_fix.json` 与上述 trace）

### 4.4 结论

三条主因全部闭环：

- C1 "忘"：窗口+digest 索引后，模型看得见 iter 0 的全部产物，无一次重复 read/grep。
- C2 "截断"：预算加大 + 软硬双阈值后，soft_cap 不再阻止本轮 submit；真要超 hard_cap 才硬停。
- C3 "空兜底"：本次甚至没触发 force-submit（模型自主 submit），但兜底链路已就位；`placeholder_summary` 指标让未来若再出现假成功会立刻暴露。

剩余 2 条 false positive（doc 缺失 + snapshot 需重生）属于模型风格层面的产出，不在本次修复范围。如需进一步压低 FP，后续可在 `SYSTEM_PROMPT_REVIEW` 或 golden 集层面做 severity 校准。

## 5. 相关改动文件

- `src/config.py`：`token_budget` 默认 24000、新增 `feedback_window_iterations`
- `src/analyzer/result_processor.py`：新增 `budget_state()`
- `src/analyzer/prompts.py`：新增 `FINALIZE_REVIEW_NOTICE / FINALIZE_DEBUG_NOTICE`
- `src/analyzer/inference_engine.py`：窗口注入 + digest 折叠 + finalize/near-last 提示
- `src/orchestrator/agent_loop.py`：窗口维护、dedup 缓存、budget_state、reason 枚举、`_maybe_force_submit_*`
- `eval/schemas.py` + `eval/runner.py`：失败画像字段 + placeholder 剔除
- `.env.example`：新配置注释
- `tests/test_agent_loop.py`：mock 签名与断言兼容新流程
