# Reviewer / Runtime Diagnostic Smoke — pytest PR 9350

> Diagnostic smoke only. This is not a Graph recall or quality comparison.

## Fixed Contract

- Fixture: `golden_pytest-dev_pytest_pr9350` (`eval/fixtures/golden_pytest-dev_pytest_pr9350.json`)
- Model: `deepseek-v4-pro`; temperature `0`; max output tokens `4096`
- Prompt / cumulative budgets: `12000` prompt; `60000/80000` soft/hard; `12000` submit reserve
- Loop / tools: `3` review iterations; `64` tool calls; request/run timeouts `90.0/170.0` seconds
- Gates: verifier `enforce`; workflow `enforce`
- Order: `A-agent-search → B1-graph-hybrid-cold`; one measured attempt per variant; no retry; no warm run
- Matcher: existing Eval matcher; fixture and gold were not modified

## Summary Matrix

| Stage | A-agent-search | B1-graph-hybrid-cold |
|---|---|---|
| Workspace valid | PASS | SKIPPED |
| Fixture validation | YES | SKIPPED |
| Runtime valid completion | FAIL | SKIPPED |
| Gold file reached | YES | SKIPPED |
| Gold symbol reached | YES | SKIPPED |
| Reviewer discovered gold | NO | SKIPPED |
| Draft persisted | NO | SKIPPED |
| submit_review | NO | SKIPPED |
| Length recovery | NOT_REQUIRED | SKIPPED |
| Pre-verifier | N/A | SKIPPED |
| Semantic verifier | N/A | SKIPPED |
| Deterministic validation | N/A | SKIPPED |
| Final finding survived | NO | SKIPPED |
| Gold match | NOT_REACHED | SKIPPED |
| Graph manifest valid | N/A | SKIPPED |
| Final diagnosis | provider_request: Provider request failed before any model response or submit_review: Model provider request timed out after 90s [code=timeout] | SKIPPED |

## Failure Attribution Matrix

| Variant | Failure Stage | Evidence | Interpretation |
|---|---|---|---|
| A-agent-search | provider_request | Provider request failed before any model response or submit_review: Model provider request timed out after 90s [code=timeout] | provider_request: Provider request failed before any model response or submit_review: Model provider request timed out after 90s [code=timeout] |
| B1-graph-hybrid-cold | provider_request | B skipped because shared runtime blocker was observed in A | shared runtime blocker; not measured |

## Per-variant Audit

### A-agent-search

- Run ID: `3b2c8501-f93d-4946-816c-618d7ef0a7bb`
- Runtime: schema_valid=False, placeholder=True, workflow_invalid=False, finish_reasons=['continue', 'run_timeout'], budget=none
- Runtime errors: provider=['Model provider request timed out after 90s [code=timeout]']; other=['Model provider request timed out after 90s [code=timeout]', 'Placeholder review output: no submit_review/debug before finalize.']
- Context path: review diff prompt (full PR diff contains the gold hunk), read_file EventLog result lines 1-80; graph_status=disabled; cache_mode=not_applicable; cache_hit=None; manifests=0; fallback=none
- Discovery: NO; evidence: no full visible/draft semantic statement
- Draft: record_draft_finding=False; count=0; correct_file_symbol=False; correct_semantics=False
- Submit: NO; summary_nonempty=False; issues=0; blank=False; schema_invalid=False; contains_gold=False
- Recovery: NOT_REQUIRED; required=False; attempted=False; evidence_preserved=None; gold_preserved=None
- Funnel: submitted=0; pre=N/A (['none']); semantic=N/A (['none']); deterministic=N/A (['none']); final_risk=0
- Artifacts: EventLog `E:\PycharmProjects\Debug\eval\outputs\event_logs\golden_pytest-dev_pytest_pr9350_3b2c8501-f93d-4946-816c-618d7ef0a7bb.jsonl`; Run Journal status=missing_no_entries, path=``

### B1-graph-hybrid-cold

B skipped because shared runtime blocker was observed in A

## Ready for formal Graph A/B?

### NO-GO: reviewer/runtime blocker

B was skipped after a shared runtime blocker in A.

Restore deepseek-v4-pro provider responsiveness under the existing 90s Core request timeout, then rerun this smoke; do not tune reviewer, verifier, matcher, or token budgets before a model response is observed.

The result is a stage attribution for one reviewed positive fixture. Any A/B HIT/MISS difference is at most an early fixture-specific signal, not evidence that Graph changes recall.
