# 评测方案

## 概述

本目录包含两类能力：

- 黄金集自建 pipeline（GitHub 自动发现仓库 + PR 解析 + LLM 辅助标注）
- 评测执行模块（对 Agent 输出计算格式合法率、命中率、误报率、人工可接受度模板、耗时/Token）

## 目录结构

```
eval/
├── crawler/
│   ├── github_client.py
│   ├── pr_parser.py
│   ├── annotator.py
│   └── fixture_generator.py
├── schemas.py
├── runner.py
├── metrics.py
├── report.py
├── run.py
├── fixtures/          # 评测用例（固定输入 + 期望输出元数据）
│   ├── manifest.json
│   └── review_checklist.md
├── outputs/           # 评测产出物（.gitignore 已忽略）
└── README.md
```

## 评测策略

### 主路径：自建黄金集（Golden Set）

- **素材来源**：自动发现小型活跃开源仓库，并筛选已合并 bugfix PR
- **缺陷来源**：PR 语义（fix/bug/security 等）+ LLM 辅助标注 expected issues
- **固定输入**：每条任务包含 diff / 相关文件片段 / 错误日志（可选），存入 `fixtures/`
- **期望行为**：
  - 检出类：输出命中目标问题类别
  - 结构类：结构化输出通过 JSON Schema 校验
  - 定位类：指向正确文件路径或行号范围

### 指标

| 指标 | 说明 |
|------|------|
| 格式合法率 | 输出通过结构化 schema 校验的比例 |
| 关键问题命中率 | 成功检出预设问题的比例 |
| 误报率 | 非预设问题的报告比例 |
| 人工可接受度 | 人工 spot-check 评分 |
| 耗时 / Token | 工程回归指标 |

### Review target semantics

For review fixtures, the primary input is the pull request diff. The temporary
sandbox contains the files from the fixture as post-diff context so the agent can
read surrounding code when needed. Expected issues should target problems that
the submitted diff introduces, exposes, or fails to fix; the eval is not a
general audit of the pre-diff repository.

When `diff_mode=True`, the eval measures review quality for the submitted diff.
File reads are contextual evidence only.

### 补充维度：公开 benchmark

在黄金集跑通后，可从 SWE-bench 等公开数据中抽取少量实例做外推验证。子集规模、筛选规则需写入评测说明，与主评测通过/失败口径分开汇报。

## 运行

```bash
# 1) 自动抓取并生成 fixture（需要 GITHUB_TOKEN）
python -m eval.run crawl --max-repos 5 --max-prs-per-repo 3

# 2) 跑评测（调用 AgentOrchestrator）
python -m eval.run eval --suite golden

# 3) 基于已有报告重新渲染终端输出
python -m eval.run report --input eval/outputs/<timestamp>_report.json
```

## 产物说明

- `eval/fixtures/manifest.json`：fixture 索引
- `eval/fixtures/review_checklist.md`：人工审核清单（用于修正 LLM 草稿）
- `eval/outputs/*_report.json`：机器可读评测报告
- `eval/outputs/*_human_review.md`：人工可接受度打分模板（0-5）
