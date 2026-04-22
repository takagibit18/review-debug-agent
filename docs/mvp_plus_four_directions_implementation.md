# MVP+ 四方向落地实施说明（本次提交）

> 日期：2026-04-20  
> 目标：将“评测侧可行”升级为“生产侧一致可用”，并把质量门禁前置到 CI 主干流程。

---

## 1. 本次改动范围总览

本次实现覆盖四个方向：

- **方向 A（P0）生产路径输入链路**
  - `load_diff` 语义调整为“相对 `HEAD` 的全量工作区变更”（staged + unstaged）。
  - 新增 `project_structure` 上下文生成。
  - 新增 `file_contents` 按需加载与预算裁剪。
  - Orchestrator → InferenceEngine → Prompt Payload 全链路透传新增上下文。
- **方向 B（P0）先探目录再搜索策略下沉**
  - review/debug 系统提示词加入路径探索策略约束。
  - 工具失败结构化回退，显式推荐下一步（优先 `list_dir`）。
  - 工具失败摘要注入模型上下文，降低无效重试。
- **方向 C（P0）Golden 扩容与 CI 门禁**
  - Golden 集扩容至 **10 条**，并满足 `4/3/3`（应检出/零问题/边界噪声）分布。
  - fixture 加载优先 `manifest`，统一读取入口。
  - 新增 eval gate 脚本并接入 CI，上传评测产物。
- **方向 D（P1）location 契约收紧**
  - 引入 canonical 位置格式：`path[:line[-end_line]]`。
  - 新增 location 归一化解析器（兼容自由文本输入）。
  - 提交 schema 与评测匹配逻辑同步：语义优先 + legacy 回退。

---

## 2. 详细变更说明

## 2.1 方向 A：生产输入链路

### 2.1.1 `load_diff` 语义调整

- 文件：`src/analyzer/context_builder.py`
- 变更：
  - 从 `git diff --cached` 改为 `git diff HEAD`。
  - 结果：在 review `diff_mode` 下可覆盖 staged + unstaged 的工作区变更，不再只看暂存区。

### 2.1.2 新增 `project_structure`

- 文件：`src/analyzer/context_builder.py`
- 新增方法：`build_project_structure(...)`
- 行为：
  - 从 `repo_path` 构建目录树摘要文本；
  - 支持深度与条目数量上限；
  - 超限时输出 `... (truncated)` 标记。

### 2.1.3 新增按需 `file_contents` 装载

- 文件：`src/analyzer/context_builder.py`
- 新增方法：`load_diff_file_contents(...)`
- 行为：
  - 优先加载 diff 涉及文件；
  - 补充同目录高相关候选（如测试文件命名变体）；
  - 三重预算控制：
    - 文件数上限；
    - 单文件字符上限；
    - 总字符上限；
  - 超预算时天然优先保留 diff 文件（候选扩展在后）。

### 2.1.4 全链路透传

- 文件：`src/orchestrator/agent_loop.py`
  - 在 analyze 前生成 `project_structure` 与 `file_contents`；
  - 透传给 inference engine。
- 文件：`src/analyzer/inference_engine.py`
  - `analyze(...)` 增加 `project_structure` 参数；
  - 向 review/debug message builder 透传。
- 文件：`src/analyzer/context_priority.py`
  - `assemble_review_payload(...)` / `assemble_debug_payload(...)` 输出 `project_structure` 字段；
  - 保持与现有 `truncated` 机制兼容。
- 文件：`src/config.py`
  - 新增配置项：
    - `PROJECT_STRUCTURE_MAX_DEPTH`
    - `PROJECT_STRUCTURE_MAX_ENTRIES`
    - `FILE_CONTEXT_MAX_FILES`
    - `FILE_CONTEXT_MAX_CHARS_PER_FILE`
    - `FILE_CONTEXT_MAX_CHARS_TOTAL`

---

## 2.2 方向 B：路径探索策略下沉

### 2.2.1 系统提示词策略强化

- 文件：`src/analyzer/prompts.py`
- 主要新增约束：
  - 路径不确定先 `list_dir`，再 `glob/grep/read_file`；
  - 发生 `Directory/File not found` 后，先校验父目录，禁止盲重试；
  - review/debug 均同步此策略。

### 2.2.2 工具失败结构化回退

- 文件：`src/orchestrator/agent_loop.py`
- 变更：
  - tool 不存在时，`ToolResult.data` 中附带结构化建议；
  - `ToolError` 时根据错误类型生成推荐下一步（路径问题优先 `list_dir`）。
- 目标：
  - 把“失败原因 + 推荐动作”显式提供给模型，减少重复失败循环。

### 2.2.3 失败反馈摘要注入

- 文件：`src/analyzer/inference_engine.py`
- 新增：
  - `_build_failure_guidance_message(...)`
  - 从 tool feedback 抽取失败调用，形成紧凑提示注入模型上下文。

---

## 2.3 方向 C：Golden 扩容与 CI 门禁

### 2.3.1 Golden fixture 扩容

- 新增 8 条 fixture（`eval/fixtures/golden_manual_*.json`）：
  - 检出类（3）：`detect_unchecked_none` / `detect_sql_injection` / `detect_swallow_exception`
  - 零问题类（2）：`zero_refactor_rename` / `zero_add_test_only`
  - 边界噪声类（3）：`boundary_large_whitespace` / `boundary_generated_file` / `boundary_mixed_rename`
