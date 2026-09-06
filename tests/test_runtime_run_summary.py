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


def test_runtime_summary_collects_review_skill_metrics(tmp_path: Path) -> None:
    log = tmp_path / "skills.jsonl"
    log.write_text(
        json.dumps({
            "run_id": "run-skills",
            "event_type": "phase_end",
            "phase": "review_complete",
            "payload": {
                "review_skill_loaded_count": 2,
                "review_skill_chars": 700,
                "review_skill_tokens": 175,
                "review_skill_retrieval_latency_ms": 1.25,
                "review_skill_fallback_count": 1,
            },
        }),
        encoding="utf-8",
    )
    summary = summarize_event_log(log)
    assert summary.review_skill_loaded_count == 2
    assert summary.review_skill_chars == 700
    assert summary.review_skill_tokens == 175
    assert summary.review_skill_retrieval_latency_ms == 1.25
    assert summary.review_skill_fallback_count == 1


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


def test_runtime_summary_collects_integrity_and_workflow_outcomes(
    tmp_path: Path,
) -> None:
    log = tmp_path / "integrity.jsonl"
    events = [
        {
            "run_id": "run-integrity",
            "event_type": "finding_candidates_built",
            "phase": "verify_findings",
            "payload": {"candidate_count": 3, "mode": "enforce"},
        },
        {
            "run_id": "run-integrity",
            "event_type": "finding_verification_completed",
            "phase": "verify_findings",
            "payload": {
                "accepted_count": 1,
                "rejected_count": 1,
                "deterministic_evidence_checked_count": 2,
                "deterministic_evidence_passed_count": 1,
                "deterministic_evidence_rejected_count": 1,
            },
        },
        {
            "run_id": "run-integrity",
            "event_type": "finding_funnel_completed",
            "phase": "finding_funnel",
            "payload": {
                "submitted_finding_count": 5,
                "no_finding_run_count": 0,
                "non_risk_not_routed_count": 1,
                "pre_verifier_rejected_count": 1,
                "risk_candidate_count": 1,
                "deterministic_rejected_count": 1,
                "final_risk_finding_count": 1,
            },
        },
        {
            "run_id": "run-integrity",
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

    assert summary.finding_candidate_count == 3
    assert summary.finding_accepted_count == 1
    assert summary.finding_rejected_count == 1
    assert summary.deterministic_evidence_checked_count == 2
    assert summary.deterministic_evidence_passed_count == 1
    assert summary.deterministic_evidence_rejected_count == 1
    assert summary.finding_funnel.submitted_finding_count == 5
    assert summary.finding_funnel.non_risk_not_routed_count == 1
    assert summary.finding_funnel.deterministic_rejected_count == 1
    assert summary.finding_funnel.final_risk_finding_count == 1
    assert summary.workflow_enforcement == "enforce"
    assert summary.workflow_required_step_count == 5
    assert summary.workflow_completed_required_step_count == 4
    assert summary.workflow_missing_steps == ["inspect_changed_context"]
    assert summary.workflow_reprompt_count == 1


def test_runtime_summary_collects_graph_path_diversity_metrics(tmp_path: Path) -> None:
    log = tmp_path / "graph.jsonl"
    log.write_text(
        json.dumps(
            {
                "event_type": "context_plan_completed",
                "phase": "context_planner",
                "payload": {
                    "available_graph_path_count": 12,
                    "selected_reviewer_path_count": 7,
                    "dropped_repeated_prefix_path_count": 4,
                    "selected_direct_path_count": 3,
                    "selected_production_path_count": 2,
                    "selected_low_hop_path_count": 1,
                    "required_production_path_count": 2,
                    "missing_production_path_count": 0,
                    "graph_reviewer_context_token_estimate": 321,
                    "path_selection_reason_counts": {
                        "selected_direct": 3,
                        "repeated_first_hop_prefix": 4,
                    },
                },
            },
        )
        + "\n"
        + json.dumps(
            {
                "event_type": "context_telemetry",
                "phase": "analyze",
                "payload": {
                    "graph_reviewer_prompt_projection": {
                        "available_path_count": 9,
                        "selected_path_count": 4,
                        "dropped_path_count": 5,
                        "selected_token_count": 222,
                        "selected_role_coverage": [
                            "execution_flow",
                            "related_test",
                        ],
                    }
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "event_type": "model_call",
                "phase": "provider_attempt",
                "payload": {
                    "success": True,
                    "usage_present": True,
                    "prompt_tokens": 1000,
                    "completion_tokens": 120,
                    "reasoning_tokens": 80,
                    "total_tokens": 1200,
                    "cached_prompt_tokens": 700,
                    "adjacent_common_prefix_tokens": 650,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_event_log(log)

    assert summary.graph_available_path_count == 12
    assert summary.graph_selected_path_count == 7
    assert summary.graph_dropped_repeated_prefix_path_count == 4
    assert summary.graph_selected_direct_path_count == 3
    assert summary.graph_selected_production_path_count == 2
    assert summary.graph_selected_low_hop_path_count == 1
    assert summary.graph_required_production_path_count == 2
    assert summary.graph_missing_production_path_count == 0
    assert summary.graph_reviewer_context_token_estimate == 321
    assert summary.graph_reviewer_available_path_count == 9
    assert summary.graph_reviewer_selected_path_count == 4
    assert summary.graph_reviewer_dropped_path_count == 5
    assert summary.graph_reviewer_selected_token_count == 222
    assert summary.graph_reviewer_role_coverage == ["execution_flow", "related_test"]
    assert summary.provider_attempt_count == 1
    assert summary.successful_prompt_tokens == 1000
    assert summary.successful_total_tokens == 1200
    assert summary.successful_cached_prompt_tokens == 700
    assert summary.successful_adjacent_common_prefix_tokens == 650
    assert summary.cache_observation_count == 1
    assert summary.provider_cache_hit_count == 1
    assert summary.graph_path_selection_reason_counts == {
        "selected_direct": 3,
        "repeated_first_hop_prefix": 4,
    }


def test_runtime_summary_ignores_retired_verifier_fields(tmp_path: Path) -> None:
    log = tmp_path / "historical.jsonl"
    log.write_text(
        json.dumps(
            {
                "run_id": "historical",
                "event_type": "finding_verification_completed",
                "phase": "verify_findings",
                "payload": {
                    "accepted_count": 1,
                    "rejected_count": 1,
                    "raw_accepted_count": 2,
                    "raw_rejected_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )

    summary = summarize_event_log(log)

    assert summary.finding_accepted_count == 1
    assert summary.finding_rejected_count == 1
    assert summary.deterministic_evidence_checked_count == 0
    assert summary.finding_funnel.model_dump() == {
        key: 0 for key in type(summary.finding_funnel).model_fields
    }
