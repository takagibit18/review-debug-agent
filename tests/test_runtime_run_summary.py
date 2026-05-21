"""Tests for runtime run-summary helpers."""

from __future__ import annotations

import json
from pathlib import Path

from src.analyzer.run_summary import (
    RunArtifactSummary,
    summarize_event_log,
    summarize_run_artifacts,
)


def test_runtime_run_summary_collects_publish_status_and_event_metrics(tmp_path: Path) -> None:
    log = tmp_path / "run-1.jsonl"
    events = [
        {
            "run_id": "run-1",
            "event_type": "model_response_detail",
            "phase": "analyze",
            "payload": {
                "model": "model-a",
                "usage": {"total_tokens": 31},
                "tool_call_summaries": [{"name": "read_file"}],
            },
        },
        {
            "run_id": "run-1",
            "event_type": "plan_parsed",
            "phase": "analyze",
            "payload": {
                "submit_review_seen": True,
                "submit_review_validation_error": "Invalid JSON arguments",
            },
        },
        {
            "run_id": "run-1",
            "event_type": "decision",
            "phase": "continue",
            "payload": {"reason": "completed", "budget_state": "none"},
        },
    ]
    log.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    summary = summarize_event_log(log, publish_status="dry_run")

    assert summary.run_id == "run-1"
    assert summary.event_log_status == "ok"
    assert summary.publish_status == "dry_run"
    assert summary.model_names == ["model-a"]
    assert summary.total_tokens == 31
    assert summary.tool_call_count == 1
    assert summary.submit_validation_errors == ["Invalid JSON arguments"]


def test_summarize_run_artifacts_includes_artifact_paths(tmp_path: Path) -> None:
    event_log = tmp_path / "run-2.jsonl"
    response_json = tmp_path / "response.json"
    publish_json = tmp_path / "publish.json"

    artifact_summary = summarize_run_artifacts(
        run_id="run-2",
        event_log_path=event_log,
        response_json_path=response_json,
        publish_result_json_path=publish_json,
        publish_status="published",
    )

    assert isinstance(artifact_summary, RunArtifactSummary)
    assert artifact_summary.run_id == "run-2"
    assert artifact_summary.publish_status == "published"
    assert artifact_summary.artifact_paths["response_json"] == str(response_json)
    assert artifact_summary.artifact_paths["publish_result_json"] == str(publish_json)
