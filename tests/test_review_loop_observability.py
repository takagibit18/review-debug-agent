"""Targeted tests for review-loop termination observability."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from eval.run_summary import extract_review_process_metrics
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.run_summary import summarize_event_log
from src.analyzer.schemas import AnalysisPlan, ReviewRequest
from src.models.exceptions import ModelTimeoutError
from src.orchestrator.agent_loop import AgentOrchestrator
from src.tools.base import BaseTool, ToolRegistry, ToolSafety, ToolSpec
from src.tools.file_read import FileReadTool


class _EchoTool(BaseTool):
    """Small readonly tool used to represent reviewer-requested exploration."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="echo_tool",
            description="Echo a value",
            parameters={"type": "object", "properties": {}},
            safety=ToolSafety.READONLY,
        )

    async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
        return {"value": kwargs.get("value", "ok")}


def _telemetry(tmp_path: Path, run_id: str) -> dict[str, object]:
    log_path = tmp_path / ".mergewarden" / "logs" / f"{run_id}.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return next(
        event["payload"]
        for event in events
        if event["event_type"] == "phase_end"
        and event["phase"] == "review_complete"
    )


def _orchestrator(
    registry: ToolRegistry | None = None,
    **kwargs: object,
) -> AgentOrchestrator:
    return AgentOrchestrator(
        registry=registry,
        context_mode="agent_search",
        finding_verifier_mode="off",
        review_workflow_enforcement="off",
        **kwargs,
    )


def test_natural_stop_records_review_and_tool_bearing_iterations(
    tmp_path: Path, monkeypatch
) -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    orchestrator = _orchestrator(registry, review_max_iterations=4)
    calls = 0

    async def _analyze(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        del state, request, tool_specs, kwargs
        calls += 1
        if calls == 1:
            return AnalysisPlan(
                needs_tools=True,
                tool_calls=[
                    {"function": {"name": "echo_tool", "arguments": "{}"}}
                ],
            )
        orchestrator._submit_review_seen_any = True  # noqa: SLF001
        return AnalysisPlan(draft_review=ReviewReport(summary="complete"))

    monkeypatch.setattr(orchestrator, "analyze", _analyze)
    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path)))
    )

    telemetry = _telemetry(tmp_path, response.run_id)
    assert telemetry["review_iterations"] == 2
    assert telemetry["tool_bearing_iterations"] == 1
    assert telemetry["submit_iteration"] == 1
    assert telemetry["natural_completion"] is True
    assert telemetry["iteration_guard_hit"] is False
    assert telemetry["termination_reason"] == "natural_model_stop"


def test_iteration_guard_is_distinguished_from_natural_stop(
    tmp_path: Path, monkeypatch
) -> None:
    registry = ToolRegistry()
    registry.register(_EchoTool())
    orchestrator = _orchestrator(registry, review_max_iterations=3)

    async def _always_explore(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        del state, request, tool_specs, kwargs
        return AnalysisPlan(
            needs_tools=True,
            tool_calls=[
                {"function": {"name": "echo_tool", "arguments": "{}"}}
            ],
        )

    monkeypatch.setattr(orchestrator, "analyze", _always_explore)
    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path)))
    )

    telemetry = _telemetry(tmp_path, response.run_id)
    assert telemetry["review_iterations"] == 3
    assert telemetry["tool_bearing_iterations"] == 3
    assert telemetry["iteration_guard_hit"] is True
    assert telemetry["natural_completion"] is False
    assert telemetry["termination_reason"] == "iteration_guard"


