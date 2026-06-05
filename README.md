# MergeWarden

> 面向开发团队的 AI PR 守门员

MergeWarden 会审查 PR 代码变更、辅助诊断 CI 失败原因，并补充 CI 难以覆盖的合并风险判断。它默认提供建议、neutral soft check、 changed-line review comments 和可复盘证据，不替代 GitHub CI / branch protection 对“能否合并”的硬性裁决。

## 功能概览

- **PR 自动审查**：按严重级别分类问题，输出 `severity` / `location` / `evidence` / `suggestion` / `confidence`，并优先定位到 changed line / changed hunk。
- **CI / Debug 辅助**：结合错误日志、测试输出和代码上下文，给出失败假设、验证步骤和最小修复建议。
- **GitHub Actions advisory**：在 PR 上发布 neutral check run，并只在 changed line 上写可更新、可标记 stale 的建议评论。
- **可观测闭环**：每次运行生成 `run_id`、结构化响应、run summary、changed-lines map、PR diff、publish result 和事件日志。
- **评测与回归**：内置 golden fixtures、schema / hit rate / false positive gate、诊断报告和趋势分析。

## 产品边界

MergeWarden 是 **advisory PR reviewer**，不是 hard merge gate：

- 不自动批准或拒绝 PR。
- 不替代已有 CI、测试、代码所有者审批或 branch protection。
- 不在默认路径中修改用户代码。
- inline 评论只落在 changed lines；无法映射到 changed line 的发现会留在 check summary。

## 快速开始

### 环境要求

- Python 3.11+
- OpenAI API Key 或 OpenAI-compatible API Key

### 本地安装

```bash
git clone https://github.com/<your-org>/mergewarden.git
cd mergewarden

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY / OPENAI_BASE_URL / MODEL_NAME

python cli.py review --help
python cli.py debug --help
```

### Docker

```bash
docker compose build agent
docker compose run --rm agent python cli.py review --help
docker compose run --rm agent python cli.py debug --help
```

Compose 服务会读取 `.env`，并把当前仓库挂载到 `/app`。真实 review/debug 运行前，先复制 `.env.example` 为 `.env` 并填入模型 API 配置。

Docker execute backend 的 smoke test 默认手动运行：

```bash
docker build -f Dockerfile.execute -t mergewarden-execute:latest .
RUN_DOCKER_TESTS=1 pytest -q tests/test_docker_backend_smoke.py -rs
```

### FastAPI

MVP+ 提供同步 FastAPI 薄层，复用 CLI 的编排器和 Pydantic 请求/响应模型。

```bash
uvicorn src.api.app:app --reload
```

可用接口：

- `GET /health`
- `POST /review`
- `POST /debug`
- `GET /runs/{run_id}/summary`

## 10 分钟接入 GitHub Action 自托管审查

这是面向中文用户的推荐 happy path：目标仓库只需要复制一个 workflow，配置一个 secret 和一个 repository variable，不需要把 MergeWarden 源码 vendoring 到业务仓库。

1. 把 [docs/examples/github-advisory-self-hosted.yml](docs/examples/github-advisory-self-hosted.yml) 复制到目标仓库的 `.github/workflows/mergewarden-advisory.yml`。
2. 在目标仓库添加 secret：`OPENAI_API_KEY`。
3. 在目标仓库添加 repository variable：`MERGEWARDEN_REPOSITORY=owner/mergewarden`，指向 MergeWarden runtime 仓库。
4. 可选：设置 `MERGEWARDEN_REF` 固定到某个 release branch 或 tag。若 runtime 仓库是私有仓库，再添加 `MERGEWARDEN_REPOSITORY_TOKEN`。
5. 在同仓库开一个测试 PR。成功后会看到 neutral check run、changed-line 评论，以及 `mergewarden-advisory-<pr-number>` artifact。

模板会双 checkout：

- `target`：被审查的 PR 仓库。
- `.mergewarden/runtime`：包含 `cli.py`、`requirements-dev.txt` 和 helper scripts 的 MergeWarden runtime 仓库。

同仓库 PR 会发布 neutral check run 和 changed-line review comments。Fork PR 默认只运行 review + dry-run publish，并把 dry-run 结果上传为 artifact，这是 GitHub 默认权限模型下更安全的路径。

完整安装说明和排障表见 [docs/github_action_self_hosted.md](docs/github_action_self_hosted.md)。

## 常用命令

```bash
python cli.py review . --diff --output-json review_response.json --summary-json run_summary.json
python scripts/github_changed_lines.py --diff-file pr.diff --output changed_lines.json
python cli.py advisory-export --response-json review_response.json --changed-lines-json changed_lines.json
python cli.py github-advisory publish --repo owner/repo --pr-number 123 --head-sha "$GITHUB_SHA" --response-json review_response.json --changed-lines-json changed_lines.json --dry-run
python -m eval.run diagnose --input eval/outputs/20260518_151719_report.json
python -m eval.run trend --inputs "eval/outputs/*_report.json"
```

## MVP+ 状态

当前版本的 MVP+ 工程范围已闭环：CLI、FastAPI、Docker CLI demo、event logs、workspace-backed golden fixtures、GitHub Actions advisory 模板和稳定 eval gate 都已落地。

当前记录的 golden baseline 是 `eval/outputs/20260518_151719_report.json`：

- schema validity：`1.0`
- hit rate：`0.75`
- false positive rate：`0.0`

它通过当前稳定门槛 `schema_validity_rate >= 1.0`、`hit_rate >= 0.6`、`false_positive_rate <= 0.5`。细节见 [docs/mvp_plus_eval_closure.md](docs/mvp_plus_eval_closure.md)。

## 项目结构

```text
src/
  analyzer/          # 核心分析、上下文、结构化输出、运行摘要
  orchestrator/      # 5 阶段 Agent 编排
  tools/             # read-only / execute 工具体系
  security/          # 执行策略、沙箱、Docker backend
  models/            # OpenAI-compatible provider 抽象
  integrations/      # GitHub advisory payload 与发布
  api/               # FastAPI 同步薄层
eval/                # golden fixtures、runner、metrics、diagnostics
tests/               # 单元测试与回归测试
docs/                # 架构、契约、路线图、安装指南
marketing/           # 静态营销页
marketing-vercel/    # Vercel 静态营销页
cli.py               # Click CLI 入口
```

## 架构分层

```text
入口层        CLI / FastAPI
编排层        5 阶段 Agent loop：prepare -> analyze -> execute -> process -> continue/stop
工具层        只读工具、执行工具、结构化 tool schemas
服务层        上下文管理、结果处理、运行摘要、事件日志
模型层        OpenAI-compatible API / provider abstraction
横切关注      配置、权限、成本与 token、结构化输出、可观测性
```

## 开发

```bash
pip install -r requirements-dev.txt
pytest
ruff check .
ruff format --check .
mypy src/
```

代码注释默认使用英文；面向用户的 README、安装文档、营销文案和 GitHub Action happy path 默认使用中文。

## 协作约定

项目采用 PR + Issue 驱动的协作模式，详见 [CONTRIBUTING.md](CONTRIBUTING.md)。使用 AI Agent 协作开发前，请先阅读 [agent.md](agent.md)。

## License

[MIT](LICENSE)
