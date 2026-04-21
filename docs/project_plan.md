# Code Review & Debug Agent — 开源项目规划（意图对齐 · 架构 · 流程 · 分工）

> 本文档由根目录 `code_review_debug_agent_plan.md` 迁移至 `docs/`，用于统一开发指导文档入口。
> 面向：大三 CS，求职 Agent 开发 / LLM 应用工程师；2 人远程协作并强化工程化能力。
>
> **MVP+ 增量**（相对当前 MVP 的优化清单与评测辅助指标）：见 [mvp_plus_roadmap.md](mvp_plus_roadmap.md)。

---

## 1. 意图对齐

### 1.1 项目目标

- 构建可部署的 LLM Agent：对本地仓库或 PR 代码做结构化 Review，并辅助 Debug。
- 在项目实践中覆盖：Tool Calling、多步推理、评测、Docker、CI、协作流程。
- 通过可复现工程资产（文档、Issue、PR、评测）形成可展示的简历项目。

### 1.2 MVP 产品形态

- 首版以 CLI 为核心，不优先做 IDE 插件或完整平台。
- 输入：仓库路径、diff/patch、指定文件与报错日志。
- 输出：
  - Review：`severity/location/evidence/suggestion/confidence`
  - Debug：假设、验证步骤、最小修复建议

### 1.3 非目标

- 不承诺自动修复所有问题并直接通过 CI。
- 首期不强制多智能体复杂编排，优先单 Agent + 清晰工具体系。

### 1.4 成功标准

- 一条命令可跑通 demo（本地或 Docker）。
- 至少 1 条端到端场景（输入缺陷代码 + 错误信息，输出结构化建议）。
- 具备最小评测集与协作痕迹（Issue/PR/规范化提交）。

---

## 2. 路线与选型

| 方案 | 描述 | 适用阶段 |
|------|------|----------|
| A | CLI 核心（review/debug 子命令） | MVP 首选 |
| B | FastAPI 薄服务层 | MVP+ |
| C | GitHub Action/Bot（PR 自动评论） | Phase 2 |
| D | IDE 插件 | 有余力再做 |

推荐路径：A →（薄）B → C。

---

## 3. 技术框架

### 3.1 分层

- 入口层：CLI（可选 FastAPI）
- 编排层：5 阶段 Agent 循环
- 工具层：Tool Calling（readonly/write/execute）
- 服务层：状态管理、上下文压缩、可观测性
- 模型层：OpenAI 兼容客户端与 Provider 抽象
- 横切关注：配置、日志、成本追踪、权限管理

### 3.2 关键工程点

- 安全：执行型工具必须沙箱化（超时、工作目录限制、审计信息）。
- 可复现：保留 run_id、工具调用序列与关键中间结果。
- 可评测：固定输入输出结构，支持回归对比。
- 状态管理：通过 `ContextState` 跟踪目标、约束、决策与错误。

### 3.3 Agent 友好能力（MVP 必做）

1. 工具 schema 化：所有工具输入输出可结构化验证。
2. 5 阶段编排：prepare → analyze → execute → process → continue/stop。
3. 按需上下文加载：默认 diff + 相关片段，必要时扩展。
4. 结构化输出：统一报告模型，便于 CLI/API/CI 消费。
5. 工具并发策略：只读可并发，写入串行，执行隔离。
6. 失败可恢复：工具/模型异常时返回结构化降级结果。

### 3.4 评测策略（黄金集优先）

- 主路径：自建 Golden Set（10-30 任务），固定 diff/日志/期望。
- 指标：格式合法率、命中率、误报率、人工可接受度、耗时与 token。
- 补充：公开 benchmark 仅做小样本外推，不与主评测口径混用。

---

## 4. 协作流程（2 人远程）

### 4.1 Git / GitHub

- `main` 受保护，仅 PR 合并。
- 分支命名：`feat/...`、`fix/...`、`chore/...`。
- PR 需包含动机、变更、关联 Issue、测试说明。

### 4.2 迭代节奏（建议）

| 周次 | 里程碑 |
|------|--------|
| W1 | 仓库骨架、CLI 调模型、基础 review 路径 |
| W2 | 读文件/diff 接入、debug 原型、Docker |
| W3 | 工具执行策略、评测 v0、文档完善 |
| W4+ | FastAPI 薄层或 GitHub 集成（二选一） |

---

## 5. 分工与协作边界

### 5.1 功能模块型分工

- Analyzer Agent：
  - 负责 `src/analyzer/`、`src/orchestrator/`、`src/models/`
  - 聚焦分析链路、状态管理、结构化输出
- Integration Agent：
  - 负责 `src/tools/`、`src/security/`、`cli.py`、部署集成
  - 聚焦工具调用、CLI 交互、安全约束与工程化

### 5.2 共同维护区域

- Prompt 模板
- JSON Schema 与输出协议
- 工具接口规范
- 测试策略
- 评测集（`eval/`）

### 5.3 协作机制

- 接口先行：先定契约，再并行实现。
- 双人结对：关键接口处共同评审与调试。
- 周度同步：固定节奏复盘进展与风险。

---

## 6. 待确认项（当前共识）

- MVP 入口：仅 CLI。
- 默认模型：云端 API。
- Review 维度：不强制 CWE/性能分类，但可给出相关建议。
- Debug 执行：允许在受控环境下运行测试/验证命令（当前默认主机 `subprocess` 硬化；容器内执行见 [execute_tools_design.md](execute_tools_design.md) 与路线图「Docker 后端」待办），保留超时与目录限制。
- 许可证：MIT。
- 展示语言：中文 README + 英文代码注释。

---

## 7. 下一步

- 对齐 `docs/shared_contracts.md` 中的接口协议，再拆解 Issue 与 Milestone，按周迭代落地。
- execute 类工具的设计与安全规范见 [execute_tools_design.md](execute_tools_design.md)；MVP+ 中 Docker 后端与容器跑测的最终对齐见 [mvp_plus_roadmap.md](mvp_plus_roadmap.md) §3.1「工具与安全」。
- MVP+ 阶段增量（评测辅助指标、Analyzer/编排补强等）集中记录在 [mvp_plus_roadmap.md](mvp_plus_roadmap.md)，与本文 §2 路线表对照使用。
