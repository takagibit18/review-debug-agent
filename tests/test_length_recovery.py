"""Regression tests for structured review recovery after output truncation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from eval.run_summary import extract_review_process_metrics
from eval.runner import _read_event_log_stats
from src.analyzer.review_failures import find_blocking_review_error
from src.analyzer.run_summary import summarize_event_log
from src.analyzer.schemas import ReviewRequest
from src.models.schemas import ModelConfig, ModelResponse, TokenUsage
from src.orchestrator.agent_loop import AgentOrchestrator
from src.orchestrator.run_journal import RunJournal
from src.tools.base import BaseTool, ToolRegistry, ToolSpec


def _submit_response(
    summary: str = "No supported issues after recovery.",
) -> ModelResponse:
    return ModelResponse(
        tool_calls=[
            {
                "id": "submit-call",
                "function": {
                    "name": "submit_review",
                    "arguments": json.dumps({"summary": summary, "issues": []}),
                },
            }
        ],
        usage=TokenUsage(total_tokens=20),
        model="fake-model",
        finish_reason="tool_calls",
    )


class _SequenceClient:
    """Return a fixed response sequence while recording recovery requests."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.default_config = ModelConfig(model="fake-model")
        self._responses = responses
        self.messages: list[list[Any]] = []
        self.tools: list[list[dict[str, Any]] | None] = []
        self.configs: list[ModelConfig | None] = []
        self.policies: list[Any] = []

    async def chat(
        self, messages, config=None, tools=None, policy=None, conversation=None
    ):  # type: ignore[no-untyped-def]
        self.messages.append(messages)
        self.tools.append(tools)
        self.configs.append(config)
        self.policies.append(policy)
        index = len(self.messages) - 1
        if index >= len(self._responses):
            raise AssertionError("Unexpected extra model call")
        return self._responses[index].model_copy(deep=True)


class _ReadEvidenceTool(BaseTool):
    """Return deterministic evidence for finalize-only handoff tests."""

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="read_file",
            description="Read evidence",
            parameters={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
            },
        )

    async def execute(self, **kwargs):  # type: ignore[no-untyped-def]
        return {
            "file_path": kwargs.get("file_path"),
            "content": "21: return self.obj == other",
        }


def _run_review(
    tmp_path: Path,
    client: _SequenceClient,
    *,
    registry: ToolRegistry | None = None,
) -> tuple[AgentOrchestrator, Any]:
    orchestrator = AgentOrchestrator(
        registry=registry,
        review_max_iterations=3,
        review_workflow_enforcement="off",
    )
    orchestrator._model_client = client  # type: ignore[assignment]  # noqa: SLF001
    response = asyncio.run(
        orchestrator.run_review(ReviewRequest(repo_path=str(tmp_path)))
    )
    return orchestrator, response


def _journal(orchestrator: AgentOrchestrator) -> list[Any]:
    journal = orchestrator._run_journal  # noqa: SLF001
    assert journal is not None
    return RunJournal(
        orchestrator._run_id,  # noqa: SLF001
        journal.path,
        fsync=False,
    ).replay()


def _tool_names(schemas: list[dict[str, Any]] | None) -> set[str]:
    return {
        str(item.get("function", {}).get("name", ""))
        for item in schemas or []
        if isinstance(item.get("function"), dict)
    }


