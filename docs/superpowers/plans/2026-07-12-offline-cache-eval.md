# Offline Cache Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run live-model Eval from existing local Git mirrors without network access.

**Architecture:** Add one opt-in setting read by the Eval runner. In offline mode, a cache hit is cloned locally and validated; a miss raises a fixture-scoped actionable error. Default online behavior is unchanged.

**Tech Stack:** Python 3.11, Pydantic settings, Git subprocesses, pytest.

## Global Constraints

- `EVAL_OFFLINE_WORKSPACE_CACHE` defaults to `false`.
- Offline mode must not execute remote update, fetch, or clone commands.
- Offline mode must not alter global Git configuration or TLS behavior.

---

### Task 1: Offline workspace cache behavior

**Files:**
- Modify: `src/config.py`
- Modify: `eval/runner.py`
- Test: `tests/test_eval_runner.py`

**Interfaces:**
- Consumes: `Settings.eval_offline_workspace_cache: bool`.
- Produces: `_ensure_git_workspace_cache(..., offline=True)` which either returns a valid local mirror or raises `RuntimeError` containing `offline cache miss`.

- [ ] **Step 1: Write failing tests**

```python
def test_offline_cache_uses_existing_mirror_without_remote_update(...):
    cache = _make_mirror_with_commit(...)
    assert _ensure_git_workspace_cache(workspace, cache, offline=True) == cache_root

def test_offline_cache_miss_is_actionable(...):
    with pytest.raises(RuntimeError, match="offline cache miss"):
        _ensure_git_workspace_cache(workspace, empty_cache, offline=True)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest tests/test_eval_runner.py -k offline_cache -q`
Expected: failure because the `offline` parameter and setting do not yet exist.

- [ ] **Step 3: Implement the minimal setting and cache branch**

```python
eval_offline_workspace_cache: bool = Field(default=False)

if offline:
    if not (cache_root / "objects").is_dir():
        raise RuntimeError(f"offline cache miss: {workspace.repo_url}")
    _run_git(["cat-file", "-e", f"{workspace.checkout_sha}^{{commit}}"], cwd=cache_root)
    return cache_root
```

- [ ] **Step 4: Run focused tests and the full Eval runner test file**

Run: `pytest tests/test_eval_runner.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

Do not commit automatically because the shared worktree contains unrelated pre-existing changes.

### Task 2: Live local-cache evaluation

**Files:**
- Create: `eval/outputs/v020_local_cache_eval_20260712.json`

**Interfaces:**
- Uses: `EVAL_OFFLINE_WORKSPACE_CACHE=true python -m eval.run eval --suite golden --samples 1`.
- Produces: the normal Eval report, diagnostics, summaries, and a baseline comparison when valid results are present.

- [ ] **Step 1: Run the local-cache evaluation**

Run: `$env:EVAL_OFFLINE_WORKSPACE_CACHE='true'; python -m eval.run eval --suite golden --samples 1 --output-json eval/outputs/v020_local_cache_eval_20260712.json`

- [ ] **Step 2: Inspect fixture outcomes**

Run: parse the report without printing API credentials and report valid/live fixture count, cache misses, metrics, and whether comparison/gate is meaningful.

- [ ] **Step 3: Verify source quality**

Run: `ruff check src eval tests; mypy src/; git diff --check`
Expected: all pass.
