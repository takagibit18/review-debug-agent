"""Unit tests for orchestrator loop behavior."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePath

from src.analyzer.event_log import EventType
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import AnalysisPlan, DebugRequest, ReviewRequest
from src.models.exceptions import ModelTimeoutError
from src.orchestrator.agent_loop import AgentOrchestrator
from src.tools.base import BaseTool, ToolRegistry, ToolSafety, ToolSpec
from src.tools.file_read import FileReadTool
from src.tools.grep_tool import GrepTool
from src.tools.glob_tool import GlobTool
from src.tools.list_dir_tool import ListDirTool


class DummyEchoTool(BaseTool):
    """Simple test tool that echoes its input."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="echo_tool",
            description="Echo payload",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs):
        return {"echo": kwargs.get("value", "")}


class DummyWriteTool(BaseTool):
    """Write-safety tool used to verify security gating behavior."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_tool",
            description="Write payload",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            safety=ToolSafety.WRITE,
        )

    async def execute(self, **kwargs):
        return {"wrote": kwargs.get("value", "")}


class DummyExecuteTool(BaseTool):
    """Execute-safety tool used to verify high-risk gating behavior."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="execute_tool",
            description="Execute payload",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
            safety=ToolSafety.EXECUTE,
        )

    async def execute(self, **kwargs):
        return {"executed": kwargs.get("value", "")}


class SlowReadonlyTool(BaseTool):
    """Readonly tool that sleeps to make concurrency observable."""

    def __init__(self, name: str, events: list[str], delay: float) -> None:
        self._name = name
        self._events = events
        self._delay = delay

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            description="Slow readonly tool",
            parameters={"type": "object", "properties": {}},
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs):
        await asyncio.sleep(self._delay)
        self._events.append(self._name)
        return {"name": self._name}


def test_review_run_stops_after_single_iteration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_MAX_ITERATIONS", "1")
    monkeypatch.chdir(tmp_path)
    orchestrator = AgentOrchestrator()
    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    continue_steps = [step for step in response.context.decisions if step.phase == "continue"]
    assert len(continue_steps) == 1
    assert continue_steps[-1].result in {
        "stop:model_completed",
        "stop:max_iterations",
    }
    assert response.run_id


def test_review_iterations_respect_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REVIEW_MAX_ITERATIONS", "2")
    monkeypatch.chdir(tmp_path)
    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry)

    async def _always_needs_tool(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            needs_tools=True,
            tool_calls=[
                {
                    "function": {
                        "name": "echo_tool",
                        "arguments": '{"value":"iteration"}',
                    }
                }
            ],
        )

    monkeypatch.setattr(orchestrator, "analyze", _always_needs_tool)
    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    continue_steps = [step for step in response.context.decisions if step.phase == "continue"]
    assert len(continue_steps) == 2
    assert continue_steps[-1].result in {"stop:max_iterations", "stop:budget_hard_capped"}


def test_review_min_tool_iterations_defers_first_round_submit(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    orchestrator = AgentOrchestrator(
        review_max_iterations=2,
        review_min_tool_iterations=1,
    )
    analyze_calls = 0

    async def _submits_without_tools(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal analyze_calls
        analyze_calls += 1
        return AnalysisPlan(
            needs_tools=False,
            tool_calls=[],
            draft_review=ReviewReport(summary="draft", issues=[]),
        )

    monkeypatch.setattr(orchestrator, "analyze", _submits_without_tools)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    continue_steps = [step for step in response.context.decisions if step.phase == "continue"]
    assert analyze_calls == 2
    assert continue_steps[0].result == "continue"
    assert continue_steps[-1].result == "stop:max_iterations"


def test_debug_run_stops_at_iteration_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry)

    async def _always_needs_tool(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            needs_tools=True,
            tool_calls=[
                {
                    "function": {
                        "name": "echo_tool",
                        "arguments": '{"value":"iteration"}',
                    }
                }
            ],
        )

    monkeypatch.setattr(orchestrator, "analyze", _always_needs_tool)
    response = asyncio.run(orchestrator.run_debug(DebugRequest(repo_path=".")))

    continue_steps = [step for step in response.context.decisions if step.phase == "continue"]
    assert len(continue_steps) == 3
    assert continue_steps[-1].result in {"stop:max_iterations", "stop:budget_hard_capped"}


def test_event_log_directory_is_relative_to_repo_path(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    workspace.mkdir()
    repo.mkdir()
    monkeypatch.chdir(workspace)
    orchestrator = AgentOrchestrator()

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=str(repo))))
    log_path = repo / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    assert log_path.exists()


