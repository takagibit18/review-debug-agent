# Core Eval v1：Raw Finding 逐条审计

> 审计对象：`core-eval-v1.json` 中 2 个 candidate fixtures、2 个 A/B variants 产生的 4 条 raw findings。
> 本报告只做离线证据审计，不修改 frozen baseline、verifier、evidence gate 或正式指标。

## 结论

4 条 raw findings 都包含真实代码风险信号，但只有 pytest 的两条直接命中当前 gold root cause。

- Pydantic A/B：发现了 `extra != 'allow'` 时未知 update key 被静默丢弃的兼容性变化。该问题与 gold 属于同一 `model_copy(update=...)` 兼容性回归族，但没有准确描述 gold 所标注的 private-attribute reclassification。
- pytest A/B：正确定位 `SafeHashWrapper.__eq__` 未解包 `other.obj`。两条 finding 的影响描述都有过度泛化，应收窄后保留，而不是直接删除。
- 1 条被 semantic verifier 拒绝；3 条先被模型 verifier 接受，再被 deterministic evidence gate 拒绝。后 3 条的拒绝原因可以归结为 provenance/context binding，而不是代码风险不存在。

因此，当前 F1=0 不能简单解释为 reviewer 没有发现风险。它同时受到 verifier false negative、跨 hunk evidence binding 和 gold 覆盖边界影响。

## 数据完整性发现

逐条审计同时发现两个 fixture 的“完整 PR”契约并不一致：

- Pydantic 的真实 `base..head` 包含 `pydantic/main.py` 和 `tests/test_construction.py`，总计 113 insertions、7 deletions；当前 fixture checkout base 后只应用 `main.py` 的局部 diff。真实 PR 新增的 104 行 model-copy tests 没有出现在 Eval workspace。
- pytest 的真实 `base..head` 包含 5 个文件；当前 fixture workspace checkout 到 head，因此测试和 changelog 存在，但交给 reviewer 的 `diff_text` 仍只包含 `src/_pytest/fixtures.py`。

这意味着当前集合虽然都能恢复完整 repository workspace，却不都提供完整 PR patch/author tests。P-B 所称“没有测试覆盖非 allow 模式”在当前 Eval workspace 中成立，但对真实 PR head 不成立；真实 Pydantic PR 已加入 forbid/ignore/private-attr tests。该差异会削弱 verifier 对作者意图和兼容性边界的判断。

在扩大样本或修改 verifier 前，应先明确并机器校验 Core fixture contract：默认使用完整 `base..head` diff 和 head workspace；如果有意只审子 patch，必须显式标记 scope，不能仍按完整 PR 解释结果。

### 契约修复状态（2026-08-11）

该数据契约问题已经在当前分支修复，且规则不依赖具体仓库或 fixture ID：

- `full_pr` 必须恢复 head workspace，并从 Git 权威 `base..head` 派生 diff；禁止 base+fixture overlay。
- `partial_pr` 必须声明仓库相对路径和人工可审计的 scope reason；candidate gold 不得位于 scope 外。
- 未声明新契约的历史 fixture 保持 `legacy` 兼容，但 Core Eval 拒绝加载。
- 5 个 Core fixtures 已全部迁移为 `full_pr`。真实恢复验证得到：Pydantic #12117 为 2 个文件、pytest #9350 为 5 个文件，其余 controls 分别为 1、1、2 个文件。

原始 A/B 报告使用的是修复前输入，因此已标记为历史诊断；不做事后改分，必须重新运行才能产生新基线。

### 契约修复后新基线（2026-08-11）

相同模型、temperature、token/iteration budget 和 judge 下完成了 full-PR 重跑，共产生 15 个 attempts：

- Baseline：5/6 attempts valid（83.3%）；两个 candidate 均 valid，但 gold recall 为 0。
- MergeWarden：3/9 attempts valid（33.3%）；两个 candidate 各连续 3 次 placeholder/incomplete，均在 hard token cap 后且未调用 `submit_review`。
- 3 个 controls 的最终代表运行均 valid；warning/critical false findings 为 0。
- Workspace failure 与 validator failure 均为 0。

因此新基线的首要结论从“verifier recall loss”变为“完整 PR 上下文下的 runtime/budget scaling failure”。MergeWarden 没有 valid candidate completion，不能用本轮数据计算或比较 A/B F1。Baseline 在 pytest candidate 上仍产生 1 条 raw finding，但被 deterministic evidence gate 过滤；这是次级质量问题，不能覆盖主要 completion failure。

6 个 MergeWarden candidate attempts 的累计 token 记录约为 92k–106k，均超过声明的 80k hard budget后才结束，说明预算在模型调用后结算，单次调用可以造成 overshoot。后续修复应优先控制进入调用前的 context size/预留输出预算，而不是简单提高全局 hard cap。

