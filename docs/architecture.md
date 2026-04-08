# Architecture

## Layered Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  Entry Layer:  CLI (Click)  ·  Optional FastAPI routes              │
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
| Entry | `cli.py` | Integration Agent |
| Orchestration | `src/orchestrator/` | Shared |
| Analyzer | `src/analyzer/` | Analyzer Agent |
| Tools | `src/tools/` | Integration Agent |
| Security | `src/security/` | Integration Agent |
| Models | `src/models/` | Analyzer Agent |
| Config | `src/config.py` | Shared |

## Key Design Decisions

### 5-Phase Agent Loop

Inspired by Claude Code's query pattern.  Each session runs a loop of:
prepare context → model analysis → tool execution → result processing →
continue-or-stop.  The loop repeats until the agent decides the task is
complete or a budget (token / time) is exhausted.

### Tool Safety Classification

Tools declare their safety level (`readonly` / `write` / `execute`).
The executor uses this to decide concurrency and confirmation requirements.

### Structured Output

All agent output conforms to Pydantic models (`ReviewIssue`,
`ReviewReport`, etc.) so consumers (CLI, API, CI) can rely on a stable
schema.

### Context Budget

Only the diff and immediately relevant file fragments are fed to the model
by default.  The context window expands on demand (interface definitions,
adjacent modules) to control token cost and reasoning noise.

### Observability

Every run logs a `run_id`, tool-call sequence, key intermediate results,
wall-clock time, and token usage, enabling post-hoc debugging of
false-positives or missed issues.
