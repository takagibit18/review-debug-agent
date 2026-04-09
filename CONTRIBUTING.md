# 协作规范

如使用 AI Agent 参与开发，请先阅读根目录 [agent.md](agent.md)（文档索引与编码约束）。

## Git 工作流

### 分支策略

- **`main`**：保护分支，仅通过 PR 合并，必须通过 CI
- 功能分支命名：`feat/<简短描述>`（如 `feat/cli-review-command`）
- 修复分支命名：`fix/<简短描述>`（如 `fix/diff-parser-edge-case`）
- 杂务分支命名：`chore/<简短描述>`（如 `chore/update-deps`）

### 提交消息格式

```
<type>(<scope>): <简要描述>

<可选详细说明>
```

**type**：`feat` | `fix` | `refactor` | `test` | `docs` | `chore` | `ci`

**scope**（可选）：`analyzer` | `tools` | `cli` | `models` | `security` | `eval` | `docker`

示例：
```
feat(cli): add review subcommand with diff input support
fix(analyzer): handle empty diff gracefully
docs: update architecture diagram in docs/
```

## Pull Request 要求

每个 PR 必须包含：

1. **动机说明**：为什么需要这个变更
2. **变更内容**：做了什么（不需要逐行解释，但要说清思路）
3. **关联 Issue**：`Closes #xxx` 或 `Relates to #xxx`
4. **测试说明**：如何验证 + 是否新增/修改了测试
5. **示例输出**（如适用）：CLI 运行截图或输出片段

合并前需要至少一位协作者 review。

## Issue 管理

### 标签约定

| 标签 | 用途 |
|------|------|
| `mvp` | MVP 阶段必须完成 |
| `phase-2` | Phase 2 扩展功能 |
| `bug` | 缺陷报告 |
| `docs` | 文档相关 |
| `discussion` | 需要讨论的设计决策 |

## 代码风格

### 基本规则

- **Python 3.11+**，使用类型注解
- **注释与 docstring 使用英文**，README / 用户文档使用中文
- 使用 `ruff` 进行 lint 和格式化，`mypy` 做类型检查
- 行宽上限 120 字符
- 函数和类必须有 docstring（简要说明即可）

### 项目约定

- Pydantic BaseModel 用于所有结构化数据（配置、输入、输出）
- 工具定义遵循统一的 JSON Schema 接口
- 异常使用自定义异常类，不裸抛 `Exception`

## 模块分工

### Analyzer Agent（核心分析引擎）

负责 `src/analyzer/`、`src/orchestrator/`、`src/models/`：
- 代码分析与上下文管理
- 结构化输出与推理引擎
- 状态管理（ContextState）

### Integration Agent（工具与交互层）

负责 `src/tools/`、`src/security/`、`cli.py`：
- CLI 交互
- 工具系统与注册机制
- 权限安全与沙箱
- 外部集成与部署配置

### 共同维护

以下区域需双方共同设计：
- Prompt 模板
- JSON Schema / 输出格式定义
- 工具接口规范
- 测试策略
- 评测集（`eval/`）

### 接口先行原则

模块间交互通过明确的接口定义（Pydantic Model / 抽象基类），先商定接口再各自实现，避免长期编辑同一文件产生冲突。
