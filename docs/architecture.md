# Architecture

## Layered Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Entry Layer:  CLI (Click)  ·  FastAPI synchronous thin routes      │
├─────────────────────────────────────────────────────────────────────┤
│  Orchestration Layer:  Agent loop (5-phase pattern)                 │
│  · Phase 1: Context preparation (load relevant files / changes)     │
│  · Phase 2: Model analysis (LLM reasoning & plan formulation)       │
│  · Phase 3: Tool execution (read files, run tests, grep, …)        │
│  · Phase 4: Result processing (aggregate, format, state update)     │
│  · Phase 5: Continue / terminate decision                           │
├─────────────────────────────────────────────────────────────────────┤
│  Tool Layer (Tool Calling)                                          │
│  · Read-only tools  — safe for concurrent execution                 │
│  · Write tools      — serialised, require confirmation              │
│  · Execute tools    — sandboxed with timeout & cwd constraints      │
│  · Structured schemas (JSON Schema / Pydantic validation)           │
├─────────────────────────────────────────────────────────────────────┤
│  Service Layer:  API client · state management · context compress    │
├─────────────────────────────────────────────────────────────────────┤
│  Model Layer:  OpenAI-compatible API / provider abstraction          │
├─────────────────────────────────────────────────────────────────────┤
│  Cross-cutting:  config · logging · structured output (Pydantic)    │
│                  cost & token tracking · permission management       │
└─────────────────────────────────────────────────────────────────────┘
```

## Package Mapping

| Layer | Package | Owner |
|-------|---------|-------|
| Entry | `cli.py`, `src/api/` | Integration Agent |
| Orchestration | `src/orchestrator/` | Shared |
| Analyzer | `src/analyzer/` | Analyzer Agent |
| Tools | `src/tools/` | Integration Agent |
| Security | `src/security/` | Integration Agent |
| Models | `src/models/` | Analyzer Agent |
| Config | `src/config.py` | Shared |

**接口契约**：CLI、编排层、工具层及与 Analyzer 相关的跨层约定见 [cli_tools_orchestrator_contract.md](./cli_tools_orchestrator_contract.md)。

## Key Design Decisions

### 5-Phase Agent Loop

Inspired by Claude Code's query pattern.  Each session runs a loop of:
prepare context → model analysis → tool execution → result processing →
continue-or-stop.  The loop repeats until the agent decides the task is
complete or a budget (token / time) is exhausted.

### Tool Safety Classification

Tools declare their safety level (`readonly` / `write` / `execute`).
The executor uses this to decide concurrency and confirmation requirements.
OpenAI-compatible **tool schemas** (registered tools plus `submit_*` pseudo-tools) are built in `src/orchestrator/tool_schemas.py` and passed into the inference layer by `AgentOrchestrator`.

FastAPI exposes a synchronous MVP+ API with `GET /health`, `POST /review`, and `POST /debug`. The HTTP layer reuses the same `ReviewRequest` / `DebugRequest` and `ReviewResponse` / `DebugResponse` Pydantic contracts as the CLI, and only handles request validation, stable JSON errors, and orchestrator dispatch. Phase 2 adds `GET /runs/{run_id}/summary` as an independent observability helper so the core review/debug response schemas stay stable.

Execute-class tools (`run_command`, `run_tests`) use `src/security/exec_policy.py` for argv parsing and allowlists, `src/security/backends.py` for pluggable backends (default `subprocess`, optional `docker`), and `src/security/sandbox.py` as the dispatch entry. Debug-only registration and `EXECUTE_*` settings are documented in [execute_tools_design.md](./execute_tools_design.md) and [shared_contracts.md](./shared_contracts.md).

### Structured Output

All agent output conforms to Pydantic models (`ReviewIssue`,
`ReviewReport`, etc.) so consumers (CLI, API, CI) can rely on a stable
schema.

### Context Budget

Only the diff and immediately relevant file fragments are fed to the model
by default.  The context window expands on demand (interface definitions,
adjacent modules) to control token cost and reasoning noise.

Graph-assisted review follows the same input budget as agent search. Candidate
manifests are truncatable prompt parts; audit-only discarded or low-confidence
paths remain in event logs and are never appended outside the prompt budget.
Every run also protects a final-submit reserve. Normal exploration stops at the
analysis ceiling, while a compact submit-only request remains available for a
structured `ReviewReport` or debug result.

For PR review integrations, the durable product contract is diff-first:
the submitted PR diff is the review target, while a full repository snapshot
may be mounted as the tool workspace for contextual reads.  The model should
not receive the full repository in the initial prompt; it should start from
the diff and changed files, then use read-only tools to inspect unchanged
context when needed.  Review findings intended for inline PR feedback must
point back to changed lines or changed hunks; unchanged files may support
the evidence but should not become the primary comment location.

CI remains the hard authority for mergeability.  MergeWarden's PR-facing role
is to produce review suggestions, soft checks, and evidence for risks that may
pass automated tests but still deserve human attention.
The first GitHub publishing surface is GitHub Actions: `github-advisory publish`
can create a neutral check run and advisory review comments using `GITHUB_TOKEN`
with `checks:write` and `pull-requests:write`. Comment lifecycle uses stable
issue fingerprints plus hidden MergeWarden metadata to update matching comments
and mark stale findings without deleting or editing human comments.

### Observability

Every run logs a `run_id`, tool-call sequence, key intermediate results,
wall-clock time, and token usage, enabling post-hoc debugging of
false-positives or missed issues.
Across loop iterations, **tool results are fed back** into the next model call (`tool_feedback` in the inference engine) so multi-step tool use remains coherent.
Runtime summaries live in `src/analyzer/run_summary.py` and are reused by CLI,
FastAPI, and eval wrappers. A summary includes event-log status, models, token
counts, tool-call counts, budget state, stop reasons, submit validation errors,
artifact paths, and GitHub publish status.

### v0.2.0 Finding Verification and Workflow Gate

Review output now passes through two explicit gates after the normal five-phase loop:

```text
ReviewReport draft
  -> deterministic policy filter
  -> stable FindingCandidate ids
  -> independent semantic verifier
  -> required-step ReviewWorkflowTracker
  -> final advisory payload
