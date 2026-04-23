"""Unit tests for orchestrator loop behavior."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePath

from src.analyzer.event_log import EventType
from src.analyzer.schemas import AnalysisPlan, DebugRequest, ReviewRequest
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
