"""Unit tests for orchestrator loop behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path, PurePath

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


class TimedReadonlyTool(BaseTool):
    """Readonly tool with delay, used for concurrency tests."""

    def __init__(self, name: str, trace: list[str], delay: float = 0.02) -> None:
        self._name = name
        self._trace = trace
        self._delay = delay

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self._name,
            description="Timed readonly tool",
            parameters={"type": "object"},
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
        self._trace.append(f"start:{self._name}")
        await asyncio.sleep(self._delay)
        self._trace.append(f"end:{self._name}")
        return {"name": self._name}


class TraceWriteTool(BaseTool):
    """Write tool that records serial execution boundaries."""

    def __init__(self, trace: list[str]) -> None:
        self._trace = trace

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_trace_tool",
            description="Trace write execution",
            parameters={"type": "object"},
            safety=ToolSafety.WRITE,
        )

    async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
        self._trace.append("start:write_trace_tool")
        self._trace.append("end:write_trace_tool")
        return {"name": "write_trace_tool"}


def test_review_run_stops_after_single_iteration(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    orchestrator = AgentOrchestrator()
    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=".")))

    continue_steps = [step for step in response.context.decisions if step.phase == "continue"]
    assert len(continue_steps) == 1
    assert response.context.decisions[-1].result in {
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

    async def _always_needs_tool(state, request, tool_specs):  # type: ignore[no-untyped-def]
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
    assert continue_steps[-1].result == "stop:max_iterations"


def test_debug_run_stops_at_iteration_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    registry = ToolRegistry()
    registry.register(DummyEchoTool())
    orchestrator = AgentOrchestrator(registry=registry)

    async def _always_needs_tool(state, request, tool_specs):  # type: ignore[no-untyped-def]
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
    assert continue_steps[-1].result == "stop:max_iterations"


def test_event_log_directory_is_relative_to_repo_path(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    repo = workspace / "repo"
    workspace.mkdir()
    repo.mkdir()
    monkeypatch.chdir(workspace)
    orchestrator = AgentOrchestrator()

    response = asyncio.run(orchestrator.run_review(ReviewRequest(repo_path=str(repo))))
    log_path = repo / ".cr-debug-agent" / "logs" / f"{response.run_id}.jsonl"
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


def test_execute_tools_parallelizes_contiguous_safe_calls_and_serializes_write() -> None:
    trace: list[str] = []
    registry = ToolRegistry()
    registry.register(TimedReadonlyTool("read_a", trace))
    registry.register(TimedReadonlyTool("read_b", trace))
    registry.register(TraceWriteTool(trace))
    registry.register(TimedReadonlyTool("read_c", trace))
    registry.register(TimedReadonlyTool("read_d", trace))

    orchestrator = AgentOrchestrator(
        registry=registry,
        confirm_high_risk=lambda _spec, _args: True,
    )
    state = orchestrator.prepare_context(ReviewRequest(repo_path="."))
    plan = AnalysisPlan(
        needs_tools=True,
        tool_calls=[
            {"function": {"name": "read_a", "arguments": "{}"}},
            {"function": {"name": "read_b", "arguments": "{}"}},
            {"function": {"name": "write_trace_tool", "arguments": "{}"}},
            {"function": {"name": "read_c", "arguments": "{}"}},
            {"function": {"name": "read_d", "arguments": "{}"}},
        ],
    )

    results = asyncio.run(orchestrator.execute_tools(plan, registry, state))

    assert [result.data["name"] for result in results] == [
        "read_a",
        "read_b",
        "write_trace_tool",
        "read_c",
        "read_d",
    ]

    idx_end_read_batch_1 = max(trace.index("end:read_a"), trace.index("end:read_b"))
    idx_start_write = trace.index("start:write_trace_tool")
    idx_end_write = trace.index("end:write_trace_tool")
    idx_start_read_c = trace.index("start:read_c")
    idx_start_read_d = trace.index("start:read_d")
    assert idx_start_write > idx_end_read_batch_1
    assert idx_start_read_c > idx_end_write
    assert idx_start_read_d > idx_end_write

    first_batch_start = {trace[0], trace[1]}
    assert first_batch_start == {"start:read_a", "start:read_b"}


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
