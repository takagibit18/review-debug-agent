# Verifier Hit-Rate Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Subagents are explicitly prohibited for this task.

**Goal:** Restore review hit rate by giving the verifier candidate-scoped evidence, fixing risk-only workflow semantics, hardening Windows cache operations, and exposing stage-specific diagnostics.

**Architecture:** A run-scoped evidence ledger captures successful context-tool results and compacts them per candidate before the bounded verifier call. Raw model verdicts and deterministic validation are measured separately; strict prompt guidance plus a deterministic high-confidence info review prevents concrete regressions from bypassing the risk gate. Cache mirrors publish from unique temporary directories with bounded Windows filesystem retries.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, pytest, Git CLI.

## Global Constraints

- Do not invoke subagents.
- Do not ask the user questions before all work is complete.
- Preserve fail-closed enforcement for warning/critical findings.
- Keep verifier context bounded and candidate-specific.
- Preserve unrelated user changes and do not create commits.

---

### Task 1: Candidate-scoped verifier evidence and staged verdicts

**Files:**
- Create: `src/analyzer/verifier_context.py`
- Modify: `src/analyzer/finding_verifier.py`
- Modify: `src/analyzer/schemas.py`
- Modify: `src/orchestrator/agent_loop.py`
- Test: `tests/test_finding_verifier.py`
- Test: `tests/test_agent_loop.py`

**Interfaces:**
- Produces: `capture_verifier_tool_evidence(entries, workspace_root) -> list[dict[str, Any]]`.
- Produces: `build_candidate_verifier_context(candidates, request, evidence, max_chars) -> list[dict[str, Any]]`.
- Extends: `FindingVerifier.verify(candidates: list[FindingCandidate], request: ReviewRequest, state: ContextState, *, tool_evidence: list[dict[str, Any]] | None = None) -> FindingVerificationBatch`.
- Produces: `FindingVerifier.last_raw_batch` and `last_post_validation_batch`.

- [ ] **Step 1: Write failing tests**

```python
def test_verifier_payload_includes_candidate_scoped_context():
    # Capture changed-context, symbol-context, and read-file results.
    # Assert candidate A receives its hunk/window/symbol and candidate B does not.

def test_deterministic_rejection_has_distinct_reason_and_preserves_raw_batch():
    # Raw model accepts an invalid location; post-validation rejects it with
    # deterministic_evidence_invalid while last_raw_batch remains accepted.
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_finding_verifier.py tests/test_agent_loop.py -q`

Expected: new assertions fail because no evidence payload/raw-stage fields exist.

- [ ] **Step 3: Implement the bounded ledger and staged validation**

```python
VERIFIER_CONTEXT_TOOL_NAMES = {
    "read_file", "get_changed_context", "changed_context",
    "find_symbol_context", "symbol_context",
}

async def verify(self, candidates, request, state, tool_evidence=None):
    payload["candidate_context"] = build_candidate_verifier_context(
        candidates, request, tool_evidence or [], max_chars=self._context_max_chars
    )
    self.last_raw_batch = _complete_fail_closed(candidates, parsed)
    self.last_post_validation_batch = validate_verifications(
        candidates, self.last_raw_batch, request
    )
    return self.last_post_validation_batch
```

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_finding_verifier.py tests/test_agent_loop.py -q`

Expected: all tests pass.

### Task 2: Risk-only workflow and bounded severity review

**Files:**
- Modify: `src/orchestrator/review_workflow.py`
- Modify: `src/analyzer/finding_verifier.py`
- Modify: `src/analyzer/prompts.py`
- Modify: `src/orchestrator/agent_loop.py`
- Test: `tests/test_review_workflow.py`
- Test: `tests/test_finding_verifier.py`
- Test: `tests/test_prompts.py`

**Interfaces:**
- Changes: `validate_candidate_draft.required_if = "has_risk_candidates"`.
- Produces: `review_high_confidence_info_findings(report, request) -> SeverityReviewResult`.

- [ ] **Step 1: Write failing tests**

```python
def test_info_only_review_does_not_require_candidate_draft_validation():
    assert tracker.missing_required(
        has_candidates=True, has_risk_candidates=False
    ) == expected_without_draft_validation

def test_high_confidence_changed_line_concrete_risk_promotes_to_warning():
    # confidence=.90, changed line, explicit data-loss claim => warning

def test_speculative_or_unchanged_info_is_not_promoted():
    # Keep info for low confidence, unchanged lines, and style findings.
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_review_workflow.py tests/test_finding_verifier.py tests/test_prompts.py -q`

Expected: workflow and severity/prompt assertions fail.

- [ ] **Step 3: Implement minimal behavior**

```python
ReviewWorkflowStep(
    step_id="validate_candidate_draft",
    phase=30,
    required_if="has_risk_candidates",
)
```

Apply severity review before `build_candidates`, emit reviewed/promoted counts, and
state the warning/critical invariant in system, user, and final-submit prompts.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_review_workflow.py tests/test_finding_verifier.py tests/test_prompts.py -q`

