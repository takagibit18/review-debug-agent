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

## 产品边界

MergeWarden 是 advisory PR reviewer，不是 hard merge gate：

- 不自动批准或拒绝 PR。
- 不替代已有 CI、测试、代码所有者审批或 branch protection。
- 不在默认路径中修改用户代码。
- inline 评论只落在 changed lines；无法映射到 changed line 的发现会留在 check summary。

## 核心能力

### PR 自动审查

MergeWarden 会围绕安全性、正确性、可维护性和工程规范审查 PR diff，并尽量把问题定位到变更行或变更块。审查结果包含严重级别、位置、证据、建议和置信度，便于开发者直接处理。

### CI 失败诊断

MergeWarden 可以读取测试失败输出和 CI 日志，提取关键报错，推断可能原因，并给出验证步骤与最小修复方向。它的目标不是生成冗长解释，而是帮助开发者尽快找到下一步。

### 合并风险提示

MergeWarden 会结合代码上下文、测试覆盖与工程约束，提示合并前仍需关注的风险，例如测试缺口、未覆盖路径、潜在兼容性问题或需要人工确认的边界条件。

### GitHub 建议式发布

MergeWarden 支持将审查结果发布为 GitHub soft check 和可选 PR 评论。默认先 dry-run，正式发布时也保持仅建议模式：GitHub CI 与 branch protection 仍然是最终合并权限来源。

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
- GitHub 建议式 soft check 摘要
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

如需执行 Docker execute backend smoke test：

```bash
docker build -f Dockerfile.execute -t mergewarden-execute:latest .
RUN_DOCKER_TESTS=1 pytest -q tests/test_docker_backend_smoke.py -rs
```

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

### Platform MVP

GitHub App mode now has a minimal persistent backend layer: SQLite-backed
installations/repositories/runs, DB webhook idempotency, a polling worker,
local run artifacts, tenant config resolution, and `/platform/*` management
APIs for local/internal validation.

Webhook handling only queues review runs; start `python cli.py platform worker`
as a separate process to execute them.

See [docs/platform_mvp.md](docs/platform_mvp.md) for setup, worker commands,
mock webhook testing, artifact paths, and current production gaps.

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

## GitHub Actions 集成

当前仓库包含 `.github/workflows/github-advisory.yml`，用于在 MergeWarden 自己的 PR 流程中运行 advisory 审查：

1. 生成 PR diff。
2. 生成 `changed_lines.json`。
3. 执行 `review --diff --output-json --summary-json`。
4. 默认执行建议式发布 dry-run。
5. 在具备写权限的同仓库 PR 中发布 soft check 和可选评论。

发布到 GitHub 时，需要为 `GITHUB_TOKEN` 授权：

- `checks: write`
- `pull-requests: write`

示例命令：

```bash
python scripts/github_changed_lines.py --diff-file pr.diff --output changed_lines.json
python cli.py review . --diff --output-json review_response.json --summary-json run_summary.json
python cli.py github-advisory publish --repo owner/repo --pr-number 123 --head-sha "$GITHUB_SHA" --response-json review_response.json --changed-lines-json changed_lines.json --dry-run
```

内联评论仅发布到变更行；无法落到变更行的问题会保留在 soft check 摘要中。MergeWarden 会为评论写入隐藏指纹，以便后续运行更新匹配评论并标记过期发现，同时避免覆盖人工评论。

## 常用命令

```bash
python cli.py review . --diff --output-json review_response.json --summary-json run_summary.json
python scripts/github_changed_lines.py --diff-file pr.diff --output changed_lines.json
python cli.py advisory-export --response-json review_response.json --changed-lines-json changed_lines.json
python cli.py github-advisory publish --repo owner/repo --pr-number 123 --head-sha "$GITHUB_SHA" --response-json review_response.json --changed-lines-json changed_lines.json --dry-run
python -m eval.core_eval audit
python -m eval.core_eval run
python -m eval.run diagnose --input eval/outputs/20260518_151719_report.json
python -m eval.run trend --inputs "eval/outputs/*_report.json"
```

## Core Eval v1

当前主评测是一个 5 个 real-world full-workspace PR 组成的 **small curated evaluation set**：2 个带人工复核 gold finding 的 candidate，加 3 个已稳定的零问题 controls。A/B 在相同模型、预算、fixture 和 deterministic one-to-one judge 下各跑一次；仅 runtime instability 才重试。

契约修复后的 `deepseek-v4-pro` 新基线共运行 15 个 attempts：simple baseline valid completion rate 为 `83.3%`，MergeWarden 为 `33.3%`。MergeWarden 在两个 positive fixtures 上连续 6 次都在 hard token cap 后、`submit_review` 前结束，因此没有可用于 Review Quality 的 valid candidate completion，A/B Precision/Recall/F1 暂不可比较。Baseline 的两个 candidate runs 均 valid，但未命中 gold；3 个 controls 上两侧均没有 warning/critical false finding。Workspace 与 validator failure 均为 0。