def test_execute_tools_uses_registry(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry)
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "echo_tool",
                    "arguments": '{"value":"ok"}',
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].data == {"echo": "ok"}


def test_execute_tools_blocks_write_without_confirmation() -> None:
    registry = ToolRegistry()
    registry.register(DummyWriteTool())
    orchestrator = AgentOrchestrator(registry=registry)
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "write_tool",
                    "arguments": '{"value":"blocked"}',
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))
    assert len(results) == 1
    assert results[0].ok is False
    assert "confirmation" in (results[0].error or "").lower()
    assert any(error.category == "security" for error in state.errors)


def test_execute_tools_blocks_execute_without_confirmation() -> None:
    registry = ToolRegistry()
    registry.register(DummyExecuteTool())
    orchestrator = AgentOrchestrator(registry=registry)
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "execute_tool",
                    "arguments": '{"value":"blocked"}',
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))
    assert len(results) == 1
    assert results[0].ok is False
    assert "confirmation" in (results[0].error or "").lower()
    assert any(error.category == "security" for error in state.errors)


def test_execute_tools_allows_execute_with_confirmation(monkeypatch) -> None:
    # GitHub Actions sets CI=true by default; this case validates interactive mode.
    # We explicitly clear CI so confirmation callback can allow execution.
    monkeypatch.delenv("CI", raising=False)

    registry = ToolRegistry()
    registry.register(DummyExecuteTool())
    orchestrator = AgentOrchestrator(
        registry=registry,
        confirm_high_risk=lambda tool_spec, arguments: True,
    )
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "execute_tool",
                    "arguments": '{"value":"ok"}',
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))
    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].data == {"executed": "ok"}


def test_execute_tools_rejects_execute_when_ci_is_true(monkeypatch) -> None:
    monkeypatch.setenv("CI", "true")
    registry = ToolRegistry()
    registry.register(DummyExecuteTool())
    orchestrator = AgentOrchestrator(
        registry=registry,
        confirm_high_risk=lambda tool_spec, arguments: True,
    )
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "execute_tool",
                    "arguments": '{"value":"blocked-by-ci"}',
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))
    assert len(results) == 1
    assert results[0].ok is False
    assert any(error.category == "security" for error in state.errors)


def test_execute_tools_does_not_confirm_readonly_tools() -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def _confirm(tool_spec, arguments):  # type: ignore[no-untyped-def]
        calls.append((tool_spec.name, arguments))
        return True

    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry, confirm_high_risk=_confirm)
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "echo_tool",
                    "arguments": '{"value":"readonly"}',
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert len(results) == 1
    assert results[0].ok is True
    assert calls == []


def test_execute_tools_supports_file_read_tool(monkeypatch) -> None:
    registry = ToolRegistry()
    registry.register(FileReadTool())
    orchestrator = AgentOrchestrator(registry=registry)
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    target_file = Path(__file__).resolve()
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "read_file",
                    "arguments": (
                        '{"file_path": "' + str(target_file).replace("\\", "\\\\") + '", "limit": 1}'
                    ),
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].data["file_path"] == str(target_file)
    assert results[0].data["content"].startswith('1: """Unit tests for orchestrator loop behavior."""')


def test_execute_tools_wraps_readonly_tool_errors() -> None:
    registry = ToolRegistry()
    registry.register(FileReadTool())
    orchestrator = AgentOrchestrator(registry=registry)
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    missing_file = Path(__file__).resolve().parent / "missing-file.txt"
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path": "'
                    + str(missing_file.resolve()).replace("\\", "\\\\")
                    + '"}',
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert len(results) == 1
    assert results[0].ok is False
    assert "Tool execution failed for read_file" in (results[0].error or "")
    assert any(error.category == "runtime" for error in state.errors)


def test_execute_tools_supports_glob_tool() -> None:
    registry = ToolRegistry()
    registry.register(GlobTool())
    orchestrator = AgentOrchestrator(registry=registry)
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    repo_root = Path(__file__).resolve().parent.parent
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "glob_files",
                    "arguments": (
                        '{"pattern": "tests/test_file_read_tool.py", "path": "'
                        + str(repo_root).replace("\\", "\\\\")
                        + '"}'
                    ),
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].data["match_count"] == 1
    assert PurePath(results[0].data["matches"][0]).parts[-2:] == (
        "tests",
        "test_file_read_tool.py",
    )


