"""Schema/config migration and v0.2.2 compatibility checks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.analyzer.finding_schema import RepairIntent, SourceAnchor
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.config import Settings
from src.orchestrator.tool_schemas import build_submit_tool_schemas


def test_legacy_review_issue_parses_and_round_trips_v022_payload() -> None:
    legacy = {
        "severity": "warning",
        "location": "service.py:12",
        "evidence": "changed lookup",
        "suggestion": "guard the lookup",
        "confidence": 0.9,
    }

    issue = ReviewIssue.model_validate(legacy)
    report = ReviewReport(summary="legacy", issues=[issue])

    assert issue.schema_version == "1.0"
    assert issue.primary_anchor is None
    assert report.schema_version == "2.0"
    assert report.v022_payload() == {
        "summary": "legacy",
        "issues": [{**legacy, "candidate_id": ""}],
    }


def test_structured_issue_has_versioned_fields_but_legacy_projection_is_stable() -> (
    None
):
    issue = ReviewIssue(
        severity=Severity.WARNING,
        location="service.py:12",
        evidence="changed lookup",
        suggestion="change cache identity",
        confidence=0.9,
        schema_version="2.0",
        finding_id="F-01",
        root_cause_id="RC-01",
        primary_anchor=SourceAnchor(file="service.py", line=12, symbol_id="symbol"),
        causal_mechanism="Cache key omits model identity",
        violated_invariant="Cache identity must match requested model configuration",
        repair_intent=RepairIntent(
            action="Include model in cache identity",
            targets=["cache_key"],
            boundary="cache lifecycle",
        ),
    )

    payload = issue.v022_payload()

    assert "root_cause_id" not in payload
    assert "primary_anchor" not in payload
    assert payload["location"] == "service.py:12"


def test_new_config_defaults_are_enabled_and_invalid_ranges_are_rejected() -> None:
    settings = Settings()

    assert settings.root_cause_consolidation_enabled is True
    assert settings.relation_graph_enabled is True
    assert settings.relation_graph_persistence_enabled is True
    assert settings.relation_graph_resolver_mode in {"ast", "resolver", "lsp"}

    with pytest.raises(ValidationError):
        Settings(root_cause_consolidation_max_block_size=1)
    with pytest.raises(ValidationError):
        Settings(relation_graph_max_depth=7)
    with pytest.raises(ValidationError):
        Settings(relation_graph_min_evidence_confidence=1.1)
    with pytest.raises(ValidationError):
        Settings(relation_graph_index_path="")


def test_reviewer_submit_schema_requires_hypothesis_fields_and_forbids_root_id() -> (
    None
):
    review_tool = next(
        item
        for item in build_submit_tool_schemas()
        if item["function"]["name"] == "submit_review"
    )
    item_schema = review_tool["function"]["parameters"]["properties"]["issues"]["items"]
    required = set(item_schema["required"])

    assert {
        "primary_anchor",
        "observed_behavior",
        "causal_mechanism",
        "violated_invariant",
        "repair_intent",
        "cause_evidence",
        "contract_evidence",
        "trigger_evidence",
        "impact_evidence",
        "context_manifest_id",
    }.issubset(required)
    assert "root_cause_id" not in item_schema["properties"]
