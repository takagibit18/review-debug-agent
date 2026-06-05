# GitHub Action 自托管安装路径

这份指南是 MergeWarden 面向中文用户的 10 分钟接入路径：不用发布包，不用托管服务，也不用把 MergeWarden 源码复制进业务仓库。目标仓库只需要复制一个 workflow，并配置必要的 GitHub secret / repository variable。

workflow 会 checkout 两个仓库：

- `target`：被审查的 PR 仓库。
- `.mergewarden/runtime`：MergeWarden runtime 仓库，里面包含 `cli.py`、`requirements-dev.txt` 和 helper scripts。

MergeWarden 只做 advisory：它会发布 neutral check run，并在 changed lines 上写可更新的建议评论；GitHub CI 和 branch protection 仍然是能否合并的硬性裁决。

## 1. 添加 workflow

在需要接入 MergeWarden 的目标仓库中，把模板复制到 `.github/workflows/mergewarden-advisory.yml`：

```bash
mkdir -p .github/workflows
cp path/to/mergewarden/docs/examples/github-advisory-self-hosted.yml \
  .github/workflows/mergewarden-advisory.yml
```

如果不是从本地 clone 复制，而是直接从 GitHub 复制，请下载你的 MergeWarden fork 或 release branch 中的 `docs/examples/github-advisory-self-hosted.yml`。

## 2. 配置 GitHub

添加一个 repository secret：

| 名称 | 值 |
| --- | --- |
| `OPENAI_API_KEY` | MergeWarden 调用模型所需的 OpenAI-compatible API key |

添加一个 repository variable：

| 名称 | 值 |
| --- | --- |
| `MERGEWARDEN_REPOSITORY` | MergeWarden runtime 仓库，格式为 `owner/mergewarden` |

可选 repository variables：

| 名称 | 默认值 | 何时修改 |
| --- | --- | --- |
| `MERGEWARDEN_REF` | `main` | 固定到 release tag 或稳定分支 |
| `MODEL_NAME` | `gpt-4o` | 使用其他 OpenAI-compatible 模型 |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 使用兼容 OpenAI API 的其他服务 |
| `REVIEW_MAX_ITERATIONS` | `4` | 控制审查循环次数 |
| `TOKEN_BUDGET` | `88000` | 控制 finalize-only 前的软预算 |
| `PROMPT_INPUT_TOKEN_BUDGET` | `88000` | 控制 prompt 输入截断预算 |

如果 MergeWarden runtime 仓库是私有仓库，还需要添加 `MERGEWARDEN_REPOSITORY_TOKEN` secret，并授予它读取 runtime 仓库的权限。

## 3. 打开测试 PR

在目标仓库中新建一个分支，做一个小改动，并打开同仓库 PR。这个路径会跑完整闭环：

1. 生成 `pr.diff`。
2. 生成 `changed_lines.json`。
3. 运行 `review . --diff`。
4. dry-run GitHub advisory payload。
5. 发布 neutral check run 和 changed-line review comments。
6. 上传 review JSON、run summary、changed-lines map、PR diff、publish result 和 event logs。

Fork PR 会运行 review 和 dry-run publish，然后把 dry-run 结果作为 artifact 保存；默认不会写 PR 评论，因为 GitHub 对 fork PR 的 `GITHUB_TOKEN` 权限有意收紧。

## 预期结果

第一次成功运行后，GitHub Actions 中应看到这些步骤完成：

- `运行 MergeWarden 审查`
- `Dry-run GitHub advisory 发布`
- 同仓库 PR：`发布 GitHub advisory`
- Fork PR：`保留 fork PR 的 dry-run 结果`
- `上传 MergeWarden 产物`

上传的 artifact 名称为 `mergewarden-advisory-<pr-number>`，包含：

- `review_response.json`
- `run_summary.json`
- `changed_lines.json`
- `pr.diff`
- `publish_dry_run.json`
- `publish_result.json`
- `event_logs/*.jsonl`

## 排障表

| 现象 | 处理方式 |
| --- | --- |
| `请设置 repository variable MERGEWARDEN_REPOSITORY` | 添加 repository variable，例如 `owner/mergewarden`。 |
| runtime 仓库 checkout 失败 | 如果 runtime 是私有仓库，添加 `MERGEWARDEN_REPOSITORY_TOKEN`。 |
| 审查被跳过 | 添加 `OPENAI_API_KEY` repository secret。 |
| 没有 inline 评论 | 查看 `publish_result.json`；无法定位到 changed line 的发现会保留在 neutral check summary。 |
| Fork PR 不发布评论 | 这是预期行为；fork 默认只跑 dry-run，除非你另外设计信任策略。 |
| 获取 base branch 失败 | 确认 base branch 存在，并且 workflow 有 `contents: read` 权限。 |