### 图预算与预留提交修复后复跑（2026-08-11）

额度恢复后从上述失败点重新运行正式 5 × 2 Core Eval，共产生 10 个 attempts：

- Baseline 与 MergeWarden 均为 5/5 valid（100.0%）；2 个 candidates 与 3 个 controls 的 A/B 都在首次 attempt 完成。
- Placeholder/incomplete、workspace failure 与 validator failure 均为 0；10/10 runs 都调用了 `submit_review`，最大累计 token 为 69,606，低于 80k hard cap。
- Pydantic candidate 的图模式普通轮次选择了 1/2 个 manifest core 和 4 条 graph paths；pytest candidate 选择了 5/7 个 manifest core 和 1 条 graph path。图上下文被纳入 prompt budget 后仍实际进入模型，而不是退化为纯工具搜索。
- 图模式普通模型调用的 provider prompt 最大为 27,096 tokens；修复前失败运行的累计 token 约为 92k–106k。两者口径不同，不能解释为精确的同口径降幅，但足以确认本轮没有再次出现单次超大 graph prompt 导致 hard-cap overshoot。
- 两侧 Precision/Recall/F1 仍均为 0；4 个 valid candidate runs 共提出 4 条 raw findings，其中 1 个 run 的 finding 在最终输出前被过滤。当前首要质量问题重新回到 verifier/evidence recall loss，不能据此宣称 MergeWarden 质量优于 baseline。

充值前的首次修复后重跑因 provider 返回 `402 Insufficient Balance` 而 30/30 attempts 失败、token 记录均为 0；它属于外部运行故障，不纳入本次模型能力基线。此次复跑没有修改 gold、verifier、evidence gate、fixture 或 60k/80k 总预算，只加入了通用的图 prompt 预算与最终提交预留机制。

## 逐条结果

| ID | Fixture / Variant | Raw finding | 与 gold 的关系 | 审计结论 | 当前过滤点 |
|---|---|---|---|---|---|
| P-A | Pydantic / baseline | `extra != 'allow'` 时未知 update key 不再写入 `__dict__`，而是静默丢弃 | 同一兼容性回归族，但不是 gold 指定的 private-key reclassification | **实质有效，gold-adjacent**；不应以 unsupported 为由删除 | Semantic verifier：`claim_not_supported`、`observed_behavior_unsupported`、`causal_mechanism_unsupported` |
| P-B | Pydantic / MergeWarden | 与 P-A 相同，并称相关测试没有覆盖非 allow 的未知 key | 同一兼容性回归族，但不是 gold 的精确机制 | **风险有效、测试断言受 fixture 缺失误导**；deterministic rejection 是 provenance contract mismatch | Raw verifier accepted；deterministic：`deterministic_evidence_invalid` |
| Y-B | pytest / MergeWarden | `SafeHashWrapper.__eq__` 比较 `self.obj == other`，未解包 peer | 直接命中 gold root cause | **应收窄后保留**；“所有 hashable values 都失败、缓存完全失效”是过度陈述 | Raw verifier accepted；deterministic：`deterministic_evidence_invalid` |
| Y-A | pytest / baseline | 同样定位未解包和错误 identity fallback，同时推测错误合并/拆分 | 直接命中 gold root cause | **应重写后保留**；部分影响推演不成立，但核心缺陷成立 | Raw verifier accepted；deterministic：`deterministic_evidence_invalid` |

## P-A：Pydantic baseline

### finding 内容

旧实现对 `extra != 'allow'` 使用 `copied.__dict__.update(update)`；新实现统一逐 key 分类，却只处理 field、private attribute 和 `extra == 'allow'` 三种情况。未知 key 在 forbid/ignore 模式下会落空。

### 判定

该行为直接由 diff 成立，不依赖跨文件推断。PR 讨论也明确记录了维护者对既有 `model_copy(update=...)` 兼容性的担忧，以及作者希望继续设置 extra fields 的提议。因此 semantic verifier 给出的三个 `unsupported` reason codes 过严。

但是，当前 gold 更具体：它关注 private-looking key 从旧存储语义被重新路由到 `__pydantic_private__`。P-A 描述的是未知非 field/private key 被丢弃。二者相关，但不能在未经人工扩展 gold 前自动视为同一命中。

### 处置建议

- Product review：保留 warning。
- Eval adjudication：标记 `gold-adjacent / possible missing gold`，暂不改正式分数。
- Verifier：diff 内可直接证明的分支消失，不应要求额外运行时证据才承认 causal mechanism。

## P-B：Pydantic MergeWarden

### finding 内容

P-B 与 P-A 的核心判断一致，raw semantic verifier 已返回 accepted。

### deterministic rejection 的直接原因

