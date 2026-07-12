# Diff-Only Local Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one live-model local smoke fixture without Git checkout.

**Architecture:** The fixture relies on the runner's existing `workspace=None` path, which writes `input.files` into a temporary repository. It preserves the original diff and expected issue, but uses a separate suite.

**Tech Stack:** Python 3.11, Pydantic fixture JSON, pytest.

## Global Constraints

- The fixture suite is exactly `local_smoke`.
- The fixture must not have `input.workspace`.
- It is excluded from Golden baseline comparison.

---

### Task 1: Local smoke fixture

**Files:**
- Create: `eval/fixtures/local_smoke_pytest_approx_pr8513.json`
- Modify: `tests/test_eval_runner.py`

**Interfaces:**
- Consumes: `load_fixtures(Path("eval/fixtures"), suite="local_smoke")`.
- Produces: a review `Fixture` with files for both diff paths and no workspace.

- [ ] **Step 1: Write the failing fixture-contract test**

```python
def test_local_smoke_fixture_is_file_backed_and_not_golden():
    fixture = Fixture.model_validate_json(Path(...).read_text(encoding="utf-8"))
    assert fixture.metadata.suite == "local_smoke"
    assert fixture.input.workspace is None
    assert set(fixture.input.files) == {"src/_pytest/python_api.py", "testing/python/approx.py"}
```

- [ ] **Step 2: Run the test and verify RED**

Run: `pytest tests/test_eval_runner.py -k local_smoke_fixture -q`
Expected: failure because the fixture file does not exist.

- [ ] **Step 3: Create the minimal line-preserving fixture**

Store the original PR diff, one warning expectation at `src/_pytest/python_api.py:285-288`, and sparse source file bodies that include every changed line in the diff.

- [ ] **Step 4: Run the contract test and runner validation**

Run: `pytest tests/test_eval_runner.py -k local_smoke_fixture -q`
Expected: PASS.

- [ ] **Step 5: Run the live smoke evaluation**

Run: `python -m eval.run eval --suite local_smoke --samples 1 --output-json eval/outputs/v020_local_smoke_eval_20260712.json`

- [ ] **Step 6: Commit**

Do not commit automatically because the worktree contains unrelated pre-existing changes.