def test_length_with_draft_and_tool_recovers_via_submit_only_context(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    initial = ModelResponse(
        tool_calls=[
            {
                "id": "draft-call",
                "function": {
                    "name": "record_draft_finding",
                    "arguments": json.dumps(
                        {
                            "file": "src/wrapper.py",
                            "line": 21,
                            "symbol": "SafeWrapper.__eq__",
                            "claim": "Equality may compare against the wrong peer object.",
                        }
                    ),
                },
            },
            {
                "id": "read-call",
                "function": {
                    "name": "read_file",
                    "arguments": '{"file_path":"src/wrapper.py"}',
                },
            },
        ],
        usage=TokenUsage(total_tokens=4096),
        model="fake-model",
        finish_reason="length",
    )
    client = _SequenceClient([initial, _submit_response()])
    registry = ToolRegistry()
    registry.register(_ReadEvidenceTool())

    orchestrator, response = _run_review(tmp_path, client, registry=registry)

    assert response.report.summary == "No supported issues after recovery."
    assert find_blocking_review_error(response) is None
    assert len(client.messages) == 2
    assert _tool_names(client.tools[1]) == {"submit_review"}
    assert client.configs[1] is not None
    assert client.policies[1].forced_tool == "submit_review"
    assert client.policies[1].thinking == "off"
    final_context = next(
        message.content
        for message in client.messages[1]
        if "final_submit_evidence_summary" in message.content
    )
    assert "Known draft findings:" in final_context
    assert "Equality may compare against the wrong peer object" in final_context
    assert "tool_evidence" in final_context
    assert "21: return self.obj == other" in final_context

    entries = _journal(orchestrator)
    assert [entry.type for entry in entries] == [
        "model_response",
        "draft_finding",
        "length_recovery",
        "tool_result",
        "length_recovery",
        "model_response",
        "length_recovery",
    ]
    assert [
        entry.payload["status"] for entry in entries if entry.type == "length_recovery"
    ] == ["required", "attempted", "succeeded"]

    event_log = tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
    runtime_summary = summarize_event_log(event_log)
    process_metrics = extract_review_process_metrics(event_log)
    eval_stats = _read_event_log_stats(tmp_path, response.run_id)
    assert runtime_summary.model_response_journal_writes == 2
    assert runtime_summary.submit_review_seen is True
    assert runtime_summary.draft_findings_created == 1
    assert runtime_summary.length_recoveries_attempted == 1
    assert runtime_summary.length_recoveries_succeeded == 1
    assert runtime_summary.length_recoveries_failed == 0
    assert process_metrics.model_response_journal_writes == 2
    assert process_metrics.draft_findings_created == 1
    assert process_metrics.length_recoveries_attempted == 1
    assert process_metrics.length_recoveries_succeeded == 1
    assert process_metrics.length_recoveries_failed == 0
    assert eval_stats["submit_review_seen_any"] is True


def test_visible_truncated_content_is_conservatively_promoted_to_minimal_draft(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = _SequenceClient(
        [
            ModelResponse(
                content=(
                    "- `src/wrapper.py:21` - Equality comparison may use the wrong "
                    "peer object instead of the wrapped peer."
                ),
                usage=TokenUsage(total_tokens=4096),
                model="fake-model",
                finish_reason="length",
            ),
            _submit_response("Visible draft was reviewed against retained context."),
        ]
    )

    orchestrator, response = _run_review(tmp_path, client)

    entries = _journal(orchestrator)
    drafts = [entry for entry in entries if entry.type == "draft_finding"]
    assert len(drafts) == 1
    assert drafts[0].payload["file"] == "src/wrapper.py"
    assert drafts[0].payload["line"] == 21
    assert drafts[0].payload["symbol"] is None
    assert set(drafts[0].payload) == {
        "id",
        "source_response_id",
        "file",
        "line",
        "symbol",
        "claim",
    }
    assert "Visible draft was reviewed" in response.report.summary
    draft_events = [
        json.loads(line)
        for line in (tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if '"phase":"draft_finding"' in line
    ]
    assert draft_events[0]["payload"]["origin"] == "visible_content_recovery"


def test_visible_prose_with_path_and_symbol_is_promoted_to_minimal_draft(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = _SequenceClient(
        [
            ModelResponse(
                content=(
                    "I inspected `src/_pytest/fixtures.py`. The key issue: "
                    "SafeHashWrapper.__eq__ compares the wrapped value against the "
                    "other wrapper instead of other.obj, so equal values may fail grouping."
                ),
                usage=TokenUsage(total_tokens=4096),
                model="fake-model",
                finish_reason="length",
            ),
            _submit_response("Visible prose draft was reviewed."),
        ]
    )

    orchestrator, response = _run_review(tmp_path, client)

    draft = next(
        entry for entry in _journal(orchestrator) if entry.type == "draft_finding"
    )
    assert draft.payload["file"] == "src/_pytest/fixtures.py"
    assert draft.payload["line"] is None
    assert draft.payload["symbol"] == "SafeHashWrapper.__eq__"
    assert "compares the wrapped value" in draft.payload["claim"]
    assert "Visible prose draft was reviewed" in response.report.summary


def test_visible_prose_without_repository_path_does_not_invent_draft(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = _SequenceClient(
        [
            ModelResponse(
                content=(
                    "SafeHashWrapper.__eq__ compares the wrong peer object and may "
                    "break grouping, but no repository file path is present."
                ),
                usage=TokenUsage(total_tokens=4096),
                model="fake-model",
                finish_reason="length",
            ),
            _submit_response("No path-bound draft was available."),
        ]
    )

    orchestrator, response = _run_review(tmp_path, client)

    assert not any(entry.type == "draft_finding" for entry in _journal(orchestrator))
    assert "No path-bound draft" in response.report.summary


def test_visible_prose_accepts_root_level_source_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = _SequenceClient(
        [
            ModelResponse(
                content=(
                    "The change is in `main.py`. Cache.lookup returns the wrong cached "
                    "value and may expose stale results after invalidation."
                ),
                usage=TokenUsage(total_tokens=4096),
                model="fake-model",
                finish_reason="length",
            ),
            _submit_response("Root source path draft was reviewed."),
        ]
    )

    orchestrator, response = _run_review(tmp_path, client)

    draft = next(
        entry for entry in _journal(orchestrator) if entry.type == "draft_finding"
    )
    assert draft.payload["file"] == "main.py"
    assert draft.payload["symbol"] == "Cache.lookup"
    assert "Root source path draft" in response.report.summary


def test_visible_prose_binds_nearest_preceding_source_path(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = _SequenceClient(
        [
            ModelResponse(
                content=(
                    "I first checked `src/unrelated.py`, then inspected "
                    "`src/cache.py`. Cache.lookup returns the wrong cached value and "
                    "may expose stale results after invalidation."
                ),
                usage=TokenUsage(total_tokens=4096),
                model="fake-model",
                finish_reason="length",
            ),
            _submit_response("Nearest source path draft was reviewed."),
        ]
    )

    orchestrator, _ = _run_review(tmp_path, client)

    draft = next(
        entry for entry in _journal(orchestrator) if entry.type == "draft_finding"
    )
    assert draft.payload["file"] == "src/cache.py"


def test_length_without_draft_still_recovers_and_never_persists_reasoning(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = _SequenceClient(
        [
            ModelResponse(
                content="",
                reasoning_content=(
                    "Correct bug found privately: equality compares the wrong wrapper."
                ),
                usage=TokenUsage(total_tokens=4096),
                model="fake-model",
                finish_reason="length",
            ),
            _submit_response("Explicit empty review after bounded recovery."),
        ]
    )

    orchestrator, response = _run_review(tmp_path, client)

    assert response.report.summary == "Explicit empty review after bounded recovery."
    assert find_blocking_review_error(response) is None
    entries = _journal(orchestrator)
    assert not any(entry.type == "draft_finding" for entry in entries)
    serialized = "\n".join(entry.model_dump_json() for entry in entries)
    assert "Correct bug found privately" not in serialized
    recovery_events = [
        json.loads(line)
        for line in (tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if '"phase":"length_recovery"' in line
    ]
    required = next(
        event for event in recovery_events if event["payload"]["status"] == "required"
    )
    assert required["payload"]["draft_finding_ids"] == []
    assert "transient_reasoning_present" not in required["payload"]


def test_length_recovery_accepts_explicit_valid_empty_review(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = _SequenceClient(
        [
            ModelResponse(
                usage=TokenUsage(total_tokens=4096),
                model="fake-model",
                finish_reason="length",
            ),
            _submit_response("No supported issues after explicit recovery."),
        ]
    )

    orchestrator, response = _run_review(tmp_path, client)

    assert len(client.messages) == 2
    assert response.report.summary == "No supported issues after explicit recovery."
    assert response.report.issues == []
    assert find_blocking_review_error(response) is None
    statuses = [
        entry.payload["status"]
        for entry in _journal(orchestrator)
        if entry.type == "length_recovery"
    ]
    assert statuses == ["required", "attempted", "succeeded"]


def test_hard_cap_marks_recovery_failed_instead_of_succeeding_empty(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("TOKEN_BUDGET", "10")
    monkeypatch.setenv("TOKEN_HARD_BUDGET", "20")
    monkeypatch.setenv("FINAL_SUBMIT_RESERVE_TOKENS", "5")
    client = _SequenceClient(
        [
            ModelResponse(
                usage=TokenUsage(total_tokens=25),
                model="fake-model",
                finish_reason="length",
            )
        ]
    )

    orchestrator, response = _run_review(tmp_path, client)

    assert len(client.messages) == 1
    assert "placeholder summary" in response.report.summary.lower()
    blocking = find_blocking_review_error(response)
    assert blocking is not None
    assert any("recovery failed" in error.message for error in response.context.errors)
    statuses = [
        entry.payload["status"]
        for entry in _journal(orchestrator)
        if entry.type == "length_recovery"
    ]
    assert statuses == ["required", "attempted", "failed"]
    failed_entry = next(
        entry
        for entry in _journal(orchestrator)
        if entry.type == "length_recovery" and entry.payload["status"] == "failed"
    )
    assert failed_entry.payload["reason"] == "budget_hard_capped"


def test_valid_submit_returned_with_length_needs_no_recovery(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    submitted = _submit_response("Explicit submit survived the provider finish reason.")
    submitted.finish_reason = "length"
    client = _SequenceClient([submitted])

    orchestrator, response = _run_review(tmp_path, client)

    assert len(client.messages) == 1
    assert "survived" in response.report.summary
    assert not any(entry.type == "length_recovery" for entry in _journal(orchestrator))
    assert find_blocking_review_error(response) is None


def test_blank_submit_returned_with_length_requires_explicit_recovery(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    submitted = _submit_response("")
    submitted.finish_reason = "length"
    client = _SequenceClient(
        [submitted, _submit_response("No supported issues after explicit recovery.")]
    )

    orchestrator, response = _run_review(tmp_path, client)

    assert len(client.messages) == 2
    assert response.report.summary == "No supported issues after explicit recovery."
    assert response.report.issues == []
    statuses = [
        entry.payload["status"]
        for entry in _journal(orchestrator)
        if entry.type == "length_recovery"
    ]
    assert statuses == ["required", "attempted", "succeeded"]
    assert find_blocking_review_error(response) is None


def test_blank_recovery_submit_is_failed_not_succeeded_empty(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    client = _SequenceClient(
        [
            ModelResponse(
                usage=TokenUsage(total_tokens=4096),
                model="fake-model",
                finish_reason="length",
            ),
            _submit_response(""),
        ]
    )

    orchestrator, response = _run_review(tmp_path, client)

    assert response.report.summary == ""
    assert response.report.issues == []
    assert find_blocking_review_error(response) is not None
    statuses = [
        entry.payload["status"]
        for entry in _journal(orchestrator)
        if entry.type == "length_recovery"
    ]
    assert statuses == ["required", "attempted", "failed"]
    failed = next(
        entry
        for entry in _journal(orchestrator)
        if entry.type == "length_recovery" and entry.payload["status"] == "failed"
    )
    assert failed.payload["reason"] == (
        "finalize_only_recovery_produced_no_valid_submit_review"
    )
