# MergeWarden

> AI PR Gatekeeper for Safer Merges

面向开发团队的 PR 守门 Agent，会审查代码变更、诊断 CI 失败原因，并帮助团队更安全地合并代码。

## 功能概述

- **PR 自动审查**：按严重级别分类问题（安全 / 正确性 / 风格 / 可维护性），支持行号引用与 diff hunk 定位
- **CI / Debug 辅助**：提取关键报错、推测失败原因、给出验证步骤与最小修复建议
- **合并门禁提示**：结合代码上下文、测试结果和工程约束，提示合并前风险与测试缺口

### 输入

- 本地仓库路径
- Git diff / PR patch
- 指定文件 + 错误日志 / 测试失败输出

### 输出

- 结构化 Review 报告（`severity` / `location` / `evidence` / `suggestion` / `confidence`）
- Debug 建议与最小修复 diff

## 快速开始

### 环境要求

- Python 3.11+
- OpenAI API Key（或兼容 API）

### 本地安装

```bash
# 克隆仓库
git clone https://github.com/<your-org>/mergewarden.git
cd mergewarden

# 创建虚拟环境并安装依赖
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 填入你的 API Key

# 运行
python cli.py review --help
python cli.py debug --help
```

### Docker

```bash
docker compose up --build
```

## 项目结构

```
├── src/
│   ├── analyzer/          # 核心分析引擎（Analyzer Agent 域）
│   │   ├── context_state.py       # 结构化状态管理
│   │   ├── inference_engine.py    # LLM 推理引擎
│   │   └── output_formatter.py    # 结构化输出格式化
│   ├── orchestrator/      # 5 阶段 Agent 编排层
│   │   └── agent_loop.py          # Agent 主循环
│   ├── tools/             # 工具系统（Integration Agent 域）
│   │   └── base.py                # 工具基类 + 注册机制
│   ├── security/          # 权限与沙箱
│   │   └── sandbox.py             # 沙箱执行
│   ├── models/            # 模型 / Provider 抽象层
│   │   └── client.py              # OpenAI 兼容客户端
│   └── config.py          # 全局配置
├── tests/                 # 测试
├── eval/                  # 评测集与评测脚本
│   └── fixtures/          # 评测用例
├── docs/                  # 项目文档（架构/契约/规划）
│   ├── architecture.md
│   ├── shared_contracts.md
│   ├── execute_tools_design.md  # execute 类工具设计与安全规范
│   ├── cli_tools_orchestrator_contract.md
│   ├── mvp_plus_roadmap.md
│   └── project_plan.md
├── cli.py                 # CLI 入口（Click）
├── agent.md               # Agent 开发约束与知识索引入口
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

### 架构分层

```
入口层    CLI (Click) · 可选 FastAPI 路由
   ↓
编排层    Agent 循环（5 阶段模式）
   ↓
工具层    Tool Calling（只读 / 写入 / 执行，分级权限）
   ↓
服务层    API 客户端 · 状态管理 · 上下文压缩
   ↓
模型层    OpenAI 兼容 API / Provider 抽象
   ↓
横切关注  配置 · 日志 · 结构化输出 · 成本追踪 · 权限
```

## 开发指南

### 环境搭建

```bash
pip install -r requirements-dev.txt
```

### 运行测试

```bash
pytest
```

### 代码风格

```bash
ruff check .          # lint
ruff format --check . # format check
mypy src/             # type check
```

项目使用 **全英文注释**，README 与用户文档使用中文。详细协作规范请参考 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 协作约定

本项目采用 PR + Issue 驱动的协作模式，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。
若使用 AI Agent 协作开发，请先阅读 [agent.md](agent.md)（渐进式知识索引与编码约束）。

## License

[MIT](LICENSE)