def test_execute_tools_supports_grep_tool() -> None:
    registry = ToolRegistry()
    registry.register(GrepTool())
    orchestrator = AgentOrchestrator(registry=registry)
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    repo_root = Path(__file__).resolve().parent.parent
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "grep_files",
                    "arguments": (
                        '{"pattern": "test_file_read_tool_reads_full_file", "glob": '
                        '"tests/test_file_read_tool.py", "path": "'
                        + str(repo_root).replace("\\", "\\\\")
                        + '"}'
                    ),
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert len(results) == 1
    assert results[0].ok is True
    assert results[0].data["match_count"] == 1
    assert PurePath(results[0].data["matches"][0]["file_path"]).parts[-2:] == (
        "tests",
        "test_file_read_tool.py",
    )
    assert "test_file_read_tool_reads_full_file" in results[0].data["matches"][0]["line_text"]


def test_execute_tools_supports_list_dir_tool() -> None:
    registry = ToolRegistry()
    registry.register(ListDirTool())
    orchestrator = AgentOrchestrator(registry=registry)
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    target_dir = Path(__file__).resolve().parent.parent / "src" / "tools"
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "list_dir",
                    "arguments": '{"path": "'
                    + str(target_dir.resolve()).replace("\\", "\\\\")
                    + '", "limit": 20}',
                }
            }
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert len(results) == 1
    assert results[0].ok is True
    names = {entry["name"] for entry in results[0].data["entries"]}
    assert "file_read.py" in names
    assert "grep_tool.py" in names


def test_execute_tools_runs_readonly_batch_concurrently_and_preserves_result_order() -> None:
    events: list[str] = []
    registry = ToolRegistry()
    registry.register(SlowReadonlyTool("slow_tool", events, delay=0.05))
    registry.register(SlowReadonlyTool("fast_tool", events, delay=0.0))
    orchestrator = AgentOrchestrator(registry=registry)
    orchestrator._reset_run(max_iterations=1, repo_path=".")  # noqa: SLF001
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {"function": {"name": "slow_tool", "arguments": "{}"}},
            {"function": {"name": "fast_tool", "arguments": "{}"}},
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert [result.data["name"] for result in results if result.ok] == ["slow_tool", "fast_tool"]
    assert events == ["fast_tool", "slow_tool"]


def test_execute_tools_uses_repo_root_for_path_checks_when_cwd_differs(monkeypatch) -> None:
    registry = ToolRegistry()
    registry.register(FileReadTool())
    orchestrator = AgentOrchestrator(registry=registry)
    repo_root = Path(__file__).resolve().parent.parent
    monkeypatch.chdir(Path(__file__).resolve().parent)
    orchestrator._reset_run(max_iterations=1, repo_path=str(repo_root))  # noqa: SLF001
    state = orchestrator.prepare_context(ReviewRequest(repo_path=str(repo_root)))
    allowed_path = (repo_root / "src" / "tools" / "base.py").resolve()
    denied_path = repo_root.parent
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path": "'
                    + str(allowed_path).replace("\\", "\\\\")
                    + '", "limit": 1}',
                }
            },
            {
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path": "'
                    + str(denied_path).replace("\\", "\\\\")
                    + '"}',
                }
            },
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert results[0].ok is True
    assert results[0].data["file_path"] == str(allowed_path)
    assert results[1].ok is False
    assert "outside the allowed workspace" in (results[1].error or "")


def test_plan_mode_skips_tool_execution_even_when_plan_requests_tools(monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_MAX_ITERATIONS", "1")
    calls: list[str] = []

    class _CountingTool(BaseTool):
        def spec(self) -> ToolSpec:
            return ToolSpec(
                name="count_tool",
                description="Count calls",
                parameters={"type": "object", "properties": {}},
                safety=ToolSafety.READONLY,
            )

        async def execute(self, **kwargs):
            calls.append("called")
            return {"ok": True}

    registry = ToolRegistry()
    registry.register(_CountingTool())
    orchestrator = AgentOrchestrator(registry=registry, permission_mode="plan")

    async def _always_needs_tool(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(
            needs_tools=True,
            tool_calls=[{"function": {"name": "count_tool", "arguments": "{}"}}],
        )

    monkeypatch.setattr(orchestrator, "analyze", _always_needs_tool)
    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    execute_steps = [step for step in response.context.decisions if step.phase == "execute_tools"]
    assert execute_steps[-1].result == "Plan mode: tool execution disabled"
    assert "plan_mode" in response.context.constraints
    assert calls == []


def test_execute_tools_emits_tool_io_event_with_iteration(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_TRACE_DETAIL", "compact")
    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry)
    orchestrator._reset_run(max_iterations=1, repo_path=".")  # noqa: SLF001
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {"function": {"name": "echo_tool", "arguments": '{"value":"trace-check"}'}},
        ],
    )

    asyncio.run(orchestrator.execute_tools(plan, registry, state))
    log_path = tmp_path / ".mergewarden" / "logs" / f"{orchestrator._run_id}.jsonl"  # noqa: SLF001
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tool_io = next(item for item in events if item["event_type"] == EventType.TOOL_IO.value)
    assert tool_io["payload"]["iteration"] == 0
    assert tool_io["payload"]["name"] == "echo_tool"


