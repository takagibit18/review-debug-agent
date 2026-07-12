# Diff-Only Local Eval Design

## Goal

Provide one live-model local smoke evaluation when full Git fixture snapshots are
unavailable. The result proves local runner and model connectivity only; it is
not a Golden baseline or regression gate.

## Fixture

The fixture is derived from `golden_pytest-dev_pytest_pr8513`. It preserves the
original unified diff and manually reviewed expected warning. It has no Git
workspace. Instead, it stores two minimal source files with line numbers that
match the diff: `src/_pytest/python_api.py` and `testing/python/approx.py`.

## Behavior

The existing file-workspace branch materializes the fixture files in a temporary
directory. The standard runner validates changed lines, invokes the configured
model, evaluates matches, and writes standard artifacts. The fixture uses a
separate `local_smoke` suite and its report is never compared to a Golden
baseline.

## Verification

Tests validate that the local fixture is schema-valid, contains no workspace,
and has files at the expected changed locations. A single-sample run records
latency, tokens, and expected-issue matching without remote Git access.
