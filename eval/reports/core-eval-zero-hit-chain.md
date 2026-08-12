# Core Eval 零命中链路修复实验记录

> 本记录只比较同一份 `eval/core_eval_v1.yaml` 的正式 5 × 2 Core Eval。冻结 gold、matcher、warning/confidence threshold、clean controls、fixture root cause、fail-closed 原则与 60k/80k 总 token budget 均未修改。

## 汇总

| Snapshot | Commit | 修改内容 | Deterministic Reject | Final Risk | Gold matched | Clean FP |
|---|---|---|---:|---:|---:|---:|
| Before | `8cab2fb` | 零命中审计后基线 | 4 | 0 | 0 | 0 |
| Fix 1 | 本提交（SHA 待提交后回填） | 暴露逐 finding/evidence 的确定性拒绝规则 | 2 | 1 | 1 | 0 |

Fix 1 的数字变化不能归因为诊断代码改善了命中链路：本提交没有改变任何通过/拒绝条件，但本轮模型输出与 Before 不同。Before 的四个 candidate run 都提交 warning 并进入 deterministic gate；Fix 1 中 pytest baseline 提交了 `info`，没有进入 verifier，而 pytest MergeWarden 本轮提交的 evidence 已满足旧 gate 并正常命中 gold。Fix 1 的可信改进仅是 deterministic rejection 变得可解释。

## Fix 1：deterministic rejection 逐规则诊断

- 目的：不放宽 gate，只让每个失败记录 candidate/finding、evidence role/index、retrieval source、文件/行号、失败字段、具体规则及 revised-finding 标记。
- Tests：57 个相关测试通过；`mypy src` 通过 76 个 source files；全仓 Ruff 通过；Core fixture audit 5/5 PASS。
- 正式运行：10/10 valid completion，10/10 首次 attempt 成功；candidate raw finding 4，进入 semantic verifier 3，semantic accepted 3，deterministic rejected 2，final risk 1，gold matched 1，clean-control warning/critical FP 0。
- 产物：`eval/outputs/core-eval-zero-hit-fix1.json`、`eval/reports/core-eval-zero-hit-fix1.md`，以及机器报告引用的 10 个 JSONL event logs。

### 本轮逐 case 结果

| Case | Raw / semantic | Deterministic / final | 结论 |
|---|---|---|---|
| Pydantic baseline | warning；semantic accepted | rejected；final 0 | trigger/impact 的 `tests/test_construction.py:336-358` 声称来自 diff，但对应 hunk 未进入 retained diff context |
| Pydantic MergeWarden | warning；semantic accepted | rejected；final 0 | verifier 返回 1 条非位置文本、1 条错误短路径；四类 structured evidence 引用的 `pydantic/main.py` span 未进入 retained diff context |
| pytest baseline | info；未路由 | deterministic 未检查；final 0 | 与 Before 的 warning 不同，属于本轮模型输出漂移，不是 gate 行为变化 |
| pytest MergeWarden | warning；semantic accepted | passed；final 1；gold 1 | 本轮提交的 finding/evidence 已满足既有 deterministic contract；命中 `SafeHashWrapper.__eq__` frozen gold |

### 具体 rejection records

| Finding | Evidence | Source / location | Rule |
|---|---|---|---|
| Pydantic baseline `00553735e81ee049` | trigger[0] | diff · `tests/test_construction.py:336-358` | `diff_evidence_context_missing` |
| Pydantic baseline `00553735e81ee049` | impact[0] | diff · `tests/test_construction.py:336-358` | `diff_evidence_context_missing` |
| Pydantic MergeWarden `6f74ac791b41247a` | verifier[1] | 非代码位置文本 | `evidence_location_missing` |
| Pydantic MergeWarden `6f74ac791b41247a` | verifier[2] | `test_construction.py:335` | `evidence_context_missing` |
| Pydantic MergeWarden `6f74ac791b41247a` | cause[0] | diff · `pydantic/main.py:408-418` | `diff_evidence_context_missing` |
| Pydantic MergeWarden `6f74ac791b41247a` | contract[0] | diff · `pydantic/main.py:405-418` | `diff_evidence_context_missing` |
| Pydantic MergeWarden `6f74ac791b41247a` | trigger[0] | diff · `pydantic/main.py:408-418` | `diff_evidence_context_missing` |
| Pydantic MergeWarden `6f74ac791b41247a` | impact[0] | diff · `pydantic/main.py:408-418` | `diff_evidence_context_missing` |

### 是否让命中链路前进

可观测性前进了一步：后续可以按 evidence item 精确区分主位置、缺失位置、未保留上下文、diff/read 来源、manifest id/hash、candidate binding 和 revised finding 重验失败。正式质量数字也出现了 1 个 pytest 命中，但由于 reviewer/verifier 输出 cohort 已变化，不能把这个命中归因给只读诊断改动；本提交不据此宣称 recall 改善。
