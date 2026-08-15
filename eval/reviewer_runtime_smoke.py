"""Single-fixture Reviewer/runtime diagnostic smoke for Graph A/B readiness."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from eval import core_eval
from eval.core_eval import CoreFixtureSpec, CoreRuntimeConfig
from eval.graph_ab_pilot import (
    clear_index,
    run_single_lifecycle,
    validate_variant_contract,
)
from eval.schemas import EvalResult, EvalVariant, Fixture
from src.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORE_CONFIG = ROOT / "eval" / "core_eval_v1.yaml"
DEFAULT_FIXTURE_ID = "golden_pytest-dev_pytest_pr9350"
DEFAULT_OUTPUT_JSON = (
    ROOT / "eval" / "outputs" / "reviewer-runtime-smoke-pytest9350.json"
)
DEFAULT_OUTPUT_MARKDOWN = (
    ROOT / "eval" / "reports" / "reviewer-runtime-smoke-pytest9350.md"
)
DEFAULT_ARTIFACT_DIR = (
    ROOT / "eval" / "outputs" / "reviewer-runtime-smoke-pytest9350-artifacts"
)
MODEL_NAME = "deepseek-v4-pro"

FailureStage = Literal[
    "workspace",
    "context_retrieval",
    "provider_request",
    "reviewer_discovery",
    "draft_persistence",
    "structured_submit",
    "length_recovery",
    "pre_verifier_policy",
    "semantic_verifier",
    "deterministic_validation",
    "matcher",
    "none",
]
DiscoveryStatus = Literal["YES", "PARTIAL", "NO"]


class StageDiagnostic(BaseModel):
    """Auditable stage-by-stage interpretation of one measured attempt."""

    variant_id: str
    skipped: bool = False
    skip_reason: str = ""
    run_id: str = ""
    workspace_valid: str = "FAIL"
    fixture_validation_passed: bool = False
    runtime_valid_completion: str = "FAIL"
    model_provider_call_errors: list[str] = Field(default_factory=list)
    runtime_errors: list[str] = Field(default_factory=list)
    schema_valid: bool = False
    placeholder_summary: bool = False
    workflow_invalid: bool = False
    finish_reasons: list[str] = Field(default_factory=list)
    budget_state: str = "none"
    budget_exhausted: bool = False
    valid_completion: bool = False
    gold_file_reached: bool = False
    gold_symbol_reached: bool = False
    context_paths: list[str] = Field(default_factory=list)
    graph_status: str = ""
    graph_cache_mode: str = ""
    cache_hit: bool | None = None
    manifest_count: int = 0
    graph_fallback_reason: str = ""
    graph_manifest_contains_gold: bool | None = None
    graph_guided_to_gold: bool | None = None
    variant_contract_valid: bool = False
    variant_contract_errors: list[str] = Field(default_factory=list)
    reviewer_discovered_gold: DiscoveryStatus = "NO"
    discovery_evidence: list[str] = Field(default_factory=list)
    record_draft_finding_called: bool = False
    draft_finding_count: int = 0
    draft_persisted: bool = False
    draft_correct_file_symbol: bool = False
    draft_correct_semantics: bool = False
    submit_review: str = "NO"
    submit_summary_nonempty: bool = False
    submitted_issue_count: int = 0
    blank_submit: bool = False
    submit_schema_invalid: bool = False
    submitted_gold_issue: bool = False
    length_recovery: str = "NOT_REQUIRED"
    recovery_required: bool = False
    recovery_attempted: bool = False
    recovery_evidence_preserved: bool | None = None
    recovery_submit_preserved_gold: bool | None = None
    raw_submitted_finding_count: int = 0
    pre_verifier: str = "N/A"
    pre_verifier_reasons: list[str] = Field(default_factory=list)
    semantic_verifier: str = "N/A"
    semantic_verifier_reasons: list[str] = Field(default_factory=list)
    deterministic_validation: str = "N/A"
    deterministic_reasons: list[str] = Field(default_factory=list)
    final_finding_survived: bool = False
    final_risk_finding_count: int = 0
    matcher_attempted: bool = False
    gold_match: str = "NOT_REACHED"
    failure_stage: FailureStage = "none"
    failure_evidence: str = ""
    final_diagnosis: str = ""
    event_log_path: str = ""
    run_journal_path: str = ""
    run_journal_status: str = ""


class ReviewerRuntimeSmokeReport(BaseModel):
    """Machine-readable companion to the Markdown diagnostic matrix."""

    experiment_id: str = "reviewer-runtime-smoke-pytest9350"
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    fixture_id: str
    fixture_path: str
    gold_description: str
    runtime_contract: dict[str, Any]
    measured_attempt_limit: int = 1
    run_order: list[str] = Field(
        default_factory=lambda: ["A-agent-search", "B1-graph-hybrid-cold"]
    )
    diagnostics: list[StageDiagnostic]
    results: dict[str, EvalResult] = Field(default_factory=dict)
    lifecycle: dict[str, dict[str, Any]] = Field(default_factory=dict)
    ready_for_formal_graph_ab: str
    conclusion_reason: str
    next_step: str


def _load_jsonl(path_value: str | Path | None) -> list[dict[str, Any]]:
    if not path_value:
        return []
    path = Path(path_value)
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _tool_call(call: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(call, dict):
        return "", {}
    function = call.get("function")
    if not isinstance(function, dict):
        return "", {}
    name = str(function.get("name", "")).strip()
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"raw": arguments}
    return name, arguments if isinstance(arguments, dict) else {}


def _issue_text(issue: Any) -> str:
    if not isinstance(issue, dict):
        return ""
    return " ".join(
        str(value)
        for key, value in issue.items()
        if key
        in {
            "location",
            "evidence",
            "suggestion",
            "observed_behavior",
            "causal_mechanism",
            "violated_invariant",
            "trigger",
            "impact",
        }
        and value
    )


def _semantic_tokens(value: str) -> set[str]:
    """Use the Core matcher vocabulary so diagnostics grade the same wording."""

    return core_eval._semantic_tokens(value)  # noqa: SLF001


def _semantic_grade(text: str, spec: CoreFixtureSpec) -> DiscoveryStatus:
    if not spec.gold_findings or not text.strip():
        return "NO"
    gold = spec.gold_findings[0]
    actual = _semantic_tokens(text)
    root = _semantic_tokens(gold.root_cause)
    description = _semantic_tokens(gold.description)
    root_overlap = len(actual & root)
    root_coverage = root_overlap / len(root) if root else 0.0
    impact_only = description - root
    impact_overlap = len(actual & impact_only)
    identifiers = {
        item.lower()
        for item in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", gold.root_cause)
        if item.startswith("__") or any(char.isupper() for char in item)
    }
    identifier_hit = bool(actual & identifiers)
    if root_overlap >= 4 and root_coverage >= 0.35 and impact_overlap >= 1:
        return "YES"
    if root_overlap >= 2 or identifier_hit:
        return "PARTIAL"
    return "NO"


def _best_grade(texts: list[str], spec: CoreFixtureSpec) -> DiscoveryStatus:
    grades = [_semantic_grade(text, spec) for text in texts]
    if "YES" in grades:
        return "YES"
    if "PARTIAL" in grades:
        return "PARTIAL"
    return "NO"


def _gold_file_and_symbols(spec: CoreFixtureSpec) -> tuple[str, list[str]]:
    gold = spec.gold_findings[0]
    dotted_identifiers = re.findall(
        r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)+[A-Za-z_][A-Za-z0-9_]*",
        gold.root_cause,
    )
    symbols = list(
        dict.fromkeys(
            item
            for identifier in dotted_identifiers
            for item in identifier.split(".")
            if item.startswith("__") or any(char.isupper() for char in item)
        )
    )
    return gold.file.replace("\\", "/"), symbols


def _payloads(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [
        event.get("payload", {})
        for event in events
        if event.get("event_type") == event_type
        and isinstance(event.get("payload"), dict)
    ]


def _extract_submit_calls(
    journal: list[dict[str, Any]],
) -> list[tuple[int, str, dict[str, Any]]]:
    calls: list[tuple[int, str, dict[str, Any]]] = []
    for position, entry in enumerate(journal):
        if entry.get("type") != "model_response":
            continue
        payload = entry.get("payload", {})
        for call in payload.get("tool_calls", []) if isinstance(payload, dict) else []:
            name, arguments = _tool_call(call)
            if name == "submit_review":
                calls.append((position, str(entry.get("id", "")), arguments))
    return calls


def _raw_final_issues(result: EvalResult) -> list[dict[str, Any]]:
    report = result.raw_output.get("report", {})
    issues = report.get("issues", []) if isinstance(report, dict) else []
    return (
        [item for item in issues if isinstance(item, dict)]
        if isinstance(issues, list)
        else []
    )


def diagnose_attempt(
    *,
    spec: CoreFixtureSpec,
    fixture: Fixture,
    variant: EvalVariant,
    result: EvalResult,
    lifecycle: dict[str, Any],
) -> StageDiagnostic:
    """Map one measured result plus EventLog/Run Journal onto diagnostic stages."""

    events = _load_jsonl(result.event_log_path)
    journal_path = str(lifecycle.get("run_journal_path", ""))
    journal = _load_jsonl(journal_path)
    contract = validate_variant_contract(variant, result, lifecycle)
    gold_file, symbols = _gold_file_and_symbols(spec)
    serialized_context = json.dumps(
        [
            event
            for event in events
            if event.get("event_type")
            in {"tool_io", "context_manifest_created", "changed_anchors_extracted"}
        ],
        ensure_ascii=False,
    )
    context_paths = ["review diff prompt (full PR diff contains the gold hunk)"]
    for payload in _payloads(events, "tool_io"):
        text = json.dumps(payload, ensure_ascii=False).replace("\\", "/")
        while "//" in text:
            text = text.replace("//", "/")
        if gold_file.lower() in text.lower():
            preview = payload.get("result_preview", {}).get("preview", {})
            data = preview.get("data", {}) if isinstance(preview, dict) else {}
            span = (
                f" lines {data.get('start_line')}-"
                f"{int(data.get('start_line', 0) or 0) + int(data.get('line_count', 0) or 0) - 1}"
                if isinstance(data, dict) and data.get("start_line")
                else ""
            )
            context_paths.append(f"{payload.get('name', 'tool')} EventLog result{span}")
    manifest_payloads = _payloads(events, "context_manifest_created")
    gold = spec.gold_findings[0]
    manifest_contains_gold = any(
        str(span.get("file", "")).replace("\\", "/") == gold_file
        and int(span.get("start_line", 0) or 0) <= gold.location.end_line
        and int(span.get("end_line", 0) or 0) >= gold.location.start_line
        for payload in manifest_payloads
        for span in payload.get("included_spans", [])
        if isinstance(span, dict)
    )
    if manifest_contains_gold:
        context_paths.append("Candidate Context Manifest gold span")
    input_contains_gold = gold_file in (fixture.input.diff_text or "")
    input_contains_symbols = all(
        symbol.lower() in (fixture.input.diff_text or "").lower() for symbol in symbols
    )
    review_call_attempted = bool(
        _payloads(events, "model_call")
        or [
            event
            for event in events
            if event.get("event_type") == "error" and event.get("phase") == "analyze"
        ]
        or journal
    )
    gold_file_reached = input_contains_gold and review_call_attempted
    gold_symbol_reached = input_contains_symbols and gold_file_reached
    if gold_file.lower() in serialized_context.lower():
        gold_file_reached = True
    if symbols and all(
        symbol.lower() in serialized_context.lower() for symbol in symbols
    ):
        gold_symbol_reached = True

    submits = _extract_submit_calls(journal)
    first_submit_position = submits[0][0] if submits else len(journal)
    discovery_texts: list[str] = []
    discovery_evidence: list[str] = []
    record_called = False
    draft_entries: list[dict[str, Any]] = []
    for position, entry in enumerate(journal):
        payload = entry.get("payload", {})
        if entry.get("type") == "draft_finding":
            draft_entries.append(payload)
            if position < first_submit_position:
                discovery_texts.append(str(payload.get("claim", "")))
        if entry.get("type") != "model_response" or position >= first_submit_position:
            continue
        content = str(payload.get("content", ""))
        if content.strip():
            discovery_texts.append(content)
        for call in payload.get("tool_calls", []) if isinstance(payload, dict) else []:
            name, arguments = _tool_call(call)
            if name == "record_draft_finding":
                record_called = True
                discovery_texts.append(json.dumps(arguments, ensure_ascii=False))
    discovery = _best_grade(discovery_texts, spec)
    for text in discovery_texts:
        grade = _semantic_grade(text, spec)
        if grade != "NO":
            excerpt = " ".join(text.split())[:240]
            discovery_evidence.append(f"{grade}: {excerpt}")
    draft_texts = [json.dumps(item, ensure_ascii=False) for item in draft_entries]
    draft_correct_semantics = _best_grade(draft_texts, spec) == "YES"
    draft_correct_file_symbol = any(
        str(item.get("file", "")).replace("\\", "/") == gold_file
        and any(
            symbol.lower() in str(item.get("symbol", "")).lower()
            or symbol.lower() in str(item.get("claim", "")).lower()
            for symbol in symbols
        )
        for item in draft_entries
    )

    last_submit = submits[-1][2] if submits else {}
    summary = str(last_submit.get("summary", ""))
    submitted_issues_raw = last_submit.get("issues", [])
    submitted_issues = (
        [item for item in submitted_issues_raw if isinstance(item, dict)]
        if isinstance(submitted_issues_raw, list)
        else []
    )
    submitted_grades = [
        _semantic_grade(_issue_text(item), spec) for item in submitted_issues
    ]
    submitted_gold_indices = {
        index for index, grade in enumerate(submitted_grades) if grade == "YES"
    }
    plan_payloads = _payloads(events, "plan_parsed")
    submit_validation_errors = [
        str(payload.get("submit_review_validation_error", ""))
        for payload in plan_payloads
        if str(payload.get("submit_review_validation_error", "")).strip()
    ]
    finish_reasons = list(result.finish_reasons)
    length_required = "length" in finish_reasons or any(
        entry.get("type") == "length_recovery"
        and entry.get("payload", {}).get("status") == "required"
        for entry in journal
    )
    metrics = result.process_metrics
    if not length_required:
        recovery = "NOT_REQUIRED"
    elif metrics.length_recoveries_succeeded:
        recovery = "SUCCESS"
    else:
        recovery = "FAIL"
    recovery_entries = [
        entry.get("payload", {})
        for entry in journal
        if entry.get("type") == "length_recovery"
    ]
    preserved_ids = {
        str(item)
        for payload in recovery_entries
        for item in payload.get("draft_finding_ids", [])
    }
    recovery_evidence_preserved = (
        bool(preserved_ids)
        or any(_semantic_grade(text, spec) == "YES" for text in discovery_texts)
        if length_required
        else None
    )

    filter_payloads = _payloads(events, "finding_filter_decision")
    gold_filter = next(
        (
            payload
            for payload in filter_payloads
            if int(payload.get("original_index", -1)) in submitted_gold_indices
        ),
        None,
    )
    candidate_payload = (_payloads(events, "finding_candidates_built") or [{}])[-1]
    gold_candidates = [
        item
        for item in candidate_payload.get("candidates", [])
        if isinstance(item, dict)
        and int(item.get("source_issue_index", -1)) in submitted_gold_indices
    ]
    gold_candidate_ids = {str(item.get("candidate_id", "")) for item in gold_candidates}
    verification_payload = (
        _payloads(events, "finding_verification_completed") or [{}]
    )[-1]
    raw_verdicts = [
        item
        for item in verification_payload.get("raw_verdicts", [])
        if isinstance(item, dict)
        and str(item.get("candidate_id", "")) in gold_candidate_ids
    ]
    verdicts = [
        item
        for item in verification_payload.get("verdicts", [])
        if isinstance(item, dict)
        and str(item.get("candidate_id", "")) in gold_candidate_ids
    ]
    deterministic_details = [
        item
        for item in verification_payload.get("deterministic_rejection_details", [])
        if isinstance(item, dict)
        and str(item.get("candidate_id", "")) in gold_candidate_ids
    ]

    if not submitted_gold_indices:
        pre_verifier = "N/A"
        pre_reasons: list[str] = []
    elif gold_candidates:
        pre_verifier = "PASS"
        pre_reasons = list(gold_filter.get("reason_codes", [])) if gold_filter else []
    else:
        pre_verifier = "REJECT"
        pre_reasons = (
            list(gold_filter.get("reason_codes", []))
            if gold_filter
            else ["candidate_not_built"]
        )
    if not gold_candidates:
        semantic = "N/A"
        semantic_reasons: list[str] = []
    elif raw_verdicts and all(
        item.get("status") == "accepted" for item in raw_verdicts
    ):
        semantic = "ACCEPT"
        semantic_reasons = [
            code for item in raw_verdicts for code in item.get("reason_codes", [])
        ]
    else:
        semantic = "REJECT"
        semantic_reasons = [
            code for item in raw_verdicts for code in item.get("reason_codes", [])
        ] or ["verdict_missing"]
    if semantic != "ACCEPT":
        deterministic = "N/A"
        deterministic_reasons: list[str] = []
    elif verdicts and all(item.get("status") == "accepted" for item in verdicts):
        deterministic = "PASS"
        deterministic_reasons = []
    else:
        deterministic = "REJECT"
        deterministic_reasons = [
            str(item.get("rule", "deterministic_validation_failed"))
            for item in deterministic_details
        ] or [code for item in verdicts for code in item.get("reason_codes", [])]

    final_issues = _raw_final_issues(result)
    final_survived = any(
        _semantic_grade(_issue_text(item), spec) == "YES" for item in final_issues
    )
    matcher_attempted = final_survived
    gold_match = (
        "HIT"
        if matcher_attempted and result.matched_count > 0
        else "MISS"
        if matcher_attempted
        else "NOT_REACHED"
    )
    error_events = _payloads(events, "error")
    provider_errors = [
        str(payload.get("message", ""))
        for payload in error_events
        if "model" in str(payload.get("error_type", "")).lower()
        or "provider" in str(payload.get("message", "")).lower()
    ]
    runtime_errors = [str(payload.get("message", "")) for payload in error_events]
    if result.error:
        runtime_errors.append(result.error)

    diagnostic = StageDiagnostic(
        variant_id=variant.id,
        run_id=result.run_id,
        workspace_valid="PASS" if lifecycle.get("workspace_prepared") else "FAIL",
        fixture_validation_passed=bool(lifecycle.get("fixture_validation_passed")),
        runtime_valid_completion=(
            "PASS"
            if result.schema_valid
            and result.run_id
            and result.submit_review_seen_any
            and not result.placeholder_summary
            and not result.workflow_invalid
            and not result.error
            else "FAIL"
        ),
        model_provider_call_errors=list(dict.fromkeys(provider_errors)),
        runtime_errors=list(dict.fromkeys(item for item in runtime_errors if item)),
        schema_valid=result.schema_valid,
        placeholder_summary=result.placeholder_summary,
        workflow_invalid=result.workflow_invalid,
        finish_reasons=finish_reasons,
        budget_state=result.budget_state,
        budget_exhausted=result.budget_exhausted,
        gold_file_reached=gold_file_reached,
        gold_symbol_reached=gold_symbol_reached,
        context_paths=list(dict.fromkeys(context_paths)),
        graph_status=metrics.graph_status,
        graph_cache_mode=metrics.graph_cache_mode,
        cache_hit=metrics.graph_cache_hit,
        manifest_count=metrics.manifest_count,
        graph_fallback_reason=metrics.graph_fallback_reason,
        graph_manifest_contains_gold=(
            manifest_contains_gold if variant.context_mode == "graph_hybrid" else None
        ),
        graph_guided_to_gold=(
            manifest_contains_gold and gold_symbol_reached
            if variant.context_mode == "graph_hybrid"
            else None
        ),
        variant_contract_valid=contract.valid,
        variant_contract_errors=contract.errors,
        reviewer_discovered_gold=discovery,
        discovery_evidence=discovery_evidence,
        record_draft_finding_called=record_called,
        draft_finding_count=len(draft_entries),
        draft_persisted=bool(draft_entries),
        draft_correct_file_symbol=draft_correct_file_symbol,
        draft_correct_semantics=draft_correct_semantics,
        submit_review=(
            "RECOVERY" if submits and length_required else "NORMAL" if submits else "NO"
        ),
        submit_summary_nonempty=bool(summary.strip()),
        submitted_issue_count=len(submitted_issues),
        blank_submit=bool(submits and not summary.strip() and not submitted_issues),
        submit_schema_invalid=bool(submit_validation_errors),
        submitted_gold_issue=bool(submitted_gold_indices),
        length_recovery=recovery,
        recovery_required=length_required,
        recovery_attempted=bool(metrics.length_recoveries_attempted),
        recovery_evidence_preserved=recovery_evidence_preserved,
        recovery_submit_preserved_gold=(
            bool(submitted_gold_indices) if length_required else None
        ),
        raw_submitted_finding_count=metrics.finding_funnel.submitted_finding_count,
        pre_verifier=pre_verifier,
        pre_verifier_reasons=pre_reasons,
        semantic_verifier=semantic,
        semantic_verifier_reasons=semantic_reasons,
        deterministic_validation=deterministic,
        deterministic_reasons=deterministic_reasons,
        final_finding_survived=final_survived,
        final_risk_finding_count=metrics.finding_funnel.final_risk_finding_count,
        matcher_attempted=matcher_attempted,
        gold_match=gold_match,
        event_log_path=str(result.event_log_path or ""),
        run_journal_path=journal_path,
        run_journal_status=str(
            lifecycle.get(
                "run_journal_status",
                "persisted" if journal_path else "missing_no_entries",
            )
        ),
    )
    diagnostic.valid_completion = diagnostic.runtime_valid_completion == "PASS"
    _attribute_failure(diagnostic)
    return diagnostic


def _attribute_failure(item: StageDiagnostic) -> None:
    if item.workspace_valid == "FAIL" or not item.fixture_validation_passed:
        item.failure_stage = "workspace"
        item.failure_evidence = (
            "; ".join(item.runtime_errors) or "workspace/fixture validation failed"
        )
    elif item.model_provider_call_errors:
        item.failure_stage = "provider_request"
        item.failure_evidence = (
            "Provider request failed before any model response or submit_review: "
            + "; ".join(item.model_provider_call_errors)
        )
    elif item.runtime_valid_completion == "FAIL" and item.submit_review == "NO":
        item.failure_stage = "structured_submit"
        item.failure_evidence = (
            "Provider/runtime failed before a model response or submit_review: "
            + ("; ".join(item.runtime_errors) or "no valid submit_review")
        )
    elif not item.gold_file_reached or not item.gold_symbol_reached:
        item.failure_stage = "context_retrieval"
        item.failure_evidence = (
            "gold file or symbol was not present in auditable reviewer context"
        )
    elif item.reviewer_discovered_gold != "YES" and not item.submitted_gold_issue:
        item.failure_stage = "reviewer_discovery"
        item.failure_evidence = "gold area was reached, but no visible response or draft expressed the full bug semantics"
    elif item.record_draft_finding_called and item.draft_finding_count == 0:
        item.failure_stage = "draft_persistence"
        item.failure_evidence = (
            "record_draft_finding was called but no DraftFinding persisted"
        )
    elif item.reviewer_discovered_gold == "YES" and not item.submitted_gold_issue:
        item.failure_stage = "structured_submit"
        item.failure_evidence = (
            "visible discovery was not preserved in the structured submission"
        )
    elif item.length_recovery == "FAIL":
        item.failure_stage = "length_recovery"
        item.failure_evidence = (
            "finish_reason=length required recovery, but recovery did not succeed"
        )
    elif item.submitted_gold_issue and item.pre_verifier == "REJECT":
        item.failure_stage = "pre_verifier_policy"
        item.failure_evidence = (
            ", ".join(item.pre_verifier_reasons)
            or "correct submitted issue was not routed"
        )
    elif item.submitted_gold_issue and item.semantic_verifier == "REJECT":
        item.failure_stage = "semantic_verifier"
        item.failure_evidence = (
            ", ".join(item.semantic_verifier_reasons)
            or "semantic verifier rejected the gold issue"
        )
    elif item.submitted_gold_issue and item.deterministic_validation == "REJECT":
        item.failure_stage = "deterministic_validation"
        item.failure_evidence = (
            ", ".join(item.deterministic_reasons)
            or "deterministic validation rejected the gold issue"
        )
    elif item.final_finding_survived and item.gold_match != "HIT":
        item.failure_stage = "matcher"
        item.failure_evidence = "gold-semantic final finding survived, but the existing matcher did not match it"
    else:
        item.failure_stage = "none"
        item.failure_evidence = "no pipeline failure observed"
    item.final_diagnosis = (
        "complete chain; gold matched"
        if item.gold_match == "HIT"
        else f"{item.failure_stage}: {item.failure_evidence}"
    )


def _shared_runtime_blocker(item: StageDiagnostic) -> bool:
    return bool(
        item.workspace_valid == "FAIL"
        or not item.fixture_validation_passed
        or item.model_provider_call_errors
        or item.submit_schema_invalid
        or (
            item.runtime_valid_completion == "FAIL"
            and item.failure_stage in {"structured_submit", "length_recovery"}
        )
    )


def _conclusion(items: list[StageDiagnostic]) -> tuple[str, str, str]:
    measured = [item for item in items if not item.skipped]
    if len(measured) != 2:
        provider_blocked = any(item.model_provider_call_errors for item in measured)
        return (
            "NO-GO: reviewer/runtime blocker",
            "B was skipped after a shared runtime blocker in A.",
            (
                "Restore deepseek-v4-pro provider responsiveness under the existing 90s Core request timeout, then rerun this smoke; do not tune reviewer, verifier, matcher, or token budgets before a model response is observed."
                if provider_blocked
                else "Fix only the shared runtime failure, then rerun this two-attempt smoke."
            ),
        )
    evidence_failures = {
        "pre_verifier_policy",
        "semantic_verifier",
        "deterministic_validation",
    }
    blocked_evidence = [
        item for item in measured if item.failure_stage in evidence_failures
    ]
    if blocked_evidence:
        gates = ", ".join(
            f"{item.variant_id}:{item.failure_stage}" for item in blocked_evidence
        )
        return (
            "NO-GO: evidence pipeline blocker",
            f"Correct submitted gold finding was lost at {gates}.",
            "Inspect only the named gate and rejection reason before rerunning the smoke.",
        )
    runtime_bad = [
        item
        for item in measured
        if not item.valid_completion
        or not item.variant_contract_valid
        or item.submit_schema_invalid
        or item.placeholder_summary
        or item.length_recovery == "FAIL"
    ]
    discoveries = [
        item.reviewer_discovered_gold == "YES" or item.submitted_gold_issue
        for item in measured
    ]
    if runtime_bad or not any(discoveries):
        reason = (
            "; ".join(
                f"{item.variant_id}: {item.final_diagnosis}" for item in runtime_bad
            )
            if runtime_bad
            else "Neither reviewer produced auditable full gold-bug semantics on the known positive fixture."
        )
        provider_blocked = any(item.model_provider_call_errors for item in runtime_bad)
        return (
            "NO-GO: reviewer/runtime blocker",
            reason,
            (
                "Restore deepseek-v4-pro provider responsiveness under the existing 90s Core request timeout, then rerun this smoke; do not tune reviewer, verifier, matcher, or token budgets before a model response is observed."
                if provider_blocked
                else "Make the smallest reviewer or submit/recovery fix identified by the failure stage, then rerun this smoke."
            ),
        )
    return (
        "GO",
        "Both runtime contracts completed validly and graph_hybrid cold ran without fallback; this single fixture is diagnostic only.",
        "Runtime/reviewer smoke passed; next step is formal Graph A/B. Do not infer a recall improvement from this smoke.",
    )


def render_markdown(report: ReviewerRuntimeSmokeReport) -> str:
    """Render concise, auditable stage and failure-attribution matrices."""

    by_id = {item.variant_id: item for item in report.diagnostics}
    a = by_id["A-agent-search"]
    b = by_id["B1-graph-hybrid-cold"]

    def cell(item: StageDiagnostic, field: str) -> str:
        if item.skipped:
            return "SKIPPED"
        value = getattr(item, field)
        if isinstance(value, bool):
            return "YES" if value else "NO"
        if value is None:
            return "N/A"
        return str(value)

    rows = [
        ("Workspace valid", "workspace_valid"),
        ("Fixture validation", "fixture_validation_passed"),
        ("Runtime valid completion", "runtime_valid_completion"),
        ("Gold file reached", "gold_file_reached"),
        ("Gold symbol reached", "gold_symbol_reached"),
        ("Reviewer discovered gold", "reviewer_discovered_gold"),
        ("Draft persisted", "draft_persisted"),
        ("submit_review", "submit_review"),
        ("Length recovery", "length_recovery"),
        ("Pre-verifier", "pre_verifier"),
        ("Semantic verifier", "semantic_verifier"),
        ("Deterministic validation", "deterministic_validation"),
        ("Final finding survived", "final_finding_survived"),
        ("Gold match", "gold_match"),
        ("Graph manifest valid", "graph_manifest_contains_gold"),
        ("Final diagnosis", "final_diagnosis"),
    ]
    lines = [
        "# Reviewer / Runtime Diagnostic Smoke — pytest PR 9350",
        "",
        "> Diagnostic smoke only. This is not a Graph recall or quality comparison.",
        "",
        "## Fixed Contract",
        "",
        f"- Fixture: `{report.fixture_id}` (`{report.fixture_path}`)",
        f"- Model: `{report.runtime_contract['model']}`; temperature `0`; max output tokens `{report.runtime_contract['model_max_tokens']}`",
        f"- Prompt / cumulative budgets: `{report.runtime_contract.get('prompt_input_token_budget', '?')}` prompt; `{report.runtime_contract.get('token_budget', '?')}/{report.runtime_contract.get('token_hard_budget', '?')}` soft/hard; `{report.runtime_contract.get('final_submit_reserve_tokens', '?')}` submit reserve",
        f"- Loop / tools: `{report.runtime_contract.get('review_max_iterations', '?')}` review iterations; `{report.runtime_contract.get('agent_max_tool_calls', '?')}` tool calls; request/run timeouts `{report.runtime_contract.get('model_request_timeout_seconds', '?')}/{report.runtime_contract.get('agent_run_timeout_seconds', '?')}` seconds",
        f"- Gates: verifier `{report.runtime_contract.get('finding_verifier_mode', '?')}`; workflow `{report.runtime_contract.get('workflow_enforcement', '?')}`",
        f"- Order: `{' → '.join(report.run_order)}`; one measured attempt per variant; no retry; no warm run",
        "- Matcher: existing Eval matcher; fixture and gold were not modified",
        "",
        "## Summary Matrix",
        "",
        "| Stage | A-agent-search | B1-graph-hybrid-cold |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| {label} | {cell(a, field)} | {cell(b, field)} |" for label, field in rows
    )
    lines.extend(
        [
            "",
            "## Failure Attribution Matrix",
            "",
            "| Variant | Failure Stage | Evidence | Interpretation |",
            "|---|---|---|---|",
        ]
    )
    for item in (a, b):
        evidence = item.skip_reason if item.skipped else item.failure_evidence
        interpretation = (
            "shared runtime blocker; not measured"
            if item.skipped
            else item.final_diagnosis
        )
        lines.append(
            f"| {item.variant_id} | {item.failure_stage} | {evidence or '—'} | {interpretation} |"
        )
    lines.extend(["", "## Per-variant Audit", ""])
    for item in (a, b):
        lines.extend([f"### {item.variant_id}", ""])
        if item.skipped:
            lines.extend([item.skip_reason, ""])
            continue
        lines.extend(
            [
                f"- Run ID: `{item.run_id}`",
                f"- Runtime: schema_valid={item.schema_valid}, placeholder={item.placeholder_summary}, workflow_invalid={item.workflow_invalid}, finish_reasons={item.finish_reasons or ['none']}, budget={item.budget_state}",
                f"- Runtime errors: provider={item.model_provider_call_errors or ['none']}; other={item.runtime_errors or ['none']}",
                f"- Context path: {', '.join(item.context_paths) or 'none'}; graph_status={item.graph_status}; cache_mode={item.graph_cache_mode}; cache_hit={item.cache_hit}; manifests={item.manifest_count}; fallback={item.graph_fallback_reason or 'none'}",
                f"- Discovery: {item.reviewer_discovered_gold}; evidence: {' | '.join(item.discovery_evidence) or 'no full visible/draft semantic statement'}",
                f"- Draft: record_draft_finding={item.record_draft_finding_called}; count={item.draft_finding_count}; correct_file_symbol={item.draft_correct_file_symbol}; correct_semantics={item.draft_correct_semantics}",
                f"- Submit: {item.submit_review}; summary_nonempty={item.submit_summary_nonempty}; issues={item.submitted_issue_count}; blank={item.blank_submit}; schema_invalid={item.submit_schema_invalid}; contains_gold={item.submitted_gold_issue}",
                f"- Recovery: {item.length_recovery}; required={item.recovery_required}; attempted={item.recovery_attempted}; evidence_preserved={item.recovery_evidence_preserved}; gold_preserved={item.recovery_submit_preserved_gold}",
                f"- Funnel: submitted={item.raw_submitted_finding_count}; pre={item.pre_verifier} ({item.pre_verifier_reasons or ['none']}); semantic={item.semantic_verifier} ({item.semantic_verifier_reasons or ['none']}); deterministic={item.deterministic_validation} ({item.deterministic_reasons or ['none']}); final_risk={item.final_risk_finding_count}",
                f"- Artifacts: EventLog `{item.event_log_path}`; Run Journal status={item.run_journal_status}, path=`{item.run_journal_path}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Ready for formal Graph A/B?",
            "",
            f"### {report.ready_for_formal_graph_ab}",
            "",
            report.conclusion_reason,
            "",
            report.next_step,
            "",
            "The result is a stage attribution for one reviewed positive fixture. Any A/B HIT/MISS difference is at most an early fixture-specific signal, not evidence that Graph changes recall.",
            "",
        ]
    )
    return "\n".join(lines)


def _skipped_b(reason: str) -> StageDiagnostic:
    return StageDiagnostic(
        variant_id="B1-graph-hybrid-cold",
        skipped=True,
        skip_reason=reason,
        failure_stage="provider_request",
        failure_evidence=reason,
        final_diagnosis=reason,
    )


async def run_smoke(
    *,
    core_config_path: Path,
    fixture_id: str,
    artifact_dir: Path,
    progress: Any = print,
) -> ReviewerRuntimeSmokeReport:
    config = core_eval.load_core_config(core_config_path)
    spec = next(item for item in config.fixtures if item.fixture_id == fixture_id)
    fixture_path = ROOT / spec.path
    fixture = Fixture.model_validate_json(fixture_path.read_text(encoding="utf-8"))
    core_eval._validate_gold_alignment(spec, fixture)
    variants = [
        EvalVariant(
            id="A-agent-search",
            context_mode="agent_search",
            graph_cache_mode="disabled",
        ),
        EvalVariant(
            id="B1-graph-hybrid-cold",
            context_mode="graph_hybrid",
            graph_cache_mode="cold",
        ),
    ]
    runtime: CoreRuntimeConfig = config.runtime
    env = {
        "MODEL_NAME": MODEL_NAME,
        "MODEL_MAX_TOKENS": str(runtime.model_max_tokens),
        "PROMPT_INPUT_TOKEN_BUDGET": str(runtime.prompt_input_token_budget),
        "TOKEN_BUDGET": str(runtime.token_budget),
        "TOKEN_HARD_BUDGET": str(runtime.token_hard_budget),
        "FINAL_SUBMIT_RESERVE_TOKENS": str(runtime.final_submit_reserve_tokens),
        "FINAL_SUBMIT_PROMPT_TOKEN_BUDGET": str(
            runtime.final_submit_prompt_token_budget
        ),
        "FINAL_SUBMIT_FEEDBACK_TOKEN_BUDGET": str(
            runtime.final_submit_feedback_token_budget
        ),
        "EVAL_REVIEW_MAX_ITERATIONS_CAP": str(runtime.review_max_iterations),
        "AGENT_TRACE_DETAIL": "full",
        "AGENT_TRACE_LOG_TOOL_BODY": "true",
        "AGENT_TRACE_MAX_CHARS": "8000",
    }
    originals = {key: os.environ.get(key) for key in env}
    diagnostics: list[StageDiagnostic] = []
    results: dict[str, EvalResult] = {}
    lifecycles: dict[str, dict[str, Any]] = {}
    artifact_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.environ.update(env)
        for variant in variants:
            if (
                variant.id == "B1-graph-hybrid-cold"
                and diagnostics
                and _shared_runtime_blocker(diagnostics[0])
            ):
                reason = "B skipped because shared runtime blocker was observed in A"
                diagnostics.append(_skipped_b(reason))
                break
            index_path = artifact_dir / "B1-graph-hybrid-cold.sqlite"
            if variant.context_mode == "graph_hybrid":
                clear_index(index_path)
            progress(f"START {fixture.id} {variant.id} measured_attempt=1")
            result, lifecycle = await run_single_lifecycle(
                fixture.model_copy(deep=True),
                variant=variant,
                relation_graph_index_path=(
                    index_path if variant.context_mode == "graph_hybrid" else None
                ),
                prime_graph_index=False,
                temperature=runtime.temperature,
                review_max_iterations=runtime.review_max_iterations,
                diagnostic_artifact_dir=artifact_dir,
            )
            diagnostic = diagnose_attempt(
                spec=spec,
                fixture=fixture,
                variant=variant,
                result=result,
                lifecycle=lifecycle,
            )
            diagnostics.append(diagnostic)
            results[variant.id] = result
            lifecycles[variant.id] = lifecycle
            progress(
                f"DONE  {fixture.id} {variant.id} run_id={result.run_id or 'none'} diagnosis={diagnostic.failure_stage}"
            )
    finally:
        for key, value in originals.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    settings = get_settings()
    conclusion, reason, next_step = _conclusion(diagnostics)
    return ReviewerRuntimeSmokeReport(
        fixture_id=fixture.id,
        fixture_path=spec.path,
        gold_description=spec.gold_findings[0].description,
        runtime_contract={
            "source": Path(core_config_path).as_posix(),
            "model": MODEL_NAME,
            "temperature": runtime.temperature,
            "model_max_tokens": runtime.model_max_tokens,
            "prompt_input_token_budget": runtime.prompt_input_token_budget,
            "token_budget": runtime.token_budget,
            "token_hard_budget": runtime.token_hard_budget,
            "final_submit_reserve_tokens": runtime.final_submit_reserve_tokens,
            "final_submit_prompt_token_budget": runtime.final_submit_prompt_token_budget,
            "final_submit_feedback_token_budget": runtime.final_submit_feedback_token_budget,
            "review_max_iterations": runtime.review_max_iterations,
            "agent_max_tool_calls": settings.agent_max_tool_calls,
            "model_request_timeout_seconds": settings.model_request_timeout_seconds,
            "agent_tool_timeout_seconds": settings.agent_tool_timeout_seconds,
            "agent_run_timeout_seconds": settings.agent_run_timeout_seconds,
            "finding_verifier_mode": settings.finding_verifier_mode,
            "workflow_enforcement": settings.review_workflow_enforcement,
        },
        diagnostics=diagnostics,
        results=results,
        lifecycle=lifecycles,
        ready_for_formal_graph_ab=conclusion,
        conclusion_reason=reason,
        next_step=next_step,
    )


def reanalyze_report(
    report: ReviewerRuntimeSmokeReport,
    *,
    core_config_path: Path,
) -> ReviewerRuntimeSmokeReport:
    """Rebuild stage attribution from persisted measured artifacts without rerunning."""

    config = core_eval.load_core_config(core_config_path)
    spec = next(
        item for item in config.fixtures if item.fixture_id == report.fixture_id
    )
    fixture = Fixture.model_validate_json(
        (ROOT / spec.path).read_text(encoding="utf-8")
    )
    variants = {
        "A-agent-search": EvalVariant(
            id="A-agent-search",
            context_mode="agent_search",
            graph_cache_mode="disabled",
        ),
        "B1-graph-hybrid-cold": EvalVariant(
            id="B1-graph-hybrid-cold",
            context_mode="graph_hybrid",
            graph_cache_mode="cold",
        ),
    }
    previous = {item.variant_id: item for item in report.diagnostics}
    diagnostics: list[StageDiagnostic] = []
    for variant_id in report.run_order:
        result = report.results.get(variant_id)
        if result is None:
            skipped_item = previous[variant_id]
            if skipped_item.skipped and diagnostics:
                inherited_stage = diagnostics[0].failure_stage
                skipped_item = skipped_item.model_copy(
                    update={
                        "failure_stage": inherited_stage,
                        "failure_evidence": skipped_item.skip_reason,
                        "final_diagnosis": skipped_item.skip_reason,
                    }
                )
            diagnostics.append(skipped_item)
            continue
        diagnostics.append(
            diagnose_attempt(
                spec=spec,
                fixture=fixture,
                variant=variants[variant_id],
                result=result,
                lifecycle=report.lifecycle.get(variant_id, {}),
            )
        )
    conclusion, reason, next_step = _conclusion(diagnostics)
    runtime = config.runtime
    settings = get_settings()
    runtime_contract = {
        **report.runtime_contract,
        "source": Path(core_config_path).as_posix(),
        "model": MODEL_NAME,
        "temperature": runtime.temperature,
        "model_max_tokens": runtime.model_max_tokens,
        "prompt_input_token_budget": runtime.prompt_input_token_budget,
        "token_budget": runtime.token_budget,
        "token_hard_budget": runtime.token_hard_budget,
        "final_submit_reserve_tokens": runtime.final_submit_reserve_tokens,
        "final_submit_prompt_token_budget": runtime.final_submit_prompt_token_budget,
        "final_submit_feedback_token_budget": runtime.final_submit_feedback_token_budget,
        "review_max_iterations": runtime.review_max_iterations,
        "agent_max_tool_calls": settings.agent_max_tool_calls,
        "model_request_timeout_seconds": settings.model_request_timeout_seconds,
        "agent_tool_timeout_seconds": settings.agent_tool_timeout_seconds,
        "agent_run_timeout_seconds": settings.agent_run_timeout_seconds,
        "finding_verifier_mode": settings.finding_verifier_mode,
        "workflow_enforcement": settings.review_workflow_enforcement,
    }
    return report.model_copy(
        update={
            "generated_at": datetime.now(UTC).isoformat(),
            "diagnostics": diagnostics,
            "runtime_contract": runtime_contract,
            "ready_for_formal_graph_ab": conclusion,
            "conclusion_reason": reason,
            "next_step": next_step,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-config", type=Path, default=DEFAULT_CORE_CONFIG)
    parser.add_argument("--fixture-id", default=DEFAULT_FIXTURE_ID)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument(
        "--reanalyze-json",
        type=Path,
        help="Rebuild diagnostics from an existing measured report; makes no model calls.",
    )
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-markdown", type=Path, default=DEFAULT_OUTPUT_MARKDOWN)
    args = parser.parse_args()
    if args.reanalyze_json is not None:
        existing = ReviewerRuntimeSmokeReport.model_validate_json(
            args.reanalyze_json.read_text(encoding="utf-8")
        )
        report = reanalyze_report(existing, core_config_path=args.core_config)
    else:
        report = asyncio.run(
            run_smoke(
                core_config_path=args.core_config,
                fixture_id=args.fixture_id,
                artifact_dir=args.artifact_dir,
            )
        )
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    args.output_markdown.write_text(render_markdown(report), encoding="utf-8")
    print(f"JSON {args.output_json}")
    print(f"MARKDOWN {args.output_markdown}")
    print(report.ready_for_formal_graph_ab)


if __name__ == "__main__":
    main()
