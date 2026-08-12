# Core Eval 零命中链路修复实验记录

> 本记录比较相同 5 fixtures × 2 variants、模型与质量合同的正式 Core Eval。冻结 gold、matcher、warning/confidence threshold、clean controls、fixture root cause、fail-closed 原则与 60k/80k 总 token budget 均未修改。按用户 2026-08-12 的经济性要求，Fix 3 起关闭不稳定自动补采并固定 `max_attempts=1`；此前正式快照也都实际在首次 attempt 完成，因此各行都恰好包含 10 个 measured attempts。

## 汇总

| Snapshot | Commit | 修改内容 | Deterministic Reject | Final Risk | Gold matched | Clean FP |
|---|---|---|---:|---:|---:|---:|
| Before | `8cab2fb` | 零命中审计后基线 | 4 | 0 | 0 | 0 |
| Fix 1 | `ffc4467` | 暴露逐 finding/evidence 的确定性拒绝规则 | 2 | 1 | 1 | 0 |
| Fix 2 | `69355f1` | 为 finding 实际引用的全部位置保留 bounded verifier context | 1 | 1 | 1 | 0 |
| Fix 3 | 本提交（SHA 待提交后回填） | 从可信上下文绑定 evidence 来源与 canonical candidate ID | 2 | 1 | 1 | 0 |

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

## Fix 2：保留 finding 全部 evidence locations

- 目的：按 `主位置 → cause → contract → trigger → impact → related` 收集每个 finding 明确引用位置的 diff hunk、成功 file window、symbol context 与 Manifest span/path，同时维持 12k bounded envelope 和 fail-closed。
- Tests：全量 `pytest` 636 passed / 1 skipped；`mypy src`、全仓 Ruff、touched-file format、Core audit 与 `git diff --check` 全部通过。
- 正式运行：这是本提交唯一正式轮次；10/10 valid、10/10 首次 attempt；candidate raw finding 3，semantic accepted 2，deterministic rejected 1，final risk 1，gold matched 1，clean-control warning/critical FP 0。
- pytest：MergeWarden finding 的跨位置证据完整保留，deterministic passed、final 1、gold 1；baseline 的 impact 声称来自未实际读取的 `src/_pytest/fixtures.py:308-312`，继续 fail closed。
- Pydantic：baseline 在 semantic verifier 被判不受支持；MergeWarden 本轮没有 finding。没有为了 gold score 修改 matcher 或标签。
- 产物：`eval/outputs/core-eval-zero-hit-fix2.json`、`eval/reports/core-eval-zero-hit-fix2.md` 及报告引用的 10 个 JSONL event logs。两个在正式轮次前发现预算顺序缺陷的矩阵分别保留为 `core-eval-zero-hit-fix2-preflight` 与 `core-eval-zero-hit-fix2-source-order-preflight`，不写入正式对比行。

### 是否让命中链路前进

是。通用回归与真实 pytest MergeWarden case 都证明后续 evidence location 不再因围绕主位置的裁剪而消失；deterministic reject 从 Fix 1 的 2 降为 1，并保留 1 个 frozen-gold 命中与 0 clean FP。剩余拒绝来自不存在的成功读取记录，说明 fail-closed 保持不变。

## Fix 3：从可信上下文绑定 provenance

- 目的：系统依据实际 diff、成功 read/changed-context/symbol 结果与 Manifest 决定 evidence 来源，并覆盖模型自报的空/错误 candidate ID；只有唯一可确认的错误来源才纠正，歧义和缺失继续拒绝。
- Tests：全量 `pytest` 639 passed / 1 skipped；`mypy src` 通过 77 个 source files；全仓 Ruff、touched-file format、Core audit 与 `git diff --check` 全部通过。
- 正式运行：唯一单轮 5 × 2；`repeat_on_instability=false`、`max_attempts=1`；10/10 valid 且全部 attempt 1；candidate raw finding 4，semantic accepted 3，deterministic rejected 2，final risk 1，gold matched 1，clean-control warning/critical FP 0。
- pytest：MergeWarden deterministic passed、final 1、gold 1；baseline 的 `read_file` impact 没有真实成功读取，正确拒绝。
- Pydantic：baseline semantic accepted 后因非 changed 主锚点、非位置 verifier evidence 与未保留的测试范围被拒；MergeWarden 在 semantic verifier 被判 claim/behavior/mechanism unsupported。未修改 gold 或 matcher。
- 产物：`eval/outputs/core-eval-zero-hit-fix3.json`、`eval/reports/core-eval-zero-hit-fix3.md` 及报告引用的 10 个 JSONL event logs。

### 是否让命中链路前进

可信绑定合同与测试覆盖前进，但正式聚合数字没有改善：Fix 2 → Fix 3 的 deterministic reject 为 1 → 2，final / gold / clean FP 保持 1 / 1 / 0。新增 reject 是本轮 Pydantic baseline 输出变化；本提交只主张可重复的机制改进，不把模型 cohort 漂移包装成质量收益。