Issue 顶层声明了 `context_manifest_id=C-001-86571f`，但四条 structured evidence 都没有携带相同的 `context_manifest_id` 或 `context_hash`。当前 gate 要求：只要 issue 声明 manifest，所有 evidence 必须绑定同一个 manifest；因此它必然被改写为 `deterministic_evidence_invalid`。

这属于 producer/validator provenance contract 不一致，而不是 finding 内容无效。

此外，P-B 关于测试缺口的 supporting claim 只对当前局部 fixture 成立：真实 PR head 的 `tests/test_construction.py` 已加入 forbid/ignore/private-attr coverage。这个错误不是 graph 检索遗漏，而是 Pydantic fixture checkout base 后没有带入 PR tests。

### 处置建议

- 不降低 deterministic gate 的可信边界。
- 让 evidence producer 在引用 manifest 时补全 exact manifest id/hash；纯 diff evidence 则不要在 issue 顶层错误声明 manifest。
- 与 P-A 一样，先进入 missing-gold adjudication，不直接改分。

## Y-B：pytest MergeWarden

### finding 内容

Finding 正确指出：`SafeHashWrapper.__eq__` 使用 `self.obj == other`，fallback 也使用 `id(self.obj) == id(other)`，没有解包另一个 wrapper。

### 行为复现

- `int`、`str` 等内建类型在比较未知 wrapper 时通常返回 `NotImplemented`，Python 的 reflected comparison 会让两个 wrapper 偶然比较成功。
- 对严格限制 peer type 的自定义 hashable value，两个底层值相等、hash 相同的 wrapper 可以比较为不相等，从而无法合并为同一 fixture key。

因此核心 gold 成立，但 raw finding 所称“所有 hashable values 都不相等”“fixture caching 对所有参数都失效”不成立。正确表述应该是“对某些不支持 wrapper cross-type comparison 的值会失败”。

### deterministic rejection 的直接原因

Candidate 主锚点位于第一个 `SafeHashWrapper` hunk（约 242–256 行），但 trigger/impact evidence 引用了第二个 hunk 的 278 行以及下游 308 行。Candidate context 的 diff-hunk 收集只保留与主锚点重叠的 hunk：

- 第二个 hunk 没有进入该 candidate 的 retained diff context；
- 308 行并非 patch 中的 changed line，却被 evidence 标为 `git_diff`；
- `_structured_candidate_evidence_valid()` 要求所有 structured evidence 都能在 candidate context 中验证，于是整体 fail closed。

### 处置建议

- Semantic verifier 应返回 revised warning，收窄 universal impact，而不是原样 accepted。
- Candidate context 应按 `all_evidence()` 的位置纳入相关 hunk/window，而不只按 primary location。
- Evidence producer 应把 308 行标成 tool/file-window evidence，而不是 `git_diff`。

## Y-A：pytest baseline

### finding 内容

Y-A 同样找到了 peer 未解包和 identity fallback 错位，因此命中 gold root cause；但它同时推测相等但不同 identity 的 unhashable values 会被错误合并。由于这类 wrapper 通常具有不同的 identity hash，它们不会仅凭 equality 被 dict 合并，该影响链不完整。

另一方面，严格类型 equality 的 hashable value 确实可以出现“底层相等且 hash 相同、wrapper 却不等”的失败，所以 finding 不应整体删除。

### deterministic rejection 的直接原因

和 Y-B 相同：主锚点只保留第一个 hunk，trigger/impact evidence 位于第二个 hunk，`diff` provenance 无法在 retained candidate context 中验证。

### 处置建议

- 由 semantic verifier 删除错误合并的推演，保留 strict-equality value 的 grouping failure。
- 修复跨 hunk candidate context binding 后，再验证 revised finding。

## 对当前指标的影响

本次审计不回写正式 Precision/Recall/F1，原因有两点：

1. Pydantic finding 是否扩展为第二条 gold，需要独立人工 adjudication，不能在看到模型输出后直接改标签。
2. pytest finding 虽命中 root cause，但 raw 文本含过度陈述，应先通过固定规则生成 revised finding，再重跑相同 A/B。

如果后续 adjudication 接受 Pydantic 为新增 gold，并让 pytest findings 经收窄后通过，两侧 reviewer 都可能从当前 0 分恢复；当前四条数据尚不能支持 MergeWarden 相对 baseline 的优势结论。

## 建议的修复顺序

1. 统一并校验 fixture 的完整 PR diff/head workspace 契约；局部 patch 必须显式标记。
2. 为 deterministic rejection 增加逐条件 reason telemetry，避免所有失败折叠成 `deterministic_evidence_invalid`。
3. 修复 graph finding 的 manifest id/hash 绑定契约。
4. 让 candidate context 收集覆盖 structured evidence 所引用的相关 hunk/window。
5. 强制 semantic verifier 对 universal claims 做收窄式 revision。
6. 对 Pydantic fixture 做盲审式 missing-gold adjudication；完成前保持正式指标不变。