def test_pre_budget_submit_is_recorded_without_changing_loop_path(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TOKEN_BUDGET", "200")
    monkeypatch.setenv("TOKEN_HARD_BUDGET", "400")
    monkeypatch.setenv("FINAL_SUBMIT_RESERVE_TOKENS", "50")
    monkeypatch.setenv("PRE_BUDGET_SUBMIT_TOKEN_RATIO", "0.3")
    registry = ToolRegistry()
    registry.register(_EchoTool())
    orchestrator = _orchestrator(registry, review_max_iterations=4)
    calls = 0

    async def _near_budget(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        del state, request, tool_specs
        calls += 1
        orchestrator._latest_tokens = 70  # noqa: SLF001
        if kwargs.get("force_submit"):
            orchestrator._submit_review_seen_any = True  # noqa: SLF001
            return AnalysisPlan(draft_review=ReviewReport(summary="reserved submit"))
        return AnalysisPlan(
            needs_tools=True,
            tool_calls=[
                {"function": {"name": "echo_tool", "arguments": "{}"}}
            ],
        )

    monkeypatch.setattr(orchestrator, "analyze", _near_budget)
    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path)))
    )

    assert calls == 2
    telemetry = _telemetry(tmp_path, response.run_id)
    assert telemetry["pre_budget_submit_triggered"] is True
    assert telemetry["termination_reason"] == "pre_budget_submit"
    assert telemetry["natural_completion"] is False


def test_provider_timeout_is_not_reported_as_natural_completion(
    tmp_path: Path, monkeypatch
) -> None:
    orchestrator = _orchestrator(review_max_iterations=3)

    class _TimeoutEngine:
        async def analyze(self, **kwargs):  # type: ignore[no-untyped-def]
            del kwargs
            raise ModelTimeoutError("provider timed out", code="timeout")

    monkeypatch.setattr(orchestrator, "_build_engine", lambda: _TimeoutEngine())
    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path)))
    )

    telemetry = _telemetry(tmp_path, response.run_id)
    assert telemetry["natural_completion"] is False
    assert telemetry["termination_reason"] == "provider_timeout"


def test_synthetic_prefetch_does_not_count_as_tool_bearing_iteration(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("REVIEW_DIFF_FIRST_CHANGED_FILES", "1")
    (tmp_path / "module.py").write_text("return 1\n", encoding="utf-8")
    registry = ToolRegistry()
    registry.register(FileReadTool())
    orchestrator = _orchestrator(registry, review_max_iterations=3)

    async def _submit(state, request, tool_specs, **kwargs):  # type: ignore[no-untyped-def]
        del state, request, tool_specs, kwargs
        orchestrator._submit_review_seen_any = True  # noqa: SLF001
        return AnalysisPlan(draft_review=ReviewReport(summary="prefetched"))

    monkeypatch.setattr(orchestrator, "analyze", _submit)
    response = asyncio.run(
        orchestrator.run_review(
            ReviewRequest(
                repo_path=str(tmp_path),
                diff_mode=True,
                diff_text="diff --git a/module.py b/module.py\n+++ b/module.py\n@@ -1 +1 @@\n-return 0\n+return 1\n",
            )
        )
    )

    telemetry = _telemetry(tmp_path, response.run_id)
    assert telemetry["tool_call_count"] == 1
    assert telemetry["tool_bearing_iterations"] == 0
    assert telemetry["review_iterations"] == 1


def test_old_event_log_defaults_new_metrics_without_breaking_parsing(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "legacy.jsonl"
    events = [
        {
            "run_id": "legacy",
            "event_type": "decision",
            "phase": "continue",
            "payload": {"reason": "model_completed"},
        },
        {
            "run_id": "legacy",
            "event_type": "phase_end",
            "phase": "review_complete",
            "payload": {
                "review_iterations": 2,
                "tool_call_count": 3,
                "submit_review_seen_any": True,
            },
        },
    ]
    log_path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )

    runtime_summary = summarize_event_log(log_path)
    process_metrics = extract_review_process_metrics(log_path)

    assert runtime_summary.actual_review_iterations == 2
    assert runtime_summary.tool_bearing_iterations == 0
    assert runtime_summary.submit_iteration is None
    assert runtime_summary.termination_reason == "natural_model_stop"
    assert process_metrics.actual_review_iterations == 2
    assert process_metrics.tool_bearing_iterations == 0
    assert process_metrics.submit_iteration is None
    assert process_metrics.termination_reason == "natural_model_stop"
