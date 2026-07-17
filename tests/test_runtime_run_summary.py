"""Tests for runtime run-summary helpers."""

from __future__ import annotations

import json
from pathlib import Path

from src.analyzer.run_summary import (
    RunArtifactSummary,
    summarize_event_log,
    summarize_run_artifacts,
)


def test_runtime_run_summary_collects_publish_status_and_event_metrics(
    tmp_path: Path,
) -> None:
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


def test_runtime_summary_collects_verifier_and_workflow_outcomes(
    tmp_path: Path,
) -> None:
    log = tmp_path / "v020.jsonl"
    events = [
        {
            "run_id": "run-v020",
            "event_type": "finding_candidates_built",
            "phase": "verify_findings",
            "payload": {"candidate_count": 3, "mode": "enforce"},
        },
        {
            "run_id": "run-v020",
            "event_type": "finding_verification_completed",
            "phase": "verify_findings",
            "payload": {
                "accepted_count": 1,
                "rejected_count": 1,
                "needs_evidence_count": 0,
                "downgraded_count": 1,
                "reason_codes": [
                    "verified",
                    "claim_not_supported",
                    "severity_overstated",
                ],
                "raw_accepted_count": 2,
                "raw_rejected_count": 0,
                "raw_needs_evidence_count": 0,
                "raw_downgraded_count": 1,
                "raw_reason_codes": ["verified", "verified", "severity_overstated"],
                "deterministic_evidence_checked_count": 2,
                "deterministic_evidence_passed_count": 1,
                "deterministic_evidence_rejected_count": 1,
            },
        },
        {
            "run_id": "run-v020",
            "event_type": "workflow_summary",
            "phase": "workflow",
            "payload": {
                "required_step_count": 5,
                "completed_required_step_count": 4,
                "missing_required_steps": ["inspect_changed_context"],
                "reprompt_count": 1,
                "enforcement": "enforce",
            },
        },
    ]
    log.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")

    summary = summarize_event_log(log)

    assert summary.finding_verifier_mode == "enforce"
    assert summary.finding_candidate_count == 3
    assert summary.finding_accepted_count == 1
    assert summary.finding_rejected_count == 1
    assert summary.finding_downgraded_count == 1
    assert summary.finding_reason_codes["claim_not_supported"] == 1
    assert summary.raw_verifier_accepted_count == 2
    assert summary.raw_verifier_rejected_count == 0
    assert summary.raw_finding_reason_codes["verified"] == 2
    assert summary.deterministic_evidence_checked_count == 2
    assert summary.deterministic_evidence_passed_count == 1
    assert summary.deterministic_evidence_rejected_count == 1
    assert summary.workflow_enforcement == "enforce"
    assert summary.workflow_required_step_count == 5
    assert summary.workflow_completed_required_step_count == 4
    assert summary.workflow_missing_steps == ["inspect_changed_context"]
    assert summary.workflow_reprompt_count == 1


def test_runtime_summary_backfills_raw_counts_for_legacy_events(tmp_path: Path) -> None:
    log = tmp_path / "legacy.jsonl"
    log.write_text(
        json.dumps(
            {
                "run_id": "legacy",
                "event_type": "finding_verification_completed",
                "phase": "verify_findings",
                "payload": {
                    "accepted_count": 1,
                    "rejected_count": 1,
                    "needs_evidence_count": 0,
                    "downgraded_count": 0,
                    "reason_codes": ["verified", "claim_not_supported"],
                },
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_event_log(log)

    assert summary.raw_verifier_accepted_count == 1
    assert summary.raw_verifier_rejected_count == 1
    assert summary.raw_finding_reason_codes == {
        "verified": 1,
        "claim_not_supported": 1,
    }
    assert summary.deterministic_evidence_checked_count == 0
