# Reproducible Golden Eval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Golden Git workspace restoration deterministic in the sandbox.

**Architecture:** Apply TLS backend and safe-directory controls per Git command; avoid remote update on cache hits.

**Tech Stack:** Python, Git subprocess, pytest.

### Task 1: Git command compatibility

- [ ] Add failing tests asserting configured Git `-c` arguments and no update on cache hit.
- [ ] Implement settings and runner command construction.
- [ ] Run focused runner/config tests.

### Task 2: Golden execution

- [ ] Run Golden in batches with the configured backend and merge reports.
- [ ] Verify every fixture produces an event log before interpreting quality metrics.
