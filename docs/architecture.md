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