- 同时将现有 2 条 fixture 的标签补齐，使总集满足：
  - **应检出 4**
  - **预期 0 issue 3**
  - **边界/噪声 3**

### 2.3.2 Manifest 统一读取

- 文件：`eval/runner.py`
- 变更：
  - `load_fixtures(...)` 改为优先解析 `eval/fixtures/manifest.json`；
  - manifest 无效时回退到目录扫描；
  - 避免 fixture 索引字段/来源不一致导致的读取偏差。

### 2.3.3 CI 门禁接入

- 新增文件：`eval/gate.py`
  - 输入 report JSON；
  - 阈值校验：
    - `schema_validity_rate >= 1.0`
    - `hit_rate >= 0.8`
    - `false_positive_rate <= 0.5`
  - 失败即返回非 0 退出码。
- 文件：`.github/workflows/ci.yml`
  - 在 lint/mypy/pytest 后新增：
    1. `python -m eval.run eval --suite golden --output-json eval/outputs/ci_report.json`
    2. `python -m eval.gate --report ...`
    3. 上传产物（report/human_review/event_logs）

### 2.3.4 稳定性参数

- 通过 CI job env 固定：
  - `EVAL_SAMPLES=1`
  - `EVAL_CONCURRENCY=1`
  - `EVAL_TEMPERATURE=0.0`
- 同时 `eval/run.py` 支持 `--output-json` 固定输出路径，便于 CI gate 消费。

---

## 2.4 方向 D：location 契约收紧

### 2.4.1 新增 location 解析器

- 新增文件：`src/analyzer/location.py`
- 能力：
  - canonical 解析：`path[:line[-end_line]]`
  - 路径归一（`\` → `/`、去冗余前缀）
  - 兼容自由文本中的 `path:line` 抽取
  - 非法输入返回 warning（不静默）

### 2.4.2 submit_review 归一化

- 文件：`src/analyzer/inference_engine.py`
- 变更：
  - `_normalize_review_payload(...)` 现在会规范化 `issue.location`；
  - 输出 location warning 元信息，进入 trace payload；
  - 保持对旧输入格式兼容。

### 2.4.3 schema 与文案同步

- 文件：`src/orchestrator/tool_schemas.py`
  - `submit_review` / `submit_debug` 的 `location` 增加 pattern 与 canonical 描述。
- 文件：`src/analyzer/output_formatter.py`
  - `ReviewIssue.location` 字段说明更新为 canonical 语义。
- 文件：`src/analyzer/prompts.py`
  - 明确要求模型输出 canonical location，禁止纯自然语言位置描述。

### 2.4.4 Eval 匹配增强

- 文件：`eval/schemas.py`
  - `ExpectedIssue` 新增：`path`, `line`, `end_line`。
- 文件：`eval/runner.py`
  - 新增 `_semantic_location_matches(...)`：
    - 路径一致 + 行号区间重叠优先；
  - 保留 `_location_matches(...)` 正则回退，保障迁移期兼容。

---

## 3. 测试与验证

本次新增/更新测试覆盖：

- `tests/test_context_builder.py`
  - `load_diff` 命令语义；
  - `project_structure` 深度/条目限制；
  - `file_contents` 预算与优先级。
- `tests/test_location_contract.py`
  - canonical、兼容解析、非法区间校验。
- `tests/test_inference_engine.py`
  - review payload location 归一化。
- `tests/test_eval_runner.py`
  - manifest 优先读取；
  - 语义 location 匹配；
  - golden 分布（>=10 且 4/3/3）校验。
- `tests/test_eval_gate.py`
  - 门禁通过/失败分支。

本地执行结果：

- `pytest -q`：**149 passed**
- `ruff check .`：**passed**
- `mypy src/`：**passed**

说明：当前项目历史上 `mypy src/ eval/` 仍存在 eval 侧既有类型告警，本次未扩范围修复（避免引入与需求无关改动）。

---

## 4. 文件清单（核心）

- 代码：
  - `src/config.py`
  - `src/analyzer/context_builder.py`
  - `src/analyzer/context_priority.py`
  - `src/analyzer/inference_engine.py`
  - `src/analyzer/prompts.py`
  - `src/analyzer/output_formatter.py`
  - `src/analyzer/location.py`（新增）
  - `src/orchestrator/agent_loop.py`
  - `src/orchestrator/tool_schemas.py`
- 评测：
  - `eval/runner.py`
  - `eval/schemas.py`
  - `eval/report.py`
  - `eval/run.py`
  - `eval/gate.py`（新增）
  - `eval/fixtures/manifest.json`
  - `eval/fixtures/review_checklist.md`
  - `eval/fixtures/golden_manual_*.json`（新增 8 条）
- CI：
  - `.github/workflows/ci.yml`
- 测试：
  - `tests/test_context_builder.py`（新增）
  - `tests/test_location_contract.py`（新增）
  - `tests/test_eval_gate.py`（新增）
  - `tests/test_eval_runner.py`
  - `tests/test_inference_engine.py`

---

## 5. 兼容性与后续建议

- 当前 location 策略为“**规范化 + 兼容解析 + warning 可观测**”，尚未强制 hard fail，符合迁移期目标。
- 建议后续按两周窗口观测 CI report：
  - 若 `schema_validity_rate` 稳定为 1.0，可进一步收紧 location 非法输入处理比例；
  - 若 `false_positive_rate` 可持续低于 0.3，可考虑收紧门禁阈值。