def test_execute_tools_times_out_slow_readonly_tool_and_logs_duration(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.01")
    events_seen: list[str] = []
    registry = ToolRegistry()
    registry.register(SlowReadonlyTool("slow_tool", events_seen, delay=0.05))
    orchestrator = AgentOrchestrator(registry=registry)
    orchestrator._reset_run(max_iterations=1, repo_path=".")  # noqa: SLF001
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[{"function": {"name": "slow_tool", "arguments": "{}"}}],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert events_seen == []
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].data["error_type"] == "ToolTimeoutError"
    assert "timed out" in (results[0].error or "")
    assert any(error.category == "runtime" and "timed out" in error.message for error in state.errors)
    log_path = tmp_path / ".mergewarden" / "logs" / f"{orchestrator._run_id}.jsonl"  # noqa: SLF001
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    tool_call = next(
        item
        for item in events
        if item["event_type"] == EventType.TOOL_CALL.value
        and item["phase"] == "execute_tools"
    )
    assert tool_call["payload"]["ok"] is False
    assert tool_call["payload"]["skip_reason"] == "tool_timeout"
    assert tool_call["payload"]["elapsed_ms"] >= 1


def test_format_result_emits_format_result_event(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_TRACE_DETAIL", "compact")
    orchestrator = AgentOrchestrator()
    orchestrator._reset_run(max_iterations=1, repo_path=".")  # noqa: SLF001
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))

    orchestrator.format_result(state, tool_results=[])
    log_path = tmp_path / ".mergewarden" / "logs" / f"{orchestrator._run_id}.jsonl"  # noqa: SLF001
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    format_event = next(
        item for item in events if item["event_type"] == EventType.FORMAT_RESULT.value
    )
    assert format_event["payload"]["iteration"] == 0
    assert "used_placeholder_summary" in format_event["payload"]