```

Only Warning/Critical findings require semantic verification. In `enforce` mode a missing, malformed, or rejected verdict is fail closed; Info/Style findings remain advisory and do not consume verifier capacity. `needs_evidence` receives one bounded re-verification round. Runtime summaries expose verifier counts/reason codes and Workflow required/completed/missing steps.

### v0.2.0 Durable Worker State

The platform queue uses an atomic SQLite claim with `lease_owner`, `lease_expires_at`, `heartbeat_at`, and `attempt`. `run_checkpoints` records `review_pipeline` and `persist_artifacts` attempts. Expired leases are requeued from the first incomplete checkpoint. Artifact writes use same-directory atomic replacement with SHA-256 sidecars, usage records are idempotent per run attempt, and GitHub check runs use a stable external id for update-on-recovery behavior.

### v0.2.3–v0.2.5 Root-Cause Review Pipeline

Warning/Critical review hypotheses now pass through two distinct gates: a per-finding evidence verifier before consolidation and a cluster-level consolidation verifier after conservative blocking and complete-link grouping. A change-centered relation graph and exact Candidate Context Manifest constrain what the Reviewer and verifier may cite. The graph answers which code to inspect; it never defines root-cause clusters.

The local static graph uses qualified symbol identities, evidence-aware edges, field read/write relations and an optional resolver interface. A versioned SQLite index supports hash-based incremental rebuilds and safe corruption/schema fallback. See [v023_v025_root_cause_relation_graph.md](./v023_v025_root_cause_relation_graph.md) for schemas, provenance rules, migrations and diagrams.
