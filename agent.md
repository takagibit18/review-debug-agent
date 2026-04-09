# AGENT 开发约束（项目级）

本文档用于约束 AI Agent 在本仓库内的开发行为，目标是让 Agent 通过**渐进式披露**按需获取知识，避免一次性加载全部文档造成噪声与偏移。

---

## 1. 使用原则（渐进式披露）

### 1.1 必须遵循的读取顺序

1. 首先阅读本文件（`agent.md`），理解任务边界与执行规范。
2. 根据任务类型按需进入 `docs/` 读取最少必要文档。
3. 若仍不确定，再补充阅读 `CONTRIBUTING.md` 与 `eval/README.md`。
4. 仅在需要详细历史背景时阅读 `docs/project_plan.md` 全文。

### 1.2 最小知识原则

- 非必要不全量扫描仓库文档。
- 先读目录索引，再定位到单一文档章节。
- 对未确认的接口/协议，不做主观扩展，先回到契约文档核对。

---

## 2. 知识目录（按需进入）

### 2.1 文档结构

```text
docs/
├── architecture.md         # 分层架构、5 阶段编排、关键设计决策
├── error_log.md            # 开发过程中遇到的错误与解决记录
├── shared_contracts.md     # Analyzer/Integration 共享接口与协议契约
└── project_plan.md         # 项目规划与里程碑（由根目录计划文档迁移）

root/
├── README.md               # 项目介绍、安装运行、目录与开发命令
├── CONTRIBUTING.md         # Git/PR/Issue/代码风格协作规范
├── agent.md                # 本文件：Agent 行为约束与索引入口
└── eval/README.md          # 评测策略与黄金集说明
```

### 2.2 任务到文档的映射

- 架构与模块边界问题 → `docs/architecture.md`
- 工具接口、状态模型、输出 schema → `docs/shared_contracts.md`
- 里程碑、分工、演进路线 → `docs/project_plan.md`
- 提交流程、分支规范、代码风格 → `CONTRIBUTING.md`
- 评测标准与基线策略 → `eval/README.md`

---

## 3. 编码规范（Agent 必须执行）

### 3.1 语言与类型

- 使用 Python 3.11+ 语法与特性。
- 新增/修改函数必须提供参数与返回值类型注解。
- 避免在模块间传递无约束裸 `dict`；结构化数据优先使用 Pydantic 模型。

### 3.2 格式与静态检查

- 代码需通过：
  - `ruff check .`
  - `ruff format --check .`
  - `mypy src/`
- 行宽上限 120 字符。

### 3.3 注释与文档语言

- 代码注释、docstring、符号命名使用英文。
- README 与面向用户/协作的说明文档使用中文。
- 函数与类应提供简明 docstring（说明职责与关键约束）。

### 3.4 数据模型与协议一致性

- 配置、输入、输出、状态模型使用 Pydantic `BaseModel`。
- 关键字段应提供 `Field(description=...)`。
- 涉及工具协议时，必须遵循 `docs/shared_contracts.md` 的定义：
  - `ToolSpec`
  - `ToolSafety`
  - `BaseTool`
  - `ToolRegistry`

### 3.5 异常与错误处理

- 不裸抛 `Exception`，优先使用语义化异常类型。
- 工具失败/命令失败需返回结构化错误，禁止静默吞错。
- 在可恢复场景提供降级路径（例如要求补充日志或改为只读分析）。

### 3.6 输出规范

- Review 输出必须兼容 `ReviewIssue` / `ReviewReport` 结构。
- Debug 输出若无既定模型，先在 `docs/shared_contracts.md` 对齐后再实现。
- 新增或变更输出字段需在文档中同步更新并说明兼容性影响。

### 3.7 工具开发规范

- 新工具必须继承 `BaseTool` 并实现 `spec()`、`execute()`。
- 必须声明 `ToolSafety` 分级（`readonly` / `write` / `execute`）。
- 并发能力必须通过 `is_concurrency_safe()` 明确表达，不默认推断。

### 3.8 模块放置规范

- 代码按职责放入：
  - `src/analyzer/`
  - `src/orchestrator/`
  - `src/tools/`
  - `src/security/`
  - `src/models/`
- 避免在 `src/` 根目录堆积无域归属文件。

### 3.9 测试要求

- 测试使用 `pytest`，放在 `tests/`。
- 新功能至少包含对应单元测试。
- 影响模块交互的改动需增加集成测试或端到端验证步骤。

### 3.10 Git 与提交

- 分支命名：`feat/...`、`fix/...`、`chore/...`
- 提交信息：`<type>(<scope>): <description>`
- 常用 scope：`analyzer`、`tools`、`cli`、`models`、`security`、`eval`、`docker`

---

## 4. Agent 执行流程（建议）

1. 读取 `agent.md`，判断任务类型。
2. 根据 2.2 映射只读取必要文档。
3. 实施改动前核对接口/输出契约（优先 `docs/shared_contracts.md`）。
4. 改动后运行最小验证（lint/type/test 中相关项）。
5. 若在开发、调试或验证过程中遇到错误（如 lint 报错、测试失败、运行异常），必须在 `docs/error_log.md` 追加记录（日期、模块、错误摘要、原因、修复方式）。
6. 若协议变化，先更新文档再更新实现。

---

## 5. 文档维护规则

- `docs/` 中文档是开发知识主存放区。
- 根目录保留高入口文件（如 `README.md`、`CONTRIBUTING.md`、`agent.md`）。
- 任何新增“开发指导类文档”默认放在 `docs/` 下并在本文件补充索引。
