# Verifier Hit-Rate Recovery Design

## Goal

Raise golden-review hit rate by removing verifier blindness, aligning workflow
conditions with risk candidates, hardening Windows workspace-cache publication,
and making candidate loss measurable without weakening the fail-closed risk gate.

## Confirmed root causes

1. `FindingVerifier` receives the diff, goal, constraints, and candidates, but no
   successful `read_file`, `get_changed_context`, or `find_symbol_context` output.
   The Agent feedback ring buffer owns those results and the verifier cannot see it.
2. `validate_candidate_draft` is declared with `required_if="has_candidates"`,
   while candidate construction and semantic verification only handle warning and
   critical findings. A run containing only info/style issues therefore skips the
   step and is later marked incomplete.
3. The eval cache publishes from one fixed `<cache>.tmp` directory and performs
   `rmtree`/`replace` once. Windows pack/index sharing conflicts surface as
   `WinError 5` and cleanup can mask the original clone/fetch failure.
4. A concrete regression can be emitted as info and never reach the risk verifier.
   The prompt currently discusses confidence but does not state the severity
   invariant strongly enough. The eval diagnostics also merge this case into a
   generic filtered/no-issue miss.
5. The verification event records only the post-validation batch. A local evidence
   location failure is rewritten to `evidence_not_found`, which is indistinguishable
   from the model's own rejection reason.

## Chosen architecture

### Candidate-scoped verifier evidence

The orchestrator keeps a run-scoped evidence ledger separate from the Agent prompt
feedback window. Only successful context tools are recorded. Before verification,
the ledger is compacted per candidate using its canonical path and line range:

- matching diff hunk and changed new-side lines;
- matching changed-context/file windows;
- enclosing symbols and relevant static symbol definitions/references;
- source tool name and arguments for auditability.

The complete compacted payload has a configured character ceiling. Entries are
ordered by direct path-and-line overlap, then same-path evidence, then cross-file
symbol evidence. This prevents old but important context from being evicted while
avoiding an unbounded verifier prompt.

Alternatives rejected:

- Passing the entire Agent feedback list is simple but includes unrelated reads,
  depends on the ring-buffer window, and raises verifier token cost.
- Giving the verifier its own tool loop would add latency, budget pressure, and a
  second orchestration state machine.

### Raw and deterministic verdict stages

`FindingVerifier` exposes the model-completed raw batch and the deterministic
post-validation batch. Deterministic location failures use the new reason code
`deterministic_evidence_invalid`, never `evidence_not_found`. Events and summaries
record raw model verdict counts/reasons, post-validation counts/reasons, and the
number of evidence checks passed/failed. Existing aggregate accepted/rejected
fields remain post-validation for compatibility.

### Risk-only draft validation and bounded severity review

`validate_candidate_draft` becomes conditional on `has_risk_candidates`. The
submit prompts state that concrete regressions, compatibility breaks, incorrect
results, data loss, and user-visible behavior changes must be warning/critical,
not info/style.

As a bounded safety net, an info finding is promoted to warning only when all of
these deterministic conditions hold:

- confidence is at least `0.85`;
- its canonical location overlaps a changed new-side line;
- evidence/suggestion contains an explicit concrete-risk phrase such as
  regression, incorrect result, data loss, silently dropped, crash, or exception
  behavior change.

Style findings and speculative text are never promoted. Counts for high-confidence
non-risk issues, reviewed issues, and promotions are emitted for diagnostics.

### Windows cache publication

Each cache initialization clones into a unique temporary parent beneath the cache
directory, then atomically publishes the mirror. Filesystem delete/rename operations
retry only Windows sharing/permission violations with bounded backoff. If another
process publishes first, a valid destination wins and the losing temporary tree is
removed. Clone/fetch failures clean the owned temporary tree and residual Git
`objects/pack/tmp_*` files without deleting a valid shared cache.

## Metrics and diagnostics

Per run and suite aggregates add:

- raw model accepted/rejected/needs-evidence/downgraded counts;
- deterministic evidence checked/passed/rejected counts;
- `verifier_accepted_coverage` (post-validation accepted / candidates);
- `evidence_validation_pass_rate` (passed / checked, 1.0 when nothing checked);
- high-confidence non-risk issue and severity-promotion counts;
- `miss_no_risk_candidate` for positive misses with high-confidence raw issues but
  no verifier candidates.

Workflow invalidity remains independent from finding matching. Infrastructure
failures before Agent execution remain schema-invalid and retain their explicit
workspace error.

## Verification

Unit tests must first fail for every new behavior, then pass after implementation.
Run focused verifier/workflow/cache/metrics/diagnostic/prompt tests, the complete
test suite, and finally the golden suite with `--fixture-concurrency 1` when model
credentials and cached/network workspaces are available. Compare the resulting
funnel, raw/post verdict split, evidence pass rate, schema validity, hit rate,
latency, and token cost against `v020_golden_retest_20260712.json`.
