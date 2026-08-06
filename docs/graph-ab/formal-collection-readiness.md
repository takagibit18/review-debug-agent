# Graph A/B Formal Collection Readiness

This is engineering-readiness and preview evidence only. It is not a formal statistical conclusion and makes no comparative superiority claim.

- Branch: `experiment/graph-ab-formal-readiness`
- Start commit: `dc9ff2a91c2f95b5b1edb5b22a33150f026fe9ae`
- Frozen baseline: `eval/agent-baseline-v1` -> `b5dc82bbffb38f1ba05587efa5dfcda08eb10b78`
- Frozen baseline modified: `False`
- Held-out executed: `False`

## Logical commits

1. `012a9c4994842fc9882998e5b5ea1cfc309548b7` feat(eval): support applying fixture diffs to restored workspaces
2. `1e5a7406bb971473d296c27c70021333fac7a770` test(eval): add reverse llxprt and haystack golden fixtures
3. `5b938b358939a1c4ed833ef8ad74d4589dd8ba32` perf(eval): add targeted workspace cache and offline restore
4. `6368a1164818131260599fb3be452ccdc046a508` feat(eval): add structural scope metrics for graph ab
5. `41295fdea87728531faf530eb8a6d433b243b7d1` feat(eval): add checkpoint resume for paired ab runs
6. `87005ffb036e97f11e7580610cbfa74232f0850a` test(eval): add formal ab preflight readiness gate
7. `SELF` test(eval): add formal ab preflight readiness gate

## Workspace cache and restore

The runner uses a targeted bare partial-clone cache, materializes only selected snapshots for offline checkout, and publishes caches atomically. Raw caches remain ignored under `eval/outputs/`.

- Prefetch success: `True` (5/5)
- Three-repeat restore test: `tests/test_eval_workspace_cache.py::test_cache_supplements_commits_and_restores_offline_three_times`

## Run evidence

| Layer | Fixtures | Measured | Valid | Invalid | Workspace failures | Fallbacks | Pairing errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| smoke | 1 | 3 | 3 | 0 | 0 | 0 | 0 |
| preflight | 3 | 9 | 4 | 5 | 0 | 0 | 0 |
| preview | 5 | 15 | 6 | 9 | 0 | 0 | 0 |

Checkpoint/resume verified: `True`.

Lifecycle checks:

- smoke: B1 all cold=`True`, B2 all warm=`True`, offline restore=`True`, checkpoint durable=`True`.
- preflight: B1 all cold=`True`, B2 all warm=`True`, offline restore=`True`, checkpoint durable=`True`.
- preview: B1 all cold=`True`, B2 all warm=`True`, offline restore=`True`, checkpoint durable=`True`.

Invalid runs:

- preflight: `golden_real_requests_netrc_pr7205` / `A-agent-search` - reasons: `run_error, placeholder_output, schema_invalid`
- preflight: `golden_real_requests_netrc_pr7205` / `B1-graph-hybrid-cold` - reasons: `run_error, placeholder_output, schema_invalid`
- preflight: `golden_real_requests_netrc_pr7205` / `B2-graph-hybrid-warm` - reasons: `run_error, placeholder_output, schema_invalid`
- preflight: `golden_pydantic_pydantic_pr12117` / `B2-graph-hybrid-warm` - reasons: `run_error, placeholder_output, schema_invalid`
- preflight: `golden_pydantic_pydantic_pr12117` / `B1-graph-hybrid-cold` - reasons: `run_error, placeholder_output, schema_invalid`
- preview: `golden_real_requests_netrc_pr7205` / `A-agent-search` - reasons: `run_error, placeholder_output, schema_invalid`
- preview: `golden_real_requests_netrc_pr7205` / `B1-graph-hybrid-cold` - reasons: `run_error, placeholder_output, schema_invalid`
- preview: `golden_real_requests_netrc_pr7205` / `B2-graph-hybrid-warm` - reasons: `run_error, placeholder_output, schema_invalid`
- preview: `golden_pydantic_pydantic_pr12117` / `B2-graph-hybrid-warm` - reasons: `run_error, placeholder_output, schema_invalid`
- preview: `golden_pydantic_pydantic_pr12117` / `B1-graph-hybrid-cold` - reasons: `run_error, placeholder_output, schema_invalid`
- preview: `golden_vybestack_llxprt-code_pr3012_reverse` / `B1-graph-hybrid-cold` - reasons: `run_error, placeholder_output, schema_invalid`
- preview: `golden_vybestack_llxprt-code_pr3012_reverse` / `B2-graph-hybrid-warm` - reasons: `run_error, placeholder_output, schema_invalid`
- preview: `golden_deepset-ai_haystack_pr12208_reverse` / `B2-graph-hybrid-warm` - reasons: `run_error, placeholder_output, schema_invalid`
- preview: `golden_deepset-ai_haystack_pr12208_reverse` / `B1-graph-hybrid-cold` - reasons: `run_error, placeholder_output, schema_invalid`

## Structural metrics and quality/cost

### A-agent-search

- Overall recall: `0.3333333333333333`; precision: `1.0`; root-cause recall: `0.3333333333333333`.
- Local/direct cross-file/multi-hop recall: `0.5` / `None` / `0.0`.
- Graph observable/unobservable recall: `0.0` / `0.5`.
- Structural coverage: `1.0`; observability coverage: `1.0`.
- Over/under merge: `0` / `0`; repair-unit accuracy: `0.0`.
- Valid/invalid runs: `4` / `1`; mean end-to-end latency: `22.657244825037196` seconds; mean total tokens: `38025.5`.

### B1-graph-hybrid-cold

- Overall recall: `None`; precision: `None`; root-cause recall: `None`.
- Local/direct cross-file/multi-hop recall: `None` / `None` / `None`.
- Graph observable/unobservable recall: `None` / `None`.
- Structural coverage: `None`; observability coverage: `None`.
- Over/under merge: `0` / `0`; repair-unit accuracy: `0.0`.
- Valid/invalid runs: `1` / `4`; mean end-to-end latency: `53.69921400002204` seconds; mean total tokens: `24902.0`.

### B2-graph-hybrid-warm

- Overall recall: `None`; precision: `None`; root-cause recall: `None`.
- Local/direct cross-file/multi-hop recall: `None` / `None` / `None`.
- Graph observable/unobservable recall: `None` / `None`.
- Structural coverage: `None`; observability coverage: `None`.
- Over/under merge: `0` / `0`; repair-unit accuracy: `0.0`.
- Valid/invalid runs: `1` / `4`; mean end-to-end latency: `54.40596620016731` seconds; mean total tokens: `24933.0`.

## Golden review status

Reviewed preflight fixtures: `3`.

| Fixture | Phase | Reviewed | Expected issues |
|---|---|---:|---:|
| development_agent_search_cross_file | smoke | True | 1 |
| golden_real_requests_netrc_pr7205 | preflight | True | 0 |
| golden_pydantic_pydantic_pr12117 | preflight | True | 1 |
| golden_pydantic_pydantic_pr12590 | preflight | True | 0 |
| golden_vybestack_llxprt-code_pr3012_reverse | preview | True | 1 |
| golden_deepset-ai_haystack_pr12208_reverse | preview | True | 1 |


## Go / No-Go

Ready for formal paired A/B: `NO`.

Blocking issues:

- preflight_variants_incomplete_or_invalid
- preflight_schema_invalid

Warnings:

- preview_invalid_runs: 9
- engineering_preview_only: no formal statistical conclusion
