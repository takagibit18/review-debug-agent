# Graph A/B 阶段二正式评测就绪性

结论：**Ready for formal paired A/B: NO**

## 已满足

- [x] `A-agent-search`、`B1-graph-hybrid-cold`、`B2-graph-hybrid-warm` 均可端到端运行。
- [x] 实际 Variant 契约依据 event log telemetry 校验，而非信任命令行名称。
- [x] Smoke fixture 三组均 valid。
- [x] B1 真实 Cold：独立 eval-owned index、运行前清理、cache miss、build latency > 0。
- [x] B2 真实 Warm：Cold context priming 与 measured run 分离，measured cache hit。
- [x] Graph fallback、cache 模式不符、event log 缺失、timeout、placeholder、workflow/schema invalid 自动 invalid。
- [x] Invalid run 不进入均值或质量比较，保留原始 run ID 和原因。
- [x] 固定 seed 的 Latin-square 顺序与同 fixture snapshot 一致性校验已实现。
- [x] Prompt、Verifier、Matcher、Finding Schema、Golden、阈值和冻结 baseline 未修改。
- [x] held-out 未执行；正式大规模 A/B 未执行。
- [x] 质量、成本、稳定性和可获取的 Agent 行为指标已进入 raw/compact 矩阵；不可获取字段为 `null`/`not_available`。
- [x] 自动化验证通过：核心 128 passed；全量 516 passed、1 skipped；mypy/ruff PASS。
- [x] Pilot 报告和 compact summary 已生成。

## 阻塞项

1. **远程 snapshot 恢复未通过有界验证。** 三个既有 reviewed fixtures 首次 clone 失败后被正确标为 9 个 invalid runs；授权重试中的完整 mirror clone 超过 20 分钟总时限，未进入 measured run。正式批量会依赖该能力。
2. **Pilot 规模不足。** 当前有效范围是 2 fixtures × 3 Variants × 1 sample，只能标记为 `pilot-smoke`，不能用于稳定性结论。
3. **代表性覆盖不足。** 当前本地 fixtures 覆盖 single/cross-file、two-hop、public API、state/cache consistency 和 test gap 标签，但缺少至少第三个可可靠恢复的真实 reviewed repository snapshot。

## 解除阻塞的最小后续验证

在不修改冻结契约、不运行 held-out 的前提下：

1. 预先建立或修复有界、可复用的 reviewed fixture repository mirror cache，并校验 checkout SHA。
2. 选择至少 3 个 reviewed development/validation fixtures，执行每 Variant 每 fixture 至少 1 次的配对预演；如预算允许再提升至 3 次。
3. 要求 A/B1/B2 contract 全部 valid、无 fallback、无 workspace/timeout/schema invalid，随后重新生成同一格式的 compact summary 和报告。

在上述阻塞解除前，不应启动正式批量 Graph A/B。
