# MergeWarden

MergeWarden 是面向工程团队的 AI PR 守门员。它会阅读 Pull Request 的代码变更、测试结果与 CI 日志，输出可追溯的审查结论、风险提示和最小修复建议，帮助团队在合并前更快发现隐藏问题。

[查看产品展示页](https://merge-warden.vercel.app/)

## 产品定位

MergeWarden 不替代 GitHub CI、分支保护或人工评审。它作为合并前的智能辅助层，默认以建议、软检查和结构化证据的形式参与 PR 流程，让团队在不增加硬性阻塞的情况下获得更稳定的代码审查反馈。

适合以下场景：

- 希望在 PR 合并前补充 AI 风险审查。
- 需要快速定位 CI 失败原因并获得可执行修复建议。
- 想把代码审查结果沉淀为结构化报告、检查摘要和可复盘证据。
- 需要在 GitHub Actions 中发布非阻塞式建议检查。

## 核心能力

### PR 自动审查

MergeWarden 会围绕安全性、正确性、可维护性和工程规范审查 PR diff，并尽量把问题定位到变更行或变更块。审查结果包含严重级别、位置、证据、建议和置信度，便于开发者直接处理。

### CI 失败诊断

MergeWarden 可以读取测试失败输出和 CI 日志，提取关键报错，推断可能原因，并给出验证步骤与最小修复方向。它的目标不是生成冗长解释，而是帮助开发者尽快找到下一步。

### 合并风险提示

MergeWarden 会结合代码上下文、测试覆盖与工程约束，提示合并前仍需关注的风险，例如测试缺口、未覆盖路径、潜在兼容性问题或需要人工确认的边界条件。

### GitHub 建议式发布

MergeWarden 支持将审查结果发布为 GitHub 软检查和可选 PR 评论。默认试运行，正式发布时也保持仅建议模式：GitHub CI 与分支保护仍然是最终合并权限来源。

## 输入与输出

MergeWarden 支持以下输入：

- 本地仓库路径
- Git diff 或 PR patch
- 指定文件、错误日志或测试失败输出
- GitHub Actions 中生成的 PR diff 与 changed-lines 映射

MergeWarden 输出以下内容：

- 结构化 Review 报告
- CI 失败诊断建议
- 最小修复方向或 diff 建议
- GitHub 建议式软检查摘要
- 可追踪的事件日志与运行摘要

## 快速开始

### 环境要求

- Python 3.11 或更高版本
- OpenAI API Key 或兼容 OpenAI API 的模型服务

### 本地安装

```bash
git clone https://github.com/<your-org>/mergewarden.git
cd mergewarden

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

Windows PowerShell 可使用以下方式启用虚拟环境：

```powershell
.\.venv\Scripts\Activate.ps1
```

编辑 `.env` 后填入模型服务凭据，然后运行：

```bash
python cli.py review --help
python cli.py debug --help
```

### Docker 运行

```bash
docker compose build agent
docker compose run --rm agent python cli.py review --help
docker compose run --rm agent python cli.py debug --help
```

Docker compose 会读取 `.env`，并将当前仓库挂载到容器的 `/app` 目录。实际审查或诊断时，将 `--help` 替换为目标路径和需要的参数即可。

### FastAPI 服务

MergeWarden 提供同步 HTTP 接口，复用 CLI 的编排流程和 Pydantic 契约。

```bash
uvicorn src.api.app:app --reload
```

可用接口：

- `GET /health`
- `POST /review`
- `POST /debug`
- `GET /runs/{run_id}/summary`

## GitHub Actions 集成

仓库包含 `.github/workflows/github-advisory.yml`，用于在 PR 流程中运行 MergeWarden：

1. 生成 PR diff。
2. 生成 `changed_lines.json`。
3. 执行 `review --diff --output-json --summary-json`。
4. 默认执行建议式发布试运行。
5. 在具备写权限的同仓库 PR 中发布软检查和可选评论。

发布到 GitHub 时，需要为 `GITHUB_TOKEN` 授权：

- `checks: write`
- `pull-requests: write`

示例命令：

```bash
python scripts/github_changed_lines.py --diff-file pr.diff --output changed_lines.json
python cli.py review . --diff --output-json review_response.json --summary-json run_summary.json
python cli.py github-advisory publish --repo owner/repo --pr-number 123 --head-sha "$GITHUB_SHA" --response-json review_response.json --changed-lines-json changed_lines.json --dry-run
```

内联评论仅发布到变更行；无法落到变更行的问题会保留在软检查摘要中。MergeWarden 会为评论写入隐藏指纹，以便后续运行更新匹配评论并标记过期发现，同时避免覆盖人工评论。

## 项目结构

```text
src/
  analyzer/        核心分析引擎
  orchestrator/    Agent 编排流程
  tools/           工具系统与注册机制
  security/        权限与沙箱执行
  models/          OpenAI 兼容模型客户端
  api/             FastAPI 服务
tests/             自动化测试
eval/              评测集与评测脚本
docs/              架构、契约与规划文档
scripts/           工程脚本
cli.py             CLI 入口
agent.md           Agent 开发约束与知识索引入口
Dockerfile         CLI 容器镜像
docker-compose.yml Docker compose 配置
requirements.txt   运行时依赖
```

## 开发命令

安装开发依赖：

```bash
pip install -r requirements-dev.txt
```

运行测试：

```bash
pytest
```

运行代码质量检查：

```bash
ruff check .
ruff format --check .
mypy src/
```

如需执行 Docker 后端 smoke test：

```bash
docker build -f Dockerfile.execute -t mergewarden-execute:latest .
RUN_DOCKER_TESTS=1 pytest -q tests/test_docker_backend_smoke.py -rs
```

## 协作约定

MergeWarden 采用 PR 与 Issue 驱动的协作方式。代码、文档和审查流程请参考 [贡献指南](CONTRIBUTING.md)。

使用 AI Agent 协作开发前，请先阅读 [agent.md](agent.md)，其中包含渐进式知识索引、编码约束和审查要求。

## 许可证

[MIT](LICENSE)