新基线表格、per-fixture 对比和契约修复前的逐条审计见 [eval/reports/core-eval-v1.md](eval/reports/core-eval-v1.md) 与 [eval/reports/core-eval-v1-finding-audit.md](eval/reports/core-eval-v1-finding-audit.md)。

## MVP+ 历史状态

当前版本的 MVP+ 工程范围已闭环：CLI、FastAPI、Docker CLI demo、event logs、workspace-backed golden fixtures、GitHub Actions advisory 模板和稳定 eval gate 都已落地。

历史 MVP+ golden baseline 是 `eval/outputs/20260518_151719_report.json`：

- schema validity：`1.0`
- hit rate：`0.75`
- false positive rate：`0.0`

它通过当前稳定门槛 `schema_validity_rate >= 1.0`、`hit_rate >= 0.6`、`false_positive_rate <= 0.5`。细节见 [docs/mvp_plus_eval_closure.md](docs/mvp_plus_eval_closure.md)。

## v0.2.0：语义验真与可恢复审查

v0.2.0 在现有 advisory review 链路上增加四项保障：

- Warning/Critical 先形成稳定 `FindingCandidate`，再由独立 verifier 逐条给出 `accepted/rejected/needs_evidence/downgraded`。GA 默认 `FINDING_VERIFIER_MODE=enforce`，无有效 verdict 时 fail closed。
- `ReviewWorkflowTracker` 强制完成 diff、changed context、draft validation、semantic verification 和 finalize；缺失上下文最多补救一次，仍缺失时不发布风险 finding。
- Platform worker 使用 SQLite lease、heartbeat 和 step checkpoint；过期 `running` run 可回收到队列，GitHub check 使用稳定 `external_id` 更新而非重复创建。
- Eval 报告增加证据绑定率、verifier 接受/拒绝率、required-step 完成率、重复工具调用率和每条 accepted finding 的 token 成本，并可与冻结 baseline 比较。

完整版本与验收规划见 [docs/v0.2.0_reliability_quality_plan.md](docs/v0.2.0_reliability_quality_plan.md)。

## 项目结构

```text
src/
  analyzer/          核心分析、上下文、结构化输出、运行摘要
  orchestrator/      5 阶段 Agent 编排
  tools/             read-only / execute 工具体系
  security/          执行策略、沙箱、Docker backend
  models/            OpenAI-compatible provider 抽象
  integrations/      GitHub advisory payload、webhook 与发布
  api/               FastAPI 同步薄层
eval/                golden fixtures、runner、metrics、diagnostics
tests/               单元测试与回归测试
docs/                架构、契约、路线图、安装指南
scripts/             工程脚本
marketing/           静态营销页
marketing-vercel/    Vercel 静态营销页
cli.py               Click CLI 入口
agent.md             Agent 开发约束与知识索引入口
Dockerfile           CLI 容器镜像
docker-compose.yml   Docker compose 配置
requirements.txt     运行时依赖
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

代码注释默认使用英文；面向用户的 README、安装文档、营销文案和 GitHub Action happy path 默认使用中文。

## 协作约定

MergeWarden 采用 PR 与 Issue 驱动的协作方式。代码、文档和审查流程请参考 [贡献指南](CONTRIBUTING.md)。

使用 AI Agent 协作开发前，请先阅读 [agent.md](agent.md)，其中包含渐进式知识索引、编码约束和审查要求。

## 许可证

[MIT](LICENSE)

## v0.2.3–v0.2.5：根因级 finding 与 change-centered 代码图

Review 流水线现在先输出带因果机制、不变量、最小修复签名和分角色 provenance 的 finding hypothesis。单条 finding 通过 evidence verifier 后，系统按确定性 blocking 形成候选组，只在共同机制、共同不变量和共同 repair unit 同时成立且反事实结果明确为 `yes` 时归并；独立的 consolidation verifier 拒绝不安全归并并恢复原 findings。

系统还会从 changed hunk/symbol 构建带 qualified identity、resolver、confidence 和 evidence eligibility 的代码关系图，通过预算化 Context Planner 生成与实际 Reviewer prompt 一致的 Candidate Context Manifest。SQLite 索引按 file hash 增量更新；LSP enrichment 可选且不是运行硬依赖。

完整架构、schema、配置、迁移、provenance、benchmark 和限制见 [v0.2.3–v0.2.5 根因级审查文档](docs/v023_v025_root_cause_relation_graph.md)。