Expected: all tests pass.

### Task 3: Windows workspace-cache publication

**Files:**
- Modify: `eval/runner.py`
- Test: `tests/test_eval_runner.py`

**Interfaces:**
- Produces: `_remove_tree_with_retry(path, attempts=5) -> None`.
- Produces: `_publish_cache_with_retry(source, destination, attempts=5) -> None`.
- Produces: unique temp parent per `_ensure_git_workspace_cache` initialization.

- [ ] **Step 1: Write failing tests**

```python
def test_workspace_cache_uses_unique_temp_directory(monkeypatch, tmp_path: Path):
    assert clone_targets[0] != cache_root.with_name(cache_root.name + ".tmp")

def test_workspace_cache_retries_windows_permission_error_and_cleans_temp(
    monkeypatch, tmp_path: Path
):
    # First replace/rmtree raises PermissionError(winerror=5), later call succeeds.

def test_workspace_cache_cleans_owned_temp_after_fetch_failure(
    monkeypatch, tmp_path: Path
):
    # No unique *.tmp directory or tmp_pack_* file remains.
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_eval_runner.py -q`

Expected: unique temp/retry/cleanup tests fail.

- [ ] **Step 3: Implement unique clone/publish and bounded retries**

```python
temp_parent = Path(tempfile.mkdtemp(
    prefix=f".{cache_root.name}.", suffix=".tmp", dir=workspace_cache_dir
))
tmp_root = temp_parent / cache_root.name
```

Retry only sharing/permission errors, accept a valid destination published by a
competitor, and clean owned temp paths in `finally` without masking the root error.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_eval_runner.py -q`

Expected: all tests pass, including real local Git cache tests.

### Task 4: Metrics and high-confidence no-candidate diagnosis

**Files:**
- Modify: `src/analyzer/run_summary.py`
- Modify: `eval/run_summary.py`
- Modify: `eval/schemas.py`
- Modify: `eval/diagnostics.py`
- Test: `tests/test_runtime_run_summary.py`
- Test: `tests/test_eval_process_metrics.py`
- Test: `tests/test_eval_diagnostics.py`

**Interfaces:**
- Adds raw-model verdict counts/reasons and deterministic evidence counts/reasons.
- Adds `verifier_accepted_coverage` and `evidence_validation_pass_rate`.
- Adds diagnostic reason `miss_no_risk_candidate`.

- [ ] **Step 1: Write failing tests**

```python
def test_metrics_separate_raw_and_deterministic_verdicts():
    assert metrics.raw_verifier_rejected_count == 1
    assert metrics.deterministic_evidence_rejected_count == 1
    assert metrics.evidence_validation_pass_rate == 0.5

def test_high_confidence_raw_issue_without_candidate_has_distinct_diagnosis():
    assert "miss_no_risk_candidate" in diagnosis.reasons
    assert "miss_no_issue" not in diagnosis.reasons
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_runtime_run_summary.py tests/test_eval_process_metrics.py tests/test_eval_diagnostics.py -q`

Expected: new fields/reason do not exist.

- [ ] **Step 3: Implement parsing, aggregation, and notes**

Keep legacy verifier counts mapped to post-validation values. Compute:

```python
verifier_accepted_coverage = accepted / candidates if candidates else 0.0
evidence_validation_pass_rate = passed / checked if checked else 1.0
```

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest tests/test_runtime_run_summary.py tests/test_eval_process_metrics.py tests/test_eval_diagnostics.py -q`

Expected: all tests pass.

### Task 5: Regression and eval verification

**Files:**
- Verify all changed production and test files.
- Output: `eval/outputs/<new-run>.json` and companion diagnostics when live eval is available.

- [ ] **Step 1: Run focused tests**

Run: `python -m pytest tests/test_finding_verifier.py tests/test_review_workflow.py tests/test_agent_loop.py tests/test_prompts.py tests/test_eval_runner.py tests/test_runtime_run_summary.py tests/test_eval_process_metrics.py tests/test_eval_diagnostics.py -q`

Expected: pass.

- [ ] **Step 2: Run quality checks**

Run: `python -m ruff check src eval tests`

Run: `python -m mypy src eval`

Expected: no newly introduced errors.

- [ ] **Step 3: Run full unit suite**

Run: `python -m pytest -q`

Expected: pass.

- [ ] **Step 4: Run serial golden fixtures**

Run the eval CLI with the golden suite and `--fixture-concurrency 1`, preserving
`samples=1` and the current provider/model settings.

Expected: Nethermind/OpenClaw reach Agent execution; workflow-invalid info/style
runs disappear; reports contain raw/post verdict split and evidence pass rate.

- [ ] **Step 5: Compare funnel**

Compare against `eval/outputs/v020_golden_retest_20260712.json`. Report schema
validity, hit rate, candidates, raw/post accepted/rejected, deterministic evidence
failures, workflow invalid runs, latency, tokens, and any remaining blockers.
