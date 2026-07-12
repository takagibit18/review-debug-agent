# Forced Tool Provider Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent thinking-mode providers from rejecting forced structured tool calls.

**Architecture:** `ModelClient.chat` derives an effective request config and merges provider-specific thinking-disable fields whenever `tool_choice` is forced. Callers and fail-closed verifier behavior remain unchanged.

**Tech Stack:** Python 3.11, OpenAI-compatible SDK, Pydantic, pytest.

## Global Constraints

- Keep forced `tool_choice`; do not downgrade to free-form JSON.
- Do not mutate caller-owned `ModelConfig`.
- Preserve unrelated `extra_body` keys.
- Other providers and calls without `tool_choice` remain unchanged.

---

### Task 1: Central provider compatibility

**Files:**
- Modify: `tests/test_model_client.py`
- Modify: `src/models/client.py`

**Interfaces:**
- Produces: `ModelClient._with_forced_tool_compat(config: ModelConfig) -> ModelConfig`.

- [ ] **Step 1:** Add failing payload tests for DeepSeek, DashScope, caller immutability, and ordinary calls.
- [ ] **Step 2:** Run `pytest tests/test_model_client.py -k forced_tool -q` and verify failure because compatibility is absent.
- [ ] **Step 3:** Implement the effective-config helper and use it before payload construction.
- [ ] **Step 4:** Run `pytest tests/test_model_client.py tests/test_finding_verifier.py -q` and verify pass.

### Task 2: Live regression verification

**Files:**
- Create: `eval/outputs/v020_local_smoke_eval_compat_20260712.json`

**Interfaces:**
- Runs the existing `local_smoke` suite through the real configured provider.

- [ ] **Step 1:** Run the single-sample local smoke Eval.
- [ ] **Step 2:** Confirm event log has `finding_verification_completed` without a provider HTTP 400.
- [ ] **Step 3:** Run full pytest, Ruff, Mypy, and diff checks.
- [ ] **Step 4:** Do not commit or stage the shared dirty worktree.