def test_soft_budget_skips_force_submit_finalize(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKEN_BUDGET", "10")
    orchestrator = AgentOrchestrator()
    analyze_calls: list[bool] = []

    async def _soft_cap_without_submit(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        analyze_calls.append(bool(kwargs.get("force_submit")))
        orchestrator._latest_tokens = 10  # noqa: SLF001
        return AnalysisPlan(needs_tools=False, tool_calls=[])

    monkeypatch.setattr(orchestrator, "analyze", _soft_cap_without_submit)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    assert analyze_calls == [False]
    assert response.context.decisions[-1].result == "stop:budget_soft_capped"
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    finalize_event = next(
        item for item in events if item["event_type"] == EventType.DECISION.value and item["phase"] == "finalize"
    )
    assert finalize_event["payload"]["finalize_attempt"] is False
    assert finalize_event["payload"]["skip_reason"] == "budget_soft_capped"


def test_soft_budget_returns_submitted_compatibility_warning(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKEN_BUDGET", "10")
    orchestrator = AgentOrchestrator()

    async def _soft_cap_with_submitted_issue(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        orchestrator._latest_tokens = 10  # noqa: SLF001
        return AnalysisPlan(
            draft_review=ReviewReport(
                summary="Partial review found a compatibility risk.",
                issues=[
                    ReviewIssue(
                        severity=Severity.WARNING,
                        location="src/_pytest/python.py:1200",
                        evidence=(
                            "if modified_val is None or len(modified_val) > 100:\n"
                            "        return str(argname) + str(idx)"
                        ),
                        suggestion=(
                            "The limit silently truncates long parameter IDs and can "
                            "break test selection scripts."
                        ),
                        confidence=0.75,
                    )
                ],
            )
        )

    monkeypatch.setattr(orchestrator, "analyze", _soft_cap_with_submitted_issue)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    assert response.context.decisions[-1].result == "stop:budget_soft_capped"
    assert [issue.location for issue in response.report.issues] == [
        "src/_pytest/python.py:1200"
    ]


def test_run_timeout_stops_and_skips_force_submit_finalize(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "0.001")
    orchestrator = AgentOrchestrator()
    analyze_calls: list[bool] = []

    async def _slow_without_submit(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        analyze_calls.append(bool(kwargs.get("force_submit")))
        await asyncio.sleep(0.01)
        return AnalysisPlan(needs_tools=False, tool_calls=[])

    monkeypatch.setattr(orchestrator, "analyze", _slow_without_submit)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    assert analyze_calls == [False]
    assert response.context.decisions[-1].result == "stop:run_timeout"
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    continue_event = next(
        item for item in events if item["event_type"] == EventType.DECISION.value and item["phase"] == "continue"
    )
    finalize_event = next(
        item for item in events if item["event_type"] == EventType.DECISION.value and item["phase"] == "finalize"
    )
    assert continue_event["payload"]["run_timed_out"] is True
    assert finalize_event["payload"]["finalize_attempt"] is False
    assert finalize_event["payload"]["skip_reason"] == "run_timeout"


def test_model_timeout_skips_force_submit_finalize(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    orchestrator = AgentOrchestrator()

    class _TimeoutEngine:
        def __init__(self) -> None:
            self.force_submit_calls: list[bool] = []

        async def analyze(self, **kwargs):  # type: ignore[no-untyped-def]
            self.force_submit_calls.append(bool(kwargs.get("force_submit")))
            raise ModelTimeoutError("provider timed out", code="timeout")

    engine = _TimeoutEngine()
    monkeypatch.setattr(orchestrator, "_build_engine", lambda: engine)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    assert engine.force_submit_calls == [False]
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    error_event = next(
        item for item in events if item["event_type"] == EventType.ERROR.value and item["phase"] == "analyze"
    )
    finalize_event = next(
        item for item in events if item["event_type"] == EventType.DECISION.value and item["phase"] == "finalize"
    )
    model_event = next(
        item for item in events if item["event_type"] == EventType.MODEL_CALL.value and item["phase"] == "analyze"
    )
    assert error_event["payload"]["error_type"] == "ModelTimeoutError"
    assert model_event["payload"]["model_request_timeout_seconds"] == 90.0
    assert model_event["payload"]["model_max_retries"] == 1
    assert model_event["payload"]["force_submit"] is False
    assert model_event["payload"]["budget_state"] == "none"
    assert finalize_event["payload"]["finalize_attempt"] is False
    assert finalize_event["payload"]["skip_reason"] == "model_timeout"


def test_prepare_start_logs_runtime_budget_and_timeout_settings(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKEN_BUDGET", "26000")
    monkeypatch.setenv("TOKEN_HARD_BUDGET", "32000")
    monkeypatch.setenv("PROMPT_INPUT_TOKEN_BUDGET", "28000")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("AGENT_RUN_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("MODEL_MAX_TOKENS", "1024")
    monkeypatch.setenv("REVIEW_DIFF_FIRST_CHANGED_FILES", "1")

    orchestrator = AgentOrchestrator()
    orchestrator._reset_run(max_iterations=1, repo_path=".")  # noqa: SLF001

    log_path = tmp_path / ".mergewarden" / "logs" / f"{orchestrator._run_id}.jsonl"  # noqa: SLF001
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    start_event = next(
        item for item in events if item["event_type"] == EventType.PHASE_START.value
    )

    assert start_event["payload"]["token_budget"] == 26000
    assert start_event["payload"]["token_hard_budget"] == 32000
    assert start_event["payload"]["prompt_input_token_budget"] == 28000
    assert start_event["payload"]["model_request_timeout_seconds"] == 45.0
    assert start_event["payload"]["agent_run_timeout_seconds"] == 90.0
    assert start_event["payload"]["agent_tool_timeout_seconds"] == 12.0
    assert start_event["payload"]["model_max_tokens"] == 1024
    assert start_event["payload"]["review_diff_first_changed_files"] is True


def test_review_diff_first_prefetch_reads_changed_files_before_model(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_DIFF_FIRST_CHANGED_FILES", "1")
    repo = tmp_path / "repo"
    changed = repo / "src" / "module.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("print('changed')\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(FileReadTool())
    orchestrator = AgentOrchestrator(registry=registry)
    feedback_seen_by_model: list[list[str]] = []

    async def _assert_prefetched_feedback(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        feedback_seen_by_model.append(
            [
                item["tool_call"]["function"]["name"]
                for item in orchestrator._tool_feedback  # noqa: SLF001
            ]
        )
        return AnalysisPlan(needs_tools=False, tool_calls=[])

    monkeypatch.setattr(orchestrator, "analyze", _assert_prefetched_feedback)
    diff_text = (
        "diff --git a/src/module.py b/src/module.py\n"
        "--- a/src/module.py\n"
        "+++ b/src/module.py\n"
        "@@ -1 +1 @@\n"
        "+print('changed')\n"
    )

    asyncio.run(
        orchestrator.run_review(
            ReviewRequest(repo_path=str(repo), diff_mode=True, diff_text=diff_text)
        )
    )

    assert feedback_seen_by_model[0] == ["read_file"]
    first_feedback_call = orchestrator._tool_feedback[0]["tool_call"]  # noqa: SLF001
    assert first_feedback_call["type"] == "function"
    assert first_feedback_call["id"].startswith("prefetch-read-file-")
    prefetch_args = json.loads(first_feedback_call["function"]["arguments"])
    assert prefetch_args == {"file_path": "src/module.py", "offset": 0, "limit": 80}
    log_path = repo / ".mergewarden" / "logs" / f"{orchestrator._run_id}.jsonl"  # noqa: SLF001
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    prefetch_event = next(
        item
        for item in events
        if item["event_type"] == EventType.DECISION.value
        and item["phase"] == "diff_first_prefetch"
    )
    assert prefetch_event["payload"]["enabled"] is True
    assert prefetch_event["payload"]["selected_files"] == ["src/module.py"]


def test_review_mode_registers_review_context_tools_from_diff_text(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    changed = repo / "src" / "module.py"
    changed.parent.mkdir(parents=True)
    changed.write_text("print('changed')\n", encoding="utf-8")
    orchestrator = AgentOrchestrator()
    seen_tool_names: list[set[str]] = []

    async def _capture_tool_specs(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        seen_tool_names.append({spec.name for spec in tool_specs})
        return AnalysisPlan(needs_tools=False, tool_calls=[])

    monkeypatch.setattr(orchestrator, "analyze", _capture_tool_specs)
    diff_text = (
        "diff --git a/src/module.py b/src/module.py\n"
        "--- a/src/module.py\n"
        "+++ b/src/module.py\n"
        "@@ -1 +1 @@\n"
        "-print('old')\n"
        "+print('changed')\n"
    )

    asyncio.run(
        orchestrator.run_review(
            ReviewRequest(repo_path=str(repo), diff_mode=True, diff_text=diff_text)
        )
    )

    assert {"get_changed_context", "find_symbol_context", "validate_review_draft"}.issubset(
        seen_tool_names[0]
    )


def test_external_registry_is_not_overridden_by_review_context_tools(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry)
    seen_tool_names: list[set[str]] = []

    async def _capture_tool_specs(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        seen_tool_names.append({spec.name for spec in tool_specs})
        return AnalysisPlan(needs_tools=False, tool_calls=[])

    monkeypatch.setattr(orchestrator, "analyze", _capture_tool_specs)

    asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text=(
                    "diff --git a/src/module.py b/src/module.py\n"
                    "--- a/src/module.py\n"
                    "+++ b/src/module.py\n"
                    "@@ -1 +1 @@\n"
                    "-old\n"
                    "+new\n"
                ),
            )
        )
    )

    assert seen_tool_names[0] == {"echo_tool"}


# ---------------------------------------------------------------------------
# Pre-budget submit-only path
# ---------------------------------------------------------------------------


def test_pre_budget_submit_triggers_when_budget_near_and_tool_feedback_exists(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKEN_BUDGET", "200")
    monkeypatch.setenv("TOKEN_HARD_BUDGET", "300")
    monkeypatch.setenv("PRE_BUDGET_SUBMIT_TOKEN_RATIO", "0.3")
    monkeypatch.setenv("REVIEW_MAX_ITERATIONS", "2")

    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry)
    analyze_calls: list[dict[str, bool]] = []

    async def _controlled_analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        force_submit = bool(kwargs.get("force_submit"))
        analyze_calls.append({"force_submit": force_submit})
        if force_submit:
            orchestrator._latest_tokens = 5  # noqa: SLF001
            return AnalysisPlan(needs_tools=False, tool_calls=[])
        orchestrator._latest_tokens = 70  # noqa: SLF001  # pushes total to 70 >= 200*0.3=60
        return AnalysisPlan(
            needs_tools=True,
            tool_calls=[{"function": {"name": "echo_tool", "arguments": "{}"}}],
        )

    monkeypatch.setattr(orchestrator, "analyze", _controlled_analyze)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    # Call 1: normal analyze → tool exec → format (total=70) → pre-budget fires
    # Call 2: pre-budget submit (force_submit=True) → draft absent → break
    # _maybe_force_submit skips (pre_budget_submit_attempted)
    assert len(analyze_calls) == 2
    assert analyze_calls[0]["force_submit"] is False
    assert analyze_calls[1]["force_submit"] is True

    # Verify pre_budget_submit event was logged
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    pre_budget_events = [
        item
        for item in events
        if item["event_type"] == EventType.DECISION.value
        and item["phase"] == "pre_budget_submit"
    ]
    assert len(pre_budget_events) == 2
    assert pre_budget_events[0]["payload"]["stage"] == "attempt"
    assert pre_budget_events[0]["payload"]["has_tool_feedback"] is True
    assert pre_budget_events[1]["payload"]["stage"] == "completed"


def test_pre_budget_submit_skips_when_budget_below_threshold(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKEN_BUDGET", "200")
    monkeypatch.setenv("PRE_BUDGET_SUBMIT_TOKEN_RATIO", "0.5")
    monkeypatch.setenv("REVIEW_MAX_ITERATIONS", "1")

    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry)
    analyze_calls: list[dict[str, bool]] = []

    async def _controlled_analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        force_submit = bool(kwargs.get("force_submit"))
        analyze_calls.append({"force_submit": force_submit})
        orchestrator._latest_tokens = 30  # noqa: SLF001  # well below 200*0.5=100
        return AnalysisPlan(
            needs_tools=True,
            tool_calls=[{"function": {"name": "echo_tool", "arguments": "{}"}}],
        )

    monkeypatch.setattr(orchestrator, "analyze", _controlled_analyze)

    asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    # Total = 30 < 100 threshold → no pre-budget submit fired
    # Loop breaks with reached_limit (max_iterations=1); _maybe_force_submit fires
    assert len(analyze_calls) == 2
    assert analyze_calls[0]["force_submit"] is False
    assert analyze_calls[1]["force_submit"] is True


def test_pre_budget_submit_skips_without_tool_feedback(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKEN_BUDGET", "100")
    monkeypatch.setenv("PRE_BUDGET_SUBMIT_TOKEN_RATIO", "0.3")

    orchestrator = AgentOrchestrator()
    analyze_calls: list[dict[str, bool]] = []

    async def _no_tool_state(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        force_submit = bool(kwargs.get("force_submit"))
        analyze_calls.append({"force_submit": force_submit})
        orchestrator._latest_tokens = 50  # noqa: SLF001  # >= 100*0.3=30
        # No tool calls → no tool_feedback accumulated
        return AnalysisPlan(needs_tools=False, tool_calls=[])

    monkeypatch.setattr(orchestrator, "analyze", _no_tool_state)

    asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    # Budget crossed threshold but no tool feedback → pre-budget should not fire
    # Call 1: normal analyze (force_submit=False)
    # Call 2: _maybe_force_submit (force_submit=True) because no draft was submitted
    assert len(analyze_calls) == 2
    assert analyze_calls[0]["force_submit"] is False
    assert analyze_calls[1]["force_submit"] is True


def test_empty_review_draft_allows_force_submit_finalize(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_MAX_ITERATIONS", "1")

    orchestrator = AgentOrchestrator()
    analyze_calls: list[dict[str, bool]] = []

    async def _empty_then_final(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        force_submit = bool(kwargs.get("force_submit"))
        analyze_calls.append({"force_submit": force_submit})
        if force_submit:
            return AnalysisPlan(
                draft_review=ReviewReport(summary="No issues found.", issues=[]),
            )
        return AnalysisPlan(draft_review=ReviewReport(summary="", issues=[]))

    monkeypatch.setattr(orchestrator, "analyze", _empty_then_final)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    assert [item["force_submit"] for item in analyze_calls] == [False, True]
    assert response.report.summary == "No issues found."


def test_hard_cap_still_skips_extra_finalize(tmp_path, monkeypatch) -> None:
    """Hard cap must never trigger an extra model call in finalize."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKEN_BUDGET", "100")
    monkeypatch.setenv("TOKEN_HARD_BUDGET", "150")

    orchestrator = AgentOrchestrator()
    analyze_calls: list[dict[str, bool]] = []

    async def _blow_budget(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        force_submit = bool(kwargs.get("force_submit"))
        analyze_calls.append({"force_submit": force_submit})
        orchestrator._latest_tokens = 200  # noqa: SLF001  # exceeds hard cap
        return AnalysisPlan(needs_tools=False, tool_calls=[])

    monkeypatch.setattr(orchestrator, "analyze", _blow_budget)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    # One call → hard_capped → finalize skipped
    assert len(analyze_calls) == 1
    assert response.context.decisions[-1].result == "stop:budget_hard_capped"
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    finalize_event = next(
        item
        for item in events
        if item["event_type"] == EventType.DECISION.value
        and item["phase"] == "finalize"
    )
    assert finalize_event["payload"]["finalize_attempt"] is False
    assert finalize_event["payload"]["skip_reason"] == "budget_hard_capped"


def test_model_timeout_still_skips_extra_finalize(tmp_path, monkeypatch) -> None:
    """Model timeout in _maybe_force_submit path does not cause extra retry."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TOKEN_BUDGET", "200")
    monkeypatch.setenv("PRE_BUDGET_SUBMIT_TOKEN_RATIO", "0.2")
    monkeypatch.setenv("TOKEN_HARD_BUDGET", "500")
    monkeypatch.setenv("REVIEW_MAX_ITERATIONS", "1")

    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry)

    class _TimeoutOnSubmitEngine:
        def __init__(self) -> None:
            self.call_count = 0
            self.force_submit_calls: list[bool] = []

        async def analyze(self, **kwargs):  # type: ignore[no-untyped-def]
            self.call_count += 1
            self.force_submit_calls.append(bool(kwargs.get("force_submit")))
            if self.call_count == 1:
                return (
                    AnalysisPlan(
                        needs_tools=True,
                        tool_calls=[{"function": {"name": "echo_tool", "arguments": "{}"}}],
                    ),
                    100,
                    "",
                )
            raise ModelTimeoutError("provider timed out after 90s", code="timeout")

    engine = _TimeoutOnSubmitEngine()
    monkeypatch.setattr(orchestrator, "_build_engine", lambda: engine)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    # Call 1: normal analyze (force_submit=False) → token budget crossed but
    # should_continue returns False (reached_limit) before pre_budget check
    # Call 2: _maybe_force_submit → analyze(force_submit=True) → timeout caught,
    # fallback plan returned, finalize_attempt=True logged
    assert engine.force_submit_calls == [False, True]

    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    # Pre-budget is NOT fired: should_continue returns False before the check
    pre_budget_events = [
        item
        for item in events
        if item["event_type"] == EventType.DECISION.value
        and item["phase"] == "pre_budget_submit"
    ]
    assert len(pre_budget_events) == 0

    # _maybe_force_submit fires but times out; method completes normally
    finalize_event = next(
        item
        for item in events
        if item["event_type"] == EventType.DECISION.value
        and item["phase"] == "finalize"
    )
    assert finalize_event["payload"]["finalize_attempt"] is True
    assert finalize_event["payload"]["finalize_submit_seen"] is False

    # Verify model timeout was logged
    error_event = next(
        item for item in events
        if item["event_type"] == EventType.ERROR.value
        and item["phase"] == "analyze"
    )
    assert error_event["payload"]["error_type"] == "ModelTimeoutError"


def test_length_truncated_empty_model_response_skips_extra_finalize(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_MAX_ITERATIONS", "3")

    orchestrator = AgentOrchestrator()
    analyze_calls: list[bool] = []

    async def _truncated_empty_plan(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        force_submit = bool(kwargs.get("force_submit"))
        analyze_calls.append(force_submit)
        plan = AnalysisPlan(
            needs_tools=False,
            tool_calls=[],
            incomplete_reason="model_finish_reason_length_no_output",
        )
        orchestrator._latest_tokens = 2148  # noqa: SLF001
        return plan

    monkeypatch.setattr(orchestrator, "analyze", _truncated_empty_plan)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    assert analyze_calls == [False]
    assert response.context.decisions[-1].result == "stop:model_incomplete"
    assert any(
        error.category == "runtime" and "length" in error.message
        for error in response.context.errors
    )
    log_path = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    finalize_event = next(
        item
        for item in events
        if item["event_type"] == EventType.DECISION.value
        and item["phase"] == "finalize"
    )
    assert finalize_event["payload"]["finalize_attempt"] is False
    assert finalize_event["payload"]["skip_reason"] == "model_incomplete"


def test_negative_fixture_fallback_emits_no_fabricated_high_confidence_issues(
    tmp_path, monkeypatch,
) -> None:
    """Placeholder fallback path must not fabricate critical/warning issues."""
    monkeypatch.chdir(tmp_path)

    orchestrator = AgentOrchestrator()

    async def _placeholder_only(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        return AnalysisPlan(needs_tools=False, tool_calls=[])

    monkeypatch.setattr(orchestrator, "analyze", _placeholder_only)

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    # Placeholder path: no draft → summary is the placeholder message
    assert "placeholder summary" in response.report.summary.lower()
    assert response.report.issues == []


def test_prepare_logs_pre_budget_submit_token_ratio(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PRE_BUDGET_SUBMIT_TOKEN_RATIO", "0.55")

    orchestrator = AgentOrchestrator()
    orchestrator._reset_run(max_iterations=1, repo_path=".")  # noqa: SLF001

    log_path = tmp_path / ".mergewarden" / "logs" / f"{orchestrator._run_id}.jsonl"  # noqa: SLF001
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    start_event = next(
        item for item in events if item["event_type"] == EventType.PHASE_START.value
    )
    assert start_event["payload"]["pre_budget_submit_token_ratio"] == 0.55
