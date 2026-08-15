# Graph prompt bad-case regression

Run date: 2026-08-15  
Optimization under test: `67593c2`  
Variant: `B2-graph-hybrid-warm`  
Policy: one measured run per fixture, no retry, priming excluded

## Result

Only 1/4 runs satisfied the full variant contract. All four had a verified Warm
cache hit, a ready graph, no fallback, and an unchanged logical index digest
between priming and measurement. The three invalid runs are Reviewer workflow
failures, not graph lifecycle failures.

| Fixture | Previous Warm (3 runs) | Regression run | Outcome |
|---|---:|---:|---|
| `golden_pydantic_pydantic_pr12117` | 0/3 valid; mean 43,507 tokens; 3.0 tools | invalid; 45,706 tokens; 3 tools | Still hard-capped before submit |
| `golden_deepset-ai_haystack_pr12208_reverse` | 0/3 valid; mean 38,230 tokens; 2.3 tools | invalid; 37,381 tokens; 3 tools | Still hard-capped before submit |
| `golden_real_requests_netrc_pr7205` | 2/3 valid; mean 65,599 tokens; 5.3 tools | invalid; 15,370 tokens; 2 tools | First request timed out; final payload rejected |
| `golden_pydantic_pydantic_pr12590` | 2/3 valid; mean 27,272 tokens; 3.0 tools | **valid**; 21,187 tokens; 3 tools | Diff-only Manifest duplication removed |

This is an `n=1` targeted regression signal for each fixture, not a replacement
for the existing A/B sample.

## Prompt effect

The old `manifest_token_cost` process metric is the graph planner's compact
estimate, not the Manifest text finally placed in the Reviewer prompt. Using
the final prompt-selection telemetry gives the following comparison:

| Fixture | Old first-prompt graph text | New first-prompt graph text | Estimated whole prompt, old -> new |
|---|---:|---:|---:|
| Pydantic 12117 | 27,076 tokens | 26,624 tokens | ~35,880 -> 35,507 |
| Haystack reverse | 30,911 tokens | 30,170 tokens | ~35,575 -> 34,864 |
| Requests netrc | 12,242 tokens | 11,634 tokens | 24,438 -> 23,887 |
| Pydantic docs-only | 1,677 tokens | **0 tokens** | ~11,225 -> 9,601 |

The docs-only case is the clean success: both changed-hunk Manifests had no
relations or graph paths, so the Reviewer now receives zero Manifest text. The
run remained valid and used 6,085 fewer total tokens than the prior mean (22.3%
lower).

For the positive hard-cap cases, graph-path text alone is 18,362 and 19,064
tokens, while relation Manifest text adds another 8,262 and 11,106 tokens. Hunk
deduplication saves only 452 and 741 tokens. Their first model calls used:

- Pydantic 12117: 37,316 prompt + 8,390 completion = 45,706 tokens.
- Haystack reverse: 36,387 prompt + 994 completion = 37,381 tokens.

Both crossed the run's 36,000 hard budget before a submit round. Both had
already produced a plausible draft finding, so the loss is in budget
scheduling/finalization rather than initial defect discovery.

## Tool behavior and confounders

This run does not establish a general tool-call improvement.

- Requests made two calls instead of the previous 5.3 mean and made no grep,
  but its first model request timed out after 90 seconds with zero recorded
  tokens. Only two deterministic changed-file reads ran. Its 76.6% token drop
  and lower tool count cannot be credited to the prompt optimization.
- Pydantic 12117 stayed at three calls: one draft finding, one targeted read,
  and one targeted grep. The calls addressed a concrete evidence gap, but the
  count did not fall.
- Haystack rose from a 2.3-call mean to three. Its repository-wide grep also
  matched `.mergewarden` event/journal files, which is avoidable search noise.
- The docs-only case stayed at three calls. Manifest removal reduced prompt
  size, not search count.

Requests also exposed a separate validator issue. It twice submitted
`issues=[]` with a clean summary containing "No concrete bugs, regressions, or
compatibility breaks". The validator treated that negated wording as a concern
and rejected the otherwise coherent payload.

## Judgment

The optimization is safe and useful for relation-free/diff-only cases, but is
insufficient for graph-heavy positive cases. The highest-leverage next fix is
to reserve completion/final-submit headroom before the first model call and trim
graph paths to that bound. A first prompt estimated near the 36k hard cap cannot
reliably complete and submit.

Separately, clean-summary validation should understand negated concern words,
and repository search should exclude `.mergewarden` runtime artifacts. These
are independent correctness/efficiency issues, not graph lifecycle failures.
