"""Agent main loop — orchestrates the 5-phase cycle."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json as _json
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from src.analyzer.context_builder import ContextBuilder
from src.analyzer.context_mode import ReviewContextMode
from src.analyzer.context_state import ContextState, DecisionStep, ErrorDetail
from src.analyzer.context_strategy import ContextStrategy, build_context_strategy
from src.analyzer.diff_lines import changed_new_lines_by_file
from src.analyzer.evidence_binding import bind_candidate_evidence
from src.analyzer.event_log import EventEntry, EventLog, EventType
from src.analyzer.finding_verifier import (
    DeterministicValidationStats,
    FindingVerifier,
    apply_verifications,
    build_candidates,
    narrowable_auxiliary_rejections,
    review_candidate_severities,
    validate_verifications_with_stats,
)
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.review_policy import evaluate_issue_filter
from src.analyzer.result_processor import ResultProcessor
from src.analyzer.root_cause import RootCauseConsolidator
from src.analyzer.schemas import (
    AnalysisPlan,
    DebugRequest,
    DebugResponse,
    FindingVerificationBatch,
    ReviewRequest,
    ReviewResponse,
)
from src.analyzer.trace import TraceRecorder
from src.analyzer.verifier_context import (
    build_candidate_verifier_context,
    capture_verifier_tool_evidence,
)
from src.config import get_settings
from src.models.client import ModelClient
from src.models.conversation import ModelConversation
from src.models.exceptions import ModelClientError, ModelTimeoutError
from src.models.schemas import DraftFinding, DraftFindingInput, ModelResponse
from src.orchestrator.draft_findings import (
    DraftFindingStore,
    extract_visible_draft_finding,
)
from src.orchestrator.run_journal import (
    LengthRecoveryJournalPayload,
    ModelResponseJournalPayload,
    PendingRunJournalEntry,
    RunJournal,
    RunJournalError,
    ToolResultJournalPayload,
    redact_sensitive_values,
)
from src.orchestrator.review_workflow import ReviewWorkflowTracker
from src.orchestrator.tool_schemas import (
    build_draft_finding_tool_schema,
    build_submit_tool_schemas,
    build_tool_schemas,
)
from src.tools import create_default_registry
from src.tools.base import BaseTool, ToolRegistry, ToolResult, ToolSafety, ToolSpec
from src.tools.exceptions import ToolError
from src.tools.path_utils import tool_workspace_root
from src.tools.review_context import ReviewToolContext


class AgentOrchestrator:
    """5-phase orchestrator for review/debug sessions."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        confirm_high_risk: Any | None = None,
        permission_mode: Literal["default", "plan"] | None = None,
        temperature: float | None = None,
        review_max_iterations: int | None = None,
        debug_max_iterations: int | None = None,
        review_min_tool_iterations: int | None = None,
        finding_verifier: Any | None = None,
        finding_verifier_mode: Literal["off", "shadow", "enforce"] | None = None,
        review_workflow_enforcement: Literal["off", "warn", "enforce"] | None = None,
        review_diff_first_changed_files: bool | None = None,
        relation_graph_index_path: str | Path | None = None,
        context_mode: ReviewContextMode | None = None,
        context_strategy: ContextStrategy | None = None,
    ) -> None:
        self._settings = get_settings()
        self._external_registry: ToolRegistry | None = registry
        self._registry = registry or create_default_registry(include_execute=False)
        self._context_builder = ContextBuilder()
        self._result_processor = ResultProcessor(
            token_budget=self._settings.token_budget,
            token_hard_budget=self._settings.token_hard_budget,
        )
        self._model_client: ModelClient | None = None
        self._confirm_high_risk = confirm_high_risk
        self._permission_mode: Literal["default", "plan"] = (
            permission_mode or self._settings.permission_mode
        )
        self._run_id = ""
        self._event_log: EventLog | None = None
        self._run_journal: RunJournal | None = None
        self._draft_finding_store = DraftFindingStore()
        self._last_plan: AnalysisPlan | None = None
        self._tool_feedback: list[dict[str, Any]] = []
        self._feedback_digest_index: dict[str, dict[str, Any]] = {}
        self._tool_dedup_cache: dict[str, ToolResult] = {}
        self._submit_review_seen_any = False
        self._submit_debug_seen_any = False
        self._latest_tokens = 0
        self._model_conversation = ModelConversation()
        self._total_tokens = 0
        self._iteration = 0
        self._max_iterations = 1
        self._blocking_error = False
        self._budget_exhausted = False
        self._budget_state: str = "none"
        self._model_completed = False
        self._last_decision_reason: str = ""
        self._workspace_root: Path | None = None
        self._run_started_at = 0.0
        self._run_timeout_seconds = self._settings.agent_run_timeout_seconds
        self._model_timeout_seen = False
        self._model_incomplete_seen = False
        self._model_length_finish_seen = False
        self._length_recovery_required = False
        self._length_recovery_attempted = 0
        self._length_recovery_succeeded = 0
        self._length_recovery_failed = 0
        self._length_recovery_source_response_ids: list[str] = []
        self._final_submit_evidence_included_count = 0
        self._final_submit_evidence_token_count = 0
        self._final_submit_evidence_truncated_count = 0
        self._pre_budget_submit_attempted = False
        self._temperature = temperature
        self._review_max_iterations_override = review_max_iterations
        self._debug_max_iterations_override = debug_max_iterations
        self._review_min_tool_iterations = max(0, review_min_tool_iterations or 0)
        self._finding_verifier = finding_verifier
        self._finding_verifier_mode = (
            finding_verifier_mode or self._settings.finding_verifier_mode
        )
        self._workflow_enforcement = (
            review_workflow_enforcement or self._settings.review_workflow_enforcement
        )
        self._review_diff_first_changed_files = (
            self._settings.review_diff_first_changed_files
            if review_diff_first_changed_files is None
            else bool(review_diff_first_changed_files)
        )
        self._relation_graph_index_path_override = relation_graph_index_path
        self._context_mode: ReviewContextMode = (
            context_mode or self._settings.review_context_mode
        )
        self._context_strategy_override = context_strategy
        self._review_workflow = ReviewWorkflowTracker()
        self._workflow_reprompt_count = 0
        self._model_raw_issue_count = 0
        self._submitted_issue_count = 0
        self._policy_passed_issue_count = 0
        self._policy_rejected_issue_count = 0
        self._non_risk_issue_count = 0
        self._verifier_candidate_count = 0
        self._risk_candidate_count = 0
        self._filter_rescue_candidate_count = 0
        self._severity_calibration_candidate_count = 0
        self._semantic_rejected_count = 0
        self._deterministic_rejected_count = 0
        self._verifier_accepted_count = 0
        self._verifier_rejected_count = 0
        self._verifier_needs_evidence_count = 0
        self._verifier_downgraded_count = 0
        self._high_confidence_info_issue_count = 0
        self._severity_reviewed_count = 0
        self._severity_promoted_count = 0
        self._consolidator_block_count = 0
        self._consolidator_proposal_count = 0
        self._consolidator_accepted_cluster_count = 0
        self._consolidator_rejected_cluster_count = 0
        self._final_root_cause_count = 0
        self._finding_inflation_ratio = 0.0
        self._verifier_tool_evidence: list[dict[str, Any]] = []
        self._tool_call_count = 0
        self._tool_name_counts: dict[str, int] = {}
        self._reviewer_latency_seconds = 0.0
        self._verifier_latency_seconds = 0.0
        self._consolidation_latency_seconds = 0.0
        self._model_response_journal_writes = 0
        self._tool_result_journal_writes = 0
        self._draft_findings_created = 0
        self._draft_findings_from_visible_content = 0
        self._trace_recorder = TraceRecorder(
            detail_mode=self._settings.agent_trace_detail,
            max_chars=self._settings.agent_trace_max_chars,
            log_tool_body=self._settings.agent_trace_log_tool_body,
        )

    async def run_review(self, request: ReviewRequest) -> ReviewResponse:
        """Run review mode through the orchestrator loop."""
        self._reset_run(
            max_iterations=(
                self._review_max_iterations_override
                or self._settings.review_max_iterations
            ),
            repo_path=request.repo_path,
        )
        review_context = self._build_review_tool_context(request)
        if self._external_registry is None:
            self._registry = create_default_registry(
                include_execute=False,
                review_context=review_context,
            )
        state = self.prepare_context(request)
        await self._prepare_review_context(state, request)
        reviewer_started = perf_counter()
        await self._maybe_prefetch_review_changed_files(state, request)
        if self._workflow_enforcement != "off":
            if request.diff_mode:
                self._complete_workflow_step("inspect_diff")
            else:
                self._skip_workflow_step("inspect_diff", "full_repo_review")
                self._skip_workflow_step("inspect_changed_context", "full_repo_review")
        response: ReviewResponse | DebugResponse | None = None
        while True:
            tool_specs = (
                [] if self._permission_mode == "plan" else self._registry.list_specs()
            )
            plan = await self.analyze(state, request, tool_specs)
            self._last_plan = plan
            self._observe_incomplete_plan(plan, state)
            tool_results = await self.execute_tools(plan, self._registry, state)
            self._observe_workflow_tools(plan, tool_results)
            response = self.format_result(state, tool_results)
            if not self.should_continue(state, response):
                break
            if self._should_pre_budget_submit(state):
                self._pre_budget_submit_attempted = True
                self._record_pre_budget_submit("attempt", state)
                submit_plan = await self.analyze(
                    state, request, tool_specs=[], force_submit=True
                )
                self._last_plan = submit_plan
                self._observe_incomplete_plan(submit_plan, state)
                response = self.format_result(state, tool_results=[])
                self._record_pre_budget_submit("completed", state, submit_plan)
                break
            self._iteration += 1
        response = await self._maybe_force_submit_review(state, request, response)
        assert isinstance(response, ReviewResponse)
        response = await self._maybe_recover_review_workflow(response, request, state)
        self._reviewer_latency_seconds = perf_counter() - reviewer_started
        verifier_started = perf_counter()
        response = await self._verify_review_response(response, request, state)
        verifier_total = perf_counter() - verifier_started
        self._verifier_latency_seconds = max(
            0.0, verifier_total - self._consolidation_latency_seconds
        )
        response = self._finalize_review_workflow(response, state)
        self._record_finding_funnel(response)
        self._record_review_telemetry(state)
        self._close_event_log()
        return response

    async def _prepare_review_context(
        self,
        state: ContextState,
        request: ReviewRequest,
    ) -> None:
        """Apply exactly one explicit context strategy to the shared review state."""

        strategy = self._context_strategy_override or build_context_strategy(
            self._context_mode,
            settings=self._settings,
            workspace_root=self._workspace_root,
            relation_graph_index_path=self._relation_graph_index_path_override,
            record_event=self._record_event,
        )
        prepared = await strategy.prepare(request)
        state.context_mode = prepared.context_mode
        state.candidate_context_manifests = list(prepared.candidate_context_manifests)
        state.relation_graph_summary = dict(prepared.graph_telemetry)
        self._record_event(
            EventType.CONTEXT_TELEMETRY,
            "context_strategy",
            {
                "context_mode": prepared.context_mode,
                **prepared.graph_telemetry,
            },
        )

    async def _verify_review_response(
        self,
        response: ReviewResponse,
        request: ReviewRequest,
        state: ContextState,
    ) -> ReviewResponse:
        submitted_report = (
            self._last_plan.draft_review
            if self._last_plan is not None and self._last_plan.draft_review is not None
            else response.report
        )
        filter_decisions = [
            evaluate_issue_filter(issue) for issue in submitted_report.issues
        ]
        self._model_raw_issue_count = len(submitted_report.issues)
        self._submitted_issue_count = self._model_raw_issue_count
        self._policy_passed_issue_count = sum(
            decision.passed for decision in filter_decisions
        )
        self._policy_rejected_issue_count = (
            self._submitted_issue_count - self._policy_passed_issue_count
        )
        self._non_risk_issue_count = sum(
            decision.severity.value not in {"critical", "warning"}
            for decision in filter_decisions
        )
        for original_index, decision in enumerate(filter_decisions):
            self._record_event(
                EventType.FINDING_FILTER_DECISION,
                "filter_findings",
                decision.event_payload(original_index=original_index),
            )

        severity_review = review_candidate_severities(response.report, request)
        self._high_confidence_info_issue_count = (
            severity_review.high_confidence_info_count
        )
        self._severity_reviewed_count = severity_review.reviewed_count
        if self._finding_verifier_mode == "enforce":
            self._severity_promoted_count = 0
            candidates = build_candidates(
                submitted_report,
                iteration=self._iteration,
                request=request,
                include_boundary=True,
            )
        else:
            response.report = severity_review.report
            self._severity_promoted_count = severity_review.promoted_count
            candidates = build_candidates(response.report, iteration=self._iteration)
        self._verifier_candidate_count = len(candidates)
        self._risk_candidate_count = sum(
            item.candidate_kind == "risk" for item in candidates
        )
        self._filter_rescue_candidate_count = sum(
            item.candidate_kind == "filter_rescue" for item in candidates
        )
        self._severity_calibration_candidate_count = sum(
            item.candidate_kind == "severity_calibration" for item in candidates
        )
        evidence_bound_count = sum(
            1
            for item in candidates
            if item.issue.evidence.strip() and item.issue.location.strip()
        )
        structured_hypothesis_count = sum(
            item.issue.is_structured_hypothesis for item in candidates
        )
        evidence_complete_count = sum(
            bool(item.issue.cause_evidence)
            and bool(item.issue.contract_evidence)
            and (not item.issue.trigger or bool(item.issue.trigger_evidence))
            and (not item.issue.impact or bool(item.issue.impact_evidence))
            for item in candidates
        )
        self._record_event(
            EventType.FINDING_CANDIDATES_BUILT,
            "verify_findings",
            {
                "candidate_count": len(candidates),
                "model_raw_issue_count": self._model_raw_issue_count,
                "submitted_issue_count": self._submitted_issue_count,
                "policy_passed_issue_count": self._policy_passed_issue_count,
                "policy_rejected_issue_count": self._policy_rejected_issue_count,
                "non_risk_issue_count": self._non_risk_issue_count,
                "verifier_candidate_count": self._verifier_candidate_count,
                "risk_candidate_count": self._risk_candidate_count,
                "filter_rescue_candidate_count": (self._filter_rescue_candidate_count),
                "severity_calibration_candidate_count": (
                    self._severity_calibration_candidate_count
                ),
                "evidence_bound_count": evidence_bound_count,
                "structured_hypothesis_count": structured_hypothesis_count,
                "evidence_complete_count": evidence_complete_count,
                "high_confidence_info_issue_count": self._high_confidence_info_issue_count,
                "severity_reviewed_count": self._severity_reviewed_count,
                "severity_promoted_count": self._severity_promoted_count,
                "verifier_context_entry_count": len(self._verifier_tool_evidence),
                "candidates": [
                    {
                        "candidate_id": item.candidate_id,
                        "source_issue_index": item.source_issue_index,
                        "candidate_kind": item.candidate_kind,
                        "finding_id": item.issue.finding_id,
                        "location": item.issue.location,
                    }
                    for item in candidates
                ],
                "mode": self._finding_verifier_mode,
            },
        )
        if self._workflow_enforcement != "off":
            if candidates:
                self._complete_workflow_step("validate_candidate_draft")
            else:
                self._skip_workflow_step(
                    "validate_candidate_draft", "no_candidate_findings"
                )
                self._skip_workflow_step(
                    "semantic_verify_findings", "no_risk_candidates"
                )
        if not candidates or self._finding_verifier_mode == "off":
            if candidates and self._workflow_enforcement != "off":
                self._fail_workflow_step(
                    "semantic_verify_findings", "finding_verifier_disabled"
                )
            return response
        verifier = self._finding_verifier
        if verifier is None and self._model_client is not None:
            verifier = FindingVerifier(self._model_client)
        if verifier is None:
            if self._finding_verifier_mode == "enforce":
                self._semantic_rejected_count = len(candidates)
            self._record_event(
                EventType.FINDING_VERIFICATION_FAILED,
                "verify_findings",
                {
                    "candidate_count": len(candidates),
                    "mode": self._finding_verifier_mode,
                    "reason": "verifier_unavailable",
                },
            )
            if self._finding_verifier_mode == "enforce":
                response.report = self._result_processor.merge_review_reports(
                    [
                        apply_verifications(
                            response.report,
                            FindingVerificationBatch(),
                            mode="enforce",
                            candidates=candidates,
                        )
                    ]
                )
            if self._workflow_enforcement != "off":
                self._fail_workflow_step(
                    "semantic_verify_findings", "verifier_unavailable"
                )
            return response
        try:
            returned_batch = await self._call_finding_verifier(
                verifier,
                candidates,
                request,
                state,
            )
            raw_batch, batch, validation_stats = self._normalize_verifier_result(
                verifier,
                candidates,
                returned_batch,
                request,
                state,
            )
            self._consume_verifier_tokens(verifier)
        except Exception as exc:  # noqa: BLE001
            self._record_event(
                EventType.FINDING_VERIFICATION_FAILED,
                "verify_findings",
                {
                    "candidate_count": len(candidates),
                    "mode": self._finding_verifier_mode,
                    "reason": exc.__class__.__name__,
                    "message": str(exc)[:500],
                },
            )
            raw_batch = FindingVerificationBatch()
            batch = FindingVerificationBatch()
            validation_stats = DeterministicValidationStats()
        first_pass_accept_count = sum(
            item.status == "accepted" for item in batch.results
        )
        needs_evidence_ids = {
            item.candidate_id
            for item in batch.results
            if item.status == "needs_evidence"
        }
        raw_accepted_ids = {
            item.candidate_id for item in raw_batch.results if item.status == "accepted"
        }
        auxiliary_rejections = {
            candidate_id: details
            for candidate_id, details in narrowable_auxiliary_rejections(
                validation_stats
            ).items()
            if candidate_id in raw_accepted_ids
        }
        auxiliary_narrowing_ids = set(auxiliary_rejections)
        repair_candidate_ids = needs_evidence_ids | auxiliary_narrowing_ids
        if repair_candidate_ids and self._settings.verifier_max_repair_rounds > 0:
            repair_candidates = [
                item for item in candidates if item.candidate_id in repair_candidate_ids
            ]
            if needs_evidence_ids:
                state.constraints.append(
                    "verifier_needs_evidence:" + ",".join(sorted(needs_evidence_ids))
                )
            for candidate_id in sorted(auxiliary_narrowing_ids):
                details = auxiliary_rejections[candidate_id]
                state.constraints.append(
                    "verifier_auxiliary_narrowing:"
                    + _json.dumps(
                        {
                            "candidate_id": candidate_id,
                            "failed_evidence": [
                                {
                                    "role": item.evidence_role,
                                    "index": item.evidence_index,
                                    "rule": item.rule,
                                    "file": item.file,
                                    "line": item.line,
                                    "end_line": item.end_line,
                                    "field": item.field,
                                }
                                for item in details
                            ],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            try:
                returned_repaired = await self._call_finding_verifier(
                    verifier,
                    repair_candidates,
                    request,
                    state,
                )
                raw_repaired, repaired, repaired_stats = (
                    self._normalize_verifier_result(
                        verifier,
                        repair_candidates,
                        returned_repaired,
                        request,
                        state,
                    )
                )
                self._consume_verifier_tokens(verifier)
            except Exception as exc:  # noqa: BLE001
                raw_repaired = FindingVerificationBatch()
                repaired = FindingVerificationBatch()
                repaired_stats = DeterministicValidationStats()
                self._record_event(
                    EventType.FINDING_VERIFICATION_FAILED,
                    "verify_findings",
                    {
                        "candidate_count": len(repair_candidates),
                        "mode": self._finding_verifier_mode,
                        "reason": exc.__class__.__name__,
                        "stage": "evidence_repair",
                    },
                )
            repaired_by_id = {item.candidate_id: item for item in repaired.results}
            raw_repaired_by_id = {
                item.candidate_id: item for item in raw_repaired.results
            }
            batch = FindingVerificationBatch(
                results=[
                    repaired_by_id.get(item.candidate_id, item)
                    if item.candidate_id in repair_candidate_ids
                    else item
                    for item in batch.results
                ]
            )
            raw_batch = FindingVerificationBatch(
                results=[
                    raw_repaired_by_id.get(item.candidate_id, item)
                    if item.candidate_id in repair_candidate_ids
                    else item
                    for item in raw_batch.results
                ]
            )
            validation_stats = DeterministicValidationStats(
                checked_count=(
                    validation_stats.checked_count + repaired_stats.checked_count
                ),
                passed_count=(
                    validation_stats.passed_count + repaired_stats.passed_count
                ),
                rejected_count=(
                    validation_stats.rejected_count + repaired_stats.rejected_count
                ),
                rejection_details=(
                    *validation_stats.rejection_details,
                    *repaired_stats.rejection_details,
                ),
            )
            self._record_event(
                EventType.FINDING_EVIDENCE_REPAIR_COMPLETED,
                "verify_findings",
                {
                    "round": 1,
                    "candidate_count": len(repair_candidates),
                    "needs_evidence_candidate_count": len(needs_evidence_ids),
                    "auxiliary_narrowing_candidate_count": len(auxiliary_narrowing_ids),
                    "resolved_count": sum(
                        item.status in {"accepted", "rejected", "downgraded"}
                        for item in repaired.results
                    ),
                },
            )
        accepted = sum(item.status == "accepted" for item in batch.results)
        rejected = sum(item.status == "rejected" for item in batch.results)
        needs_evidence = sum(item.status == "needs_evidence" for item in batch.results)
        downgraded = sum(item.status == "downgraded" for item in batch.results)
        raw_accepted = sum(item.status == "accepted" for item in raw_batch.results)
        raw_rejected = sum(item.status == "rejected" for item in raw_batch.results)
        raw_needs_evidence = sum(
            item.status == "needs_evidence" for item in raw_batch.results
        )
        raw_downgraded = sum(item.status == "downgraded" for item in raw_batch.results)
        self._semantic_rejected_count = max(0, len(candidates) - raw_accepted)
        self._deterministic_rejected_count = validation_stats.rejected_count
        self._verifier_accepted_count = accepted
        self._verifier_rejected_count = rejected
        self._verifier_needs_evidence_count = needs_evidence
        self._verifier_downgraded_count = downgraded
        self._record_event(
            EventType.FINDING_VERIFICATION_COMPLETED,
            "verify_findings",
            {
                "candidate_count": len(candidates),
                "model_raw_issue_count": self._model_raw_issue_count,
                "submitted_issue_count": self._submitted_issue_count,
                "policy_passed_issue_count": self._policy_passed_issue_count,
                "policy_rejected_issue_count": self._policy_rejected_issue_count,
                "non_risk_issue_count": self._non_risk_issue_count,
                "verifier_candidate_count": self._verifier_candidate_count,
                "risk_candidate_count": self._risk_candidate_count,
                "filter_rescue_candidate_count": (self._filter_rescue_candidate_count),
                "severity_calibration_candidate_count": (
                    self._severity_calibration_candidate_count
                ),
                "accepted_count": accepted,
                "verifier_accepted_count": accepted,
                "rejected_count": rejected,
                "verifier_rejected_count": rejected,
                "needs_evidence_count": needs_evidence,
                "verifier_needs_evidence_count": needs_evidence,
                "downgraded_count": downgraded,
                "verifier_downgraded_count": downgraded,
                "first_pass_accept_count": first_pass_accept_count,
                "raw_accepted_count": raw_accepted,
                "raw_rejected_count": raw_rejected,
                "raw_needs_evidence_count": raw_needs_evidence,
                "raw_downgraded_count": raw_downgraded,
                "raw_reason_codes": [
                    code for item in raw_batch.results for code in item.reason_codes
                ],
                "raw_verdicts": [
                    {
                        "candidate_id": item.candidate_id,
                        "status": item.status,
                        "reason_codes": item.reason_codes,
                    }
                    for item in raw_batch.results
                ],
                "deterministic_evidence_checked_count": validation_stats.checked_count,
                "deterministic_evidence_passed_count": validation_stats.passed_count,
                "deterministic_evidence_rejected_count": validation_stats.rejected_count,
                "deterministic_rejection_details": [
                    item.model_dump(mode="json")
                    for item in validation_stats.rejection_details
                ],
                "semantic_rejected_count": self._semantic_rejected_count,
                "deterministic_rejected_count": self._deterministic_rejected_count,
                "mode": self._finding_verifier_mode,
                "reason_codes": [
                    code for item in batch.results for code in item.reason_codes
                ],
                "verdicts": [
                    {
                        "candidate_id": item.candidate_id,
                        "status": item.status,
                        "reason_codes": item.reason_codes,
                    }
                    for item in batch.results
                ],
            },
        )
        if self._workflow_enforcement != "off":
            terminal_ids = {
                item.candidate_id
                for item in batch.results
                if item.status in {"accepted", "rejected", "downgraded"}
            }
            if all(item.candidate_id in terminal_ids for item in candidates):
                self._complete_workflow_step("semantic_verify_findings")
            else:
                self._fail_workflow_step(
                    "semantic_verify_findings", "missing_verifier_verdict"
                )
        response.report = self._result_processor.merge_review_reports(
            [
                apply_verifications(
                    response.report,
                    batch,
                    mode=self._finding_verifier_mode,
                    candidates=candidates,
                )
            ]
        )
        response = self._consolidate_verified_findings(
            response,
            batch,
            request,
            state,
        )
        return response

    def _consolidate_verified_findings(
        self,
        response: ReviewResponse,
        batch: FindingVerificationBatch,
        request: ReviewRequest,
        state: ContextState,
    ) -> ReviewResponse:
        if not self._settings.root_cause_consolidation_enabled:
            return response
        accepted_ids = {
            item.candidate_id for item in batch.results if item.status == "accepted"
        }
        verified_risk = [
            issue
            for issue in response.report.issues
            if issue.severity.value in {"critical", "warning"}
            and issue.candidate_id in accepted_ids
        ]
        untouched = [
            issue for issue in response.report.issues if issue not in verified_risk
        ]
        if not verified_risk:
            return response
        consolidation_started = perf_counter()
        result = RootCauseConsolidator(
            max_block_size=(self._settings.root_cause_consolidation_max_block_size),
            conservative_mode=(
                self._settings.root_cause_consolidation_conservative_mode
            ),
            extra_retrieval_enabled=(
                self._settings.root_cause_consolidation_extra_retrieval_enabled
            ),
        ).consolidate(
            ReviewReport(summary=response.report.summary, issues=verified_risk),
            diff_text=request.diff_text or "",
            manifests=state.candidate_context_manifests,
        )
        response.report = ReviewReport(
            summary=response.report.summary,
            issues=[*result.report.issues, *untouched],
            schema_version=response.report.schema_version,
        )
        metrics = result.metrics
        self._consolidator_block_count = metrics.block_count
        self._consolidator_proposal_count = metrics.proposal_count
        self._consolidator_accepted_cluster_count = metrics.accepted_cluster_count
        self._consolidator_rejected_cluster_count = metrics.rejected_cluster_count
        self._final_root_cause_count = metrics.final_root_cause_count
        self._finding_inflation_ratio = metrics.finding_inflation_ratio
        self._record_event(
            EventType.FINDING_BLOCKS_BUILT,
            "root_cause_consolidation",
            {
                "block_count": metrics.block_count,
                "average_block_size": metrics.average_block_size,
                "signal_count": result.blocking.signal_count,
                "blocks": [
                    {
                        "block_id": block.block_id,
                        "finding_ids": block.finding_ids,
                        "signal_kinds": sorted(
                            {signal.kind for signal in block.signals}
                        ),
                    }
                    for block in result.blocking.blocks
                ],
            },
        )
        for proposal in result.proposals:
            self._record_event(
                EventType.ROOT_CAUSE_MERGE_PROPOSED,
                "root_cause_consolidation",
                {
                    "root_cause_id": proposal.root_cause_id,
                    "member_findings": proposal.member_findings,
                    "counterfactual_result": proposal.counterfactual_result,
                    "absorbed_roles": proposal.absorbed_roles,
                    "allowed_context_manifest_ids": (
                        proposal.allowed_context_manifest_ids
                    ),
                },
            )
        for verdict in result.verifications:
            self._record_event(
                EventType.CONSOLIDATION_VERIFICATION_COMPLETED,
                "consolidation_verifier",
                {
                    "root_cause_id": verdict.root_cause_id,
                    "accepted": verdict.accepted,
                    "reasons": verdict.reasons,
                },
            )
            if not verdict.accepted:
                self._record_event(
                    EventType.CONSOLIDATION_REJECTED,
                    "consolidation_verifier",
                    {
                        "root_cause_id": verdict.root_cause_id,
                        "reasons": verdict.reasons,
                        "fallback": "original_findings_separate",
                    },
                )
        manifest_hashes = {
            str(span.get("context_hash", ""))
            for manifest in state.candidate_context_manifests
            for span in manifest.get("included_spans", [])
            if isinstance(span, dict) and span.get("context_hash")
        }
        used_hashes = {
            evidence.context_hash
            for issue in result.report.issues
            for evidence in issue.all_evidence()
            if evidence.context_hash
        }
        edge_confidences = [
            float(evidence.edge_confidence)
            for issue in result.report.issues
            for evidence in issue.all_evidence()
            if evidence.edge_confidence is not None
        ]
        consolidation_payload = metrics.model_dump(mode="json")
        consolidation_payload.update(
            {
                "unused_context_ratio": (
                    1.0 - len(manifest_hashes & used_hashes) / len(manifest_hashes)
                    if manifest_hashes
                    else 0.0
                ),
                "edge_confidence_contribution": (
                    sum(edge_confidences) / len(edge_confidences)
                    if edge_confidences
                    else 0.0
                ),
                "evidence_complete_count": sum(
                    bool(issue.cause_evidence)
                    and bool(issue.contract_evidence)
                    and (not issue.trigger or bool(issue.trigger_evidence))
                    and (not issue.impact or bool(issue.impact_evidence))
                    for issue in result.report.issues
                    if issue.severity.value in {"critical", "warning"}
                ),
            }
        )
        self._record_event(
            EventType.ROOT_CAUSE_CONSOLIDATION_COMPLETED,
            "root_cause_consolidation",
            consolidation_payload,
        )
        self._consolidation_latency_seconds += perf_counter() - consolidation_started
        return response

    async def _maybe_recover_review_workflow(
        self,
        response: ReviewResponse,
        request: ReviewRequest,
        state: ContextState,
    ) -> ReviewResponse:
        if self._workflow_enforcement != "enforce":
            return response
        has_candidates = bool(response.report.issues)
        has_risk = any(
            issue.severity.value in {"critical", "warning"}
            for issue in response.report.issues
        )
        missing = self._review_workflow.missing_required(
            has_candidates=has_candidates,
            has_risk_candidates=has_risk,
        )
        recoverable = [
            item
            for item in missing
            if item.step_id in {"inspect_diff", "inspect_changed_context"}
        ]
        if not recoverable or self._workflow_reprompt_count >= 1:
            return response
        if any(item.step_id == "inspect_changed_context" for item in recoverable):
            await self._maybe_prefetch_review_changed_files(
                state,
                request,
                force=True,
                trigger="workflow_recovery",
            )
            missing = self._review_workflow.missing_required(
                has_candidates=has_candidates,
                has_risk_candidates=has_risk,
            )
            recoverable = [
                item
                for item in missing
                if item.step_id in {"inspect_diff", "inspect_changed_context"}
            ]
            if not recoverable:
                return response
        self._workflow_reprompt_count += 1
        state.constraints.append(
            "workflow_missing_required:"
            + ",".join(item.step_id for item in recoverable)
        )
        recovery_plan = await self.analyze(
            state,
            request,
            self._registry.list_specs(),
        )
        self._total_tokens += self._latest_tokens
        self._budget_state = self._result_processor.budget_state(self._total_tokens)
        self._budget_exhausted = self._budget_state != "none"
        recovery_results = await self.execute_tools(
            recovery_plan,
            self._registry,
            state,
        )
        self._observe_workflow_tools(recovery_plan, recovery_results)
        return response

    def _consume_verifier_tokens(self, verifier: Any) -> None:
        raw_tokens = getattr(verifier, "last_call_tokens", 0)
        try:
            tokens = max(0, int(raw_tokens or 0))
        except (TypeError, ValueError):
            tokens = 0
        self._total_tokens += tokens
        self._budget_state = self._result_processor.budget_state(self._total_tokens)
        self._budget_exhausted = self._budget_state != "none"

    async def _call_finding_verifier(
        self,
        verifier: Any,
        candidates: list[Any],
        request: ReviewRequest,
        state: ContextState,
    ) -> FindingVerificationBatch:
        """Pass captured evidence when supported while preserving injected verifiers."""
        verify = verifier.verify
        try:
            parameters = inspect.signature(verify).parameters.values()
            accepts_evidence = any(
                item.name == "tool_evidence"
                or item.kind == inspect.Parameter.VAR_KEYWORD
                for item in parameters
            )
        except (TypeError, ValueError):
            accepts_evidence = False
        if accepts_evidence:
            return FindingVerificationBatch.model_validate(
                await verify(
                    candidates,
                    request,
                    state,
                    tool_evidence=list(self._verifier_tool_evidence),
                )
            )
        return FindingVerificationBatch.model_validate(
            await verify(candidates, request, state)
        )

    def _normalize_verifier_result(
        self,
        verifier: Any,
        candidates: list[Any],
        returned_batch: FindingVerificationBatch,
        request: ReviewRequest,
        state: ContextState,
    ) -> tuple[
        FindingVerificationBatch,
        FindingVerificationBatch,
        DeterministicValidationStats,
    ]:
        """Return raw and post-validation batches without validating twice."""
        if isinstance(verifier, FindingVerifier):
            return (
                verifier.last_raw_batch,
                verifier.last_post_validation_batch,
                verifier.last_validation_stats,
            )
        manifests = [dict(item) for item in state.candidate_context_manifests]
        candidates[:] = bind_candidate_evidence(
            candidates,
            request,
            list(self._verifier_tool_evidence),
            context_manifests=manifests,
        )
        post_batch, stats = validate_verifications_with_stats(
            candidates,
            returned_batch,
            request,
            candidate_context=build_candidate_verifier_context(
                candidates,
                request,
                list(self._verifier_tool_evidence),
                context_manifests=manifests,
                context_mode=state.context_mode,
            ),
        )
        return returned_batch, post_batch, stats

    def _finalize_review_workflow(
        self,
        response: ReviewResponse,
        state: ContextState,
    ) -> ReviewResponse:
        if self._workflow_enforcement == "off":
            return response
        self._complete_workflow_step("finalize_review")
        has_candidates = bool(response.report.issues)
        has_risk = any(
            issue.severity.value in {"critical", "warning"}
            for issue in response.report.issues
        )
        summary = self._review_workflow.summary(
            has_candidates=has_candidates,
            has_risk_candidates=has_risk,
        )
        summary.update(
            {
                "reprompt_count": self._workflow_reprompt_count,
                "enforcement": self._workflow_enforcement,
            }
        )
        raw_missing = summary.get("missing_required_steps", [])
        missing = (
            [str(item) for item in raw_missing] if isinstance(raw_missing, list) else []
        )
        workflow_filtered_issue_count = 0
        workflow_invalid = bool(missing)
        if missing and self._workflow_enforcement == "enforce":
            before_filter_count = len(response.report.issues)
            response.report.issues = [
                issue
                for issue in response.report.issues
                if issue.severity.value not in {"critical", "warning"}
            ]
            workflow_filtered_issue_count = before_filter_count - len(
                response.report.issues
            )
            state.errors.append(
                ErrorDetail(
                    file="",
                    message="Review workflow incomplete: " + ", ".join(missing),
                    category="runtime",
                )
            )
            self._last_decision_reason = "workflow_incomplete"
        response.workflow_invalid = workflow_invalid
        response.workflow_missing_steps = missing
        summary.update(
            {
                "model_raw_issue_count": self._model_raw_issue_count,
                "submitted_issue_count": self._submitted_issue_count,
                "policy_passed_issue_count": self._policy_passed_issue_count,
                "policy_rejected_issue_count": self._policy_rejected_issue_count,
                "non_risk_issue_count": self._non_risk_issue_count,
                "verifier_candidate_count": self._verifier_candidate_count,
                "verifier_accepted_count": self._verifier_accepted_count,
                "verifier_rejected_count": self._verifier_rejected_count,
                "verifier_needs_evidence_count": self._verifier_needs_evidence_count,
                "verifier_downgraded_count": self._verifier_downgraded_count,
                "consolidator_block_count": self._consolidator_block_count,
                "consolidator_proposal_count": self._consolidator_proposal_count,
                "consolidator_accepted_cluster_count": (
                    self._consolidator_accepted_cluster_count
                ),
                "consolidator_rejected_cluster_count": (
                    self._consolidator_rejected_cluster_count
                ),
                "final_root_cause_count": self._final_root_cause_count,
                "finding_inflation_ratio": self._finding_inflation_ratio,
                "workflow_filtered_issue_count": workflow_filtered_issue_count,
                "final_effective_issue_count": len(response.report.issues),
                "workflow_invalid": workflow_invalid,
            }
        )
        self._record_event(EventType.WORKFLOW_SUMMARY, "workflow", summary)
        return response

    def _observe_workflow_tools(
        self,
        plan: AnalysisPlan,
        results: list[ToolResult],
    ) -> None:
        if self._workflow_enforcement == "off":
            return
        successful_names = {
            self._parse_tool_call(raw)["name"]
            for raw, result in zip(plan.tool_calls, results)
            if result.ok
        }
        if successful_names & {
            "read_file",
            "changed_context",
            "get_changed_context",
            "symbol_context",
            "find_symbol_context",
            "grep",
            "glob",
            "list_dir",
        }:
            self._complete_workflow_step("inspect_changed_context")
        if "validate_review_draft" in successful_names:
            self._complete_workflow_step("validate_candidate_draft")

    def _complete_workflow_step(self, step_id: str) -> None:
        state = self._review_workflow.states[step_id]
        if state.status == "completed":
            return
        self._review_workflow.complete(step_id)
        self._record_event(
            EventType.WORKFLOW_STEP_COMPLETED,
            "workflow",
            {"step_id": step_id, "attempts": state.attempts},
        )

    def _skip_workflow_step(self, step_id: str, reason: str) -> None:
        state = self._review_workflow.states[step_id]
        if state.status in {"completed", "skipped"}:
            return
        self._review_workflow.skip(step_id, reason, condition_not_applicable=True)
        self._record_event(
            EventType.WORKFLOW_STEP_SKIPPED,
            "workflow",
            {"step_id": step_id, "reason": reason},
        )

    def _fail_workflow_step(self, step_id: str, reason: str) -> None:
        state = self._review_workflow.states[step_id]
        if state.status == "pending":
            try:
                self._review_workflow.start(step_id)
            except ValueError:
                state.status = "in_progress"
                state.attempts = max(1, state.attempts)
        if state.status == "in_progress":
            self._review_workflow.fail(step_id, reason)
        self._record_event(
            EventType.WORKFLOW_STEP_FAILED,
            "workflow",
            {"step_id": step_id, "reason": reason, "attempts": state.attempts},
        )

    async def run_debug(self, request: DebugRequest) -> DebugResponse:
        """Run debug mode through the orchestrator loop."""
        self._reset_run(
            max_iterations=(
                self._debug_max_iterations_override
                or self._settings.debug_max_iterations
            ),
            repo_path=request.repo_path,
        )
        if self._external_registry is None:
            self._registry = create_default_registry(include_execute=True)
        state = self.prepare_context(request)
        response: ReviewResponse | DebugResponse | None = None
        while True:
            tool_specs = (
                [] if self._permission_mode == "plan" else self._registry.list_specs()
            )
            plan = await self.analyze(state, request, tool_specs)
            self._last_plan = plan
            self._observe_incomplete_plan(plan, state)
            tool_results = await self.execute_tools(plan, self._registry, state)
            response = self.format_result(state, tool_results)
            if not self.should_continue(state, response):
                break
            if self._should_pre_budget_submit(state):
                self._pre_budget_submit_attempted = True
                self._record_pre_budget_submit("attempt", state)
                submit_plan = await self.analyze(
                    state, request, tool_specs=[], force_submit=True
                )
                self._last_plan = submit_plan
                self._observe_incomplete_plan(submit_plan, state)
                response = self.format_result(state, tool_results=[])
                self._record_pre_budget_submit("completed", state, submit_plan)
                break
            self._iteration += 1
        response = await self._maybe_force_submit_debug(state, request, response)
        assert isinstance(response, DebugResponse)
        self._close_event_log()
        return response

    async def _maybe_force_submit_review(
        self,
        state: ContextState,
        request: ReviewRequest,
        response: ReviewResponse | DebugResponse | None,
    ) -> ReviewResponse | DebugResponse:
        """Finalize normal runs or recover a truncated review submission."""
        assert response is not None
        if self._permission_mode == "plan":
            return response
        if not isinstance(response, ReviewResponse):
            return response
        plan = self._last_plan
        if plan is None:
            return response
        if self._has_review_business_output(plan.draft_review):
            return response
        if self._length_recovery_required:
            return await self._recover_review_after_length(state, request, response)
        skip_reason = self._finalize_skip_reason()
        if skip_reason:
            self._record_finalize_skipped(skip_reason)
            return response
        finalize_plan = await self.analyze(
            state, request, tool_specs=[], force_submit=True
        )
        self._last_plan = finalize_plan
        self._observe_incomplete_plan(finalize_plan, state)
        formatted = self.format_result(state, tool_results=[])
        assert isinstance(formatted, ReviewResponse)
        response = formatted
        self._record_event(
            EventType.DECISION,
            "finalize",
            {
                "iteration": self._iteration,
                "finalize_attempt": True,
                "finalize_submit_seen": finalize_plan.draft_review is not None,
                "budget_state": self._budget_state,
                "prior_length_finish_seen": self._model_length_finish_seen,
                "final_submit_evidence_included_count": (
                    finalize_plan.final_submit_evidence_included_count
                ),
                "final_submit_evidence_token_count": (
                    finalize_plan.final_submit_evidence_token_count
                ),
                "final_submit_evidence_truncated_count": (
                    finalize_plan.final_submit_evidence_truncated_count
                ),
            },
        )
        return response

    async def _recover_review_after_length(
        self,
        state: ContextState,
        request: ReviewRequest,
        response: ReviewResponse,
    ) -> ReviewResponse:
        """Run one bounded submit-only recovery after a truncated response."""

        if self._length_recovery_attempted:
            return response
        self._length_recovery_attempted = 1
        self._record_length_recovery_transition("attempted")
        blocker = self._length_recovery_block_reason()
        if blocker:
            self._mark_length_recovery_failed(state, blocker)
            self._record_event(
                EventType.DECISION,
                "finalize",
                {
                    "iteration": self._iteration,
                    "finalize_attempt": False,
                    "finalize_submit_seen": False,
                    "length_recovery": True,
                    "length_recovery_status": "failed",
                    "skip_reason": blocker,
                    "budget_state": self._budget_state,
                },
            )
            return response

        finalize_plan = await self.analyze(
            state,
            request,
            tool_specs=[],
            force_submit=True,
        )
        self._last_plan = finalize_plan
        self._observe_incomplete_plan(finalize_plan, state)
        formatted = self.format_result(state, tool_results=[])
        assert isinstance(formatted, ReviewResponse)
        response = formatted
        recovered = self._has_review_business_output(finalize_plan.draft_review)
        if recovered:
            self._length_recovery_succeeded = 1
            self._record_length_recovery_transition(
                "succeeded",
                submit_response_id=finalize_plan.source_response_id,
            )
        else:
            reason = (
                finalize_plan.incomplete_reason
                or "finalize_only_recovery_produced_no_valid_submit_review"
            )
            self._mark_length_recovery_failed(state, reason)
        self._record_event(
            EventType.DECISION,
            "finalize",
            {
                "iteration": self._iteration,
                "finalize_attempt": True,
                "finalize_submit_seen": finalize_plan.draft_review is not None,
                "length_recovery": True,
                "length_recovery_status": "succeeded" if recovered else "failed",
                "budget_state": self._budget_state,
                "prior_length_finish_seen": True,
                "draft_finding_count": len(self._draft_finding_store),
                "final_submit_evidence_included_count": (
                    finalize_plan.final_submit_evidence_included_count
                ),
                "final_submit_evidence_token_count": (
                    finalize_plan.final_submit_evidence_token_count
                ),
                "final_submit_evidence_truncated_count": (
                    finalize_plan.final_submit_evidence_truncated_count
                ),
            },
        )
        return response

    async def _maybe_force_submit_debug(
        self,
        state: ContextState,
        request: DebugRequest,
        response: ReviewResponse | DebugResponse | None,
    ) -> ReviewResponse | DebugResponse:
        assert response is not None
        if self._permission_mode == "plan":
            return response
        skip_reason = self._finalize_skip_reason()
        if skip_reason:
            self._record_finalize_skipped(skip_reason)
            return response
        if not isinstance(response, DebugResponse):
            return response
        plan = self._last_plan
        if plan is None or plan.draft_debug is not None:
            return response
        finalize_plan = await self.analyze(
            state, request, tool_specs=[], force_submit=True
        )
        self._last_plan = finalize_plan
        self._observe_incomplete_plan(finalize_plan, state)
        response = self.format_result(state, tool_results=[])
        self._record_event(
            EventType.DECISION,
            "finalize",
            {
                "iteration": self._iteration,
                "finalize_attempt": True,
                "finalize_submit_seen": finalize_plan.draft_debug is not None,
                "budget_state": self._budget_state,
                "prior_length_finish_seen": self._model_length_finish_seen,
                "final_submit_evidence_included_count": (
                    finalize_plan.final_submit_evidence_included_count
                ),
                "final_submit_evidence_token_count": (
                    finalize_plan.final_submit_evidence_token_count
                ),
                "final_submit_evidence_truncated_count": (
                    finalize_plan.final_submit_evidence_truncated_count
                ),
            },
        )
        return response

    def prepare_context(self, request: ReviewRequest | DebugRequest) -> ContextState:
        """Create the initial context state for one run."""
        start = perf_counter()
        state = self._context_builder.prepare_context(request)
        if isinstance(request, ReviewRequest) and request.diff_mode:
            state.constraints.append("diff_mode")
        if isinstance(request, DebugRequest) and (
            request.error_log_path or request.error_log_text
        ):
            state.constraints.append("error_log_provided")
        if self._permission_mode == "plan":
            state.constraints.append("plan_mode")
        self._record_event(
            EventType.PHASE_END,
            "prepare",
            {"elapsed_ms": int((perf_counter() - start) * 1000)},
        )
        return state

    async def analyze(
        self,
        state: ContextState,
        request: ReviewRequest | DebugRequest,
        tool_specs: list[ToolSpec],
        *,
        force_submit: bool = False,
    ) -> AnalysisPlan:
        """Run model analysis and return structured plan."""
        start = perf_counter()
        state.decisions.append(
            DecisionStep(
                phase="analyze",
                action="Run model analysis",
                result="Preparing messages and tool schemas",
            )
        )

        diff_text = ""
        error_log_text = ""
        project_structure = self._context_builder.build_project_structure(
            request.repo_path,
            max_depth=self._settings.project_structure_max_depth,
            max_entries=self._settings.project_structure_max_entries,
        )
        file_contents: dict[str, str] = {}
        if isinstance(request, ReviewRequest):
            diff_text = request.diff_text or ""
            if request.diff_mode and not diff_text:
                diff_text = self._context_builder.load_diff(request.repo_path)
            file_contents = self._context_builder.load_diff_file_contents(
                request.repo_path,
                diff_text=diff_text,
                max_files=self._settings.file_context_max_files,
                max_chars_per_file=self._settings.file_context_max_chars_per_file,
                max_chars_total=self._settings.file_context_max_chars_total,
            )
        else:
            error_log_text = self._context_builder.load_error_log(
                request.error_log_path, request.error_log_text
            )

        engine = self._build_engine()
        if engine is None:
            state.errors.append(
                ErrorDetail(
                    file=request.repo_path,
                    message="Model client unavailable; using fallback plan.",
                    category="runtime",
                )
            )
            result = self._fallback_plan(request)
            self._latest_tokens = 0
        else:
            try:
                defer_review_submit = (
                    isinstance(request, ReviewRequest)
                    and not force_submit
                    and self._iteration < self._review_min_tool_iterations
                    and self._permission_mode != "plan"
                )
                if force_submit:
                    serialized_tools = build_submit_tool_schemas()
                else:
                    serialized_tools = build_tool_schemas(tool_specs)
                    if (
                        isinstance(request, ReviewRequest)
                        and self._permission_mode != "plan"
                    ):
                        serialized_tools.append(build_draft_finding_tool_schema())
                    if not defer_review_submit:
                        serialized_tools += build_submit_tool_schemas()
                result, usage = await engine.analyze(
                    state=state,
                    request=request,
                    tool_specs=tool_specs,
                    tool_schemas=serialized_tools,
                    diff_text=diff_text,
                    error_log=error_log_text,
                    project_structure=project_structure,
                    file_contents=file_contents,
                    tool_feedback=self._tool_feedback,
                    feedback_digest_index=self._feedback_digest_index,
                    draft_findings=self._draft_finding_store.all(),
                    prompt_input_token_budget=self._settings.prompt_input_token_budget,
                    iteration=self._iteration,
                    force_submit=force_submit,
                    near_last_iteration=(self._iteration + 1) >= self._max_iterations,
                    defer_submit=defer_review_submit,
                )
                self._latest_tokens = usage.total_tokens
                self._persist_draft_finding_calls(result)
                if result.draft_review is not None:
                    self._submit_review_seen_any = True
                if result.draft_debug is not None:
                    self._submit_debug_seen_any = True
            except ModelClientError as exc:
                if isinstance(exc, ModelTimeoutError):
                    self._model_timeout_seen = True
                state.errors.append(
                    ErrorDetail(
                        file=request.repo_path,
                        message=f"Model analysis failed: {exc}",
                        category="runtime",
                    )
                )
                self._record_event(
                    EventType.ERROR,
                    "analyze",
                    {
                        "iteration": self._iteration,
                        "error_type": exc.__class__.__name__,
                        "code": exc.code or "",
                        "message": str(exc),
                    },
                )
                result = self._fallback_plan(request)
                self._latest_tokens = 0
            except RunJournalError as exc:
                self._model_incomplete_seen = True
                state.errors.append(
                    ErrorDetail(
                        file=str(self._run_journal.path) if self._run_journal else "",
                        message=f"Run journal persistence failed: {exc}",
                        category="runtime",
                    )
                )
                self._record_event(
                    EventType.ERROR,
                    "analyze",
                    {
                        "iteration": self._iteration,
                        "error_type": exc.__class__.__name__,
                        "message": str(exc),
                    },
                )
                result = self._fallback_plan(request)
                self._latest_tokens = 0

        self._record_event(
            EventType.MODEL_CALL,
            "analyze",
            {
                "iteration": self._iteration,
                "needs_tools": result.needs_tools,
                "tool_calls": len(result.tool_calls),
                "elapsed_ms": int((perf_counter() - start) * 1000),
                "tokens": self._latest_tokens,
                "model_request_timeout_seconds": self._settings.model_request_timeout_seconds,
                "model_max_retries": self._settings.model_max_retries,
                "force_submit": force_submit,
                "model_finish_reason": result.model_finish_reason,
                "model_length_finish_seen": (
                    self._model_length_finish_seen
                    or result.model_finish_reason == "length"
                ),
                "final_submit_evidence_included_count": (
                    result.final_submit_evidence_included_count
                ),
                "final_submit_evidence_token_count": (
                    result.final_submit_evidence_token_count
                ),
                "final_submit_evidence_truncated_count": (
                    result.final_submit_evidence_truncated_count
                ),
                "budget_state": self._budget_state,
            },
        )
        return result

    async def execute_tools(
        self,
        plan: AnalysisPlan,
        registry: ToolRegistry,
        state: ContextState,
    ) -> list[ToolResult]:
        """Execute model-planned tools via registry."""
        state.decisions.append(
            DecisionStep(
                phase="execute_tools",
                action="Execute tool plan",
                result=(
                    "Plan mode: tool execution disabled"
                    if self._permission_mode == "plan" and plan.needs_tools
                    else "No tools requested"
                    if not plan.needs_tools
                    else "Executing requested tools"
                ),
            )
        )
        if self._permission_mode == "plan":
            return []
        if not plan.needs_tools:
            return []

        results: list[ToolResult] = []
        executed_feedback: list[dict[str, Any]] = []
        index = 0
        while index < len(plan.tool_calls):
            if self._tool_call_count >= self._settings.agent_max_tool_calls:
                state.errors.append(
                    ErrorDetail(
                        file="",
                        message="Agent tool-call budget exhausted.",
                        category="runtime",
                    )
                )
                self._record_event(
                    EventType.DECISION,
                    "execute_tools",
                    {
                        "iteration": self._iteration,
                        "reason": "tool_budget_exhausted",
                        "tool_budget": self._settings.agent_max_tool_calls,
                    },
                )
                for skipped_call in plan.tool_calls[index:]:
                    skipped_result = ToolResult(
                        ok=False,
                        error="Agent tool-call budget exhausted.",
                        data={
                            "ok": False,
                            "error_type": "tool_budget_exhausted",
                        },
                    )
                    self._journal_tool_result(plan, skipped_call, skipped_result)
                    executed_feedback.append(
                        {"tool_call": skipped_call, "result": skipped_result}
                    )
                break
            raw_call = plan.tool_calls[index]
            call = self._parse_tool_call(raw_call)
            tool_name = call["name"]
            args = call["arguments"]
            tool = registry.get(tool_name)
            if tool is None:
                err = f"Tool not found: {tool_name}"
                structured = {
                    "ok": False,
                    "error_type": "tool_not_found",
                    "message": err,
                    "recommended_next_step": (
                        "Use list_dir on the parent directory first, then retry with a valid tool name/path."
                    ),
                }
                state.errors.append(
                    ErrorDetail(file="", message=err, category="runtime")
                )
                results.append(ToolResult(ok=False, error=err, data=structured))
                self._journal_tool_result(plan, raw_call, results[-1])
                executed_feedback.append(
                    {
                        "tool_call": raw_call,
                        "result": results[-1],
                    }
                )
                index += 1
                continue

            tool_spec = tool.spec()
            if tool_spec.safety in {ToolSafety.WRITE, ToolSafety.EXECUTE}:
                is_allowed = await self._is_high_risk_allowed(tool_spec, args)
                if not is_allowed:
                    err = f"Tool execution requires confirmation: {tool_name}"
                    state.errors.append(
                        ErrorDetail(file="", message=err, category="security")
                    )
                    results.append(ToolResult(ok=False, error=err))
                    self._journal_tool_result(plan, raw_call, results[-1])
                    self._record_event(
                        EventType.ERROR,
                        "execute_tools",
                        {
                            "iteration": self._iteration,
                            "name": tool_name,
                            "category": "security",
                        },
                    )
                    executed_feedback.append(
                        {
                            "tool_call": raw_call,
                            "result": results[-1],
                        }
                    )
                    index += 1
                    continue

                (
                    result,
                    error_detail,
                    elapsed_ms,
                ) = await self._execute_one_tool_and_journal(
                    plan=plan,
                    raw_call=raw_call,
                    tool_name=tool_name,
                    tool=tool,
                    args=args,
                )
                if error_detail is not None:
                    state.errors.append(error_detail)
                results.append(result)
                self._record_event(
                    EventType.TOOL_CALL,
                    "execute_tools",
                    self._build_tool_call_event_payload(
                        name=tool_name,
                        result=result,
                        elapsed_ms=elapsed_ms,
                    ),
                )
                self._trace_recorder.record(
                    self._record_event,
                    EventType.TOOL_IO,
                    "execute_tools",
                    {
                        "iteration": self._iteration,
                        "name": tool_name,
                        "ok": result.ok,
                        "error": result.error or "",
                        "args_digest": self._trace_recorder.build_tool_result_preview(
                            args
                        ).get("digest", {}),
                        "result_preview": self._trace_recorder.build_tool_result_preview(
                            result
                        ),
                    },
                )
                executed_feedback.append(
                    {
                        "tool_call": raw_call,
                        "result": result,
                    }
                )
                index += 1
                continue

            if not tool.is_concurrency_safe():
                (
                    result,
                    error_detail,
                    elapsed_ms,
                ) = await self._execute_one_tool_and_journal(
                    plan=plan,
                    raw_call=raw_call,
                    tool_name=tool_name,
                    tool=tool,
                    args=args,
                )
                if error_detail is not None:
                    state.errors.append(error_detail)
                results.append(result)
                self._record_event(
                    EventType.TOOL_CALL,
                    "execute_tools",
                    self._build_tool_call_event_payload(
                        name=tool_name,
                        result=result,
                        elapsed_ms=elapsed_ms,
                    ),
                )
                self._trace_recorder.record(
                    self._record_event,
                    EventType.TOOL_IO,
                    "execute_tools",
                    {
                        "iteration": self._iteration,
                        "name": tool_name,
                        "ok": result.ok,
                        "error": result.error or "",
                        "args_digest": self._trace_recorder.build_tool_result_preview(
                            args
                        ).get("digest", {}),
                        "result_preview": self._trace_recorder.build_tool_result_preview(
                            result
                        ),
                    },
                )
                executed_feedback.append(
                    {
                        "tool_call": raw_call,
                        "result": result,
                    }
                )
                index += 1
                continue

            batch_calls: list[tuple[dict[str, Any], str, BaseTool, dict[str, Any]]] = [
                (raw_call, tool_name, tool, args)
            ]
            scan = index + 1
            remaining_budget = (
                self._settings.agent_max_tool_calls - self._tool_call_count
            )
            while scan < len(plan.tool_calls) and len(batch_calls) < remaining_budget:
                next_raw = plan.tool_calls[scan]
                next_call = self._parse_tool_call(next_raw)
                next_name = next_call["name"]
                next_args = next_call["arguments"]
                next_tool = registry.get(next_name)
                if next_tool is None:
                    break
                next_spec = next_tool.spec()
                if next_spec.safety in {ToolSafety.WRITE, ToolSafety.EXECUTE}:
                    break
                if not next_tool.is_concurrency_safe():
                    break
                batch_calls.append((next_raw, next_name, next_tool, next_args))
                scan += 1

            batch_results = await asyncio.gather(
                *[
                    self._execute_one_tool_and_journal(
                        plan=plan,
                        raw_call=batch_raw,
                        tool_name=batch_name,
                        tool=batch_tool,
                        args=batch_args,
                    )
                    for (batch_raw, batch_name, batch_tool, batch_args) in batch_calls
                ]
            )
            for (batch_raw, batch_name, _, _), (
                batch_result,
                batch_error,
                elapsed_ms,
            ) in zip(batch_calls, batch_results):
                if batch_error is not None:
                    state.errors.append(batch_error)
                results.append(batch_result)
                self._record_event(
                    EventType.TOOL_CALL,
                    "execute_tools",
                    self._build_tool_call_event_payload(
                        name=batch_name,
                        result=batch_result,
                        elapsed_ms=elapsed_ms,
                    ),
                )
                batch_call = self._parse_tool_call(batch_raw)
                self._trace_recorder.record(
                    self._record_event,
                    EventType.TOOL_IO,
                    "execute_tools",
                    {
                        "iteration": self._iteration,
                        "name": batch_name,
                        "ok": batch_result.ok,
                        "error": batch_result.error or "",
                        "args_digest": self._trace_recorder.build_tool_result_preview(
                            batch_call.get("arguments", {})
                        ).get("digest", {}),
                        "result_preview": self._trace_recorder.build_tool_result_preview(
                            batch_result
                        ),
                    },
                )
                executed_feedback.append(
                    {
                        "tool_call": batch_raw,
                        "result": batch_result,
                    }
                )
            index += len(batch_calls)

        self._append_tool_feedback(executed_feedback)
        return results

    def format_result(
        self,
        state: ContextState,
        tool_results: list[ToolResult],
    ) -> ReviewResponse | DebugResponse:
        """Build final response according to run mode."""
        plan = self._last_plan or AnalysisPlan(needs_tools=False, tool_calls=[])
        self._total_tokens += self._latest_tokens
        self._budget_state = self._result_processor.budget_state(self._total_tokens)
        self._budget_exhausted = self._budget_state != "none"

        response: ReviewResponse | DebugResponse
        blocking_error: bool
        if self._is_review_mode(state):
            response, blocking_error = self._result_processor.format_review(
                plan, tool_results, state
            )
        else:
            response, blocking_error = self._result_processor.format_debug(
                plan, tool_results, state
            )
        self._blocking_error = blocking_error
        response.context = state
        response.run_id = self._run_id or str(uuid4())
        self._record_event(
            EventType.PHASE_END,
            "format",
            {
                "iteration": self._iteration,
                "blocking_error": blocking_error,
                "total_tokens": self._total_tokens,
                "budget_exhausted": self._budget_exhausted,
                "budget_state": self._budget_state,
            },
        )
        self._trace_recorder.record(
            self._record_event,
            EventType.FORMAT_RESULT,
            "format",
            {
                "iteration": self._iteration,
                "blocking_tool_error": blocking_error,
                "draft_review_present": plan.draft_review is not None,
                "draft_debug_present": plan.draft_debug is not None,
                "used_placeholder_summary": (
                    self._is_review_mode(state)
                    and plan.draft_review is None
                    and isinstance(response, ReviewResponse)
                    and response.report.summary
                    == "Review pipeline completed with placeholder summary."
                ),
                "issues_count": (
                    len(response.report.issues)
                    if isinstance(response, ReviewResponse)
                    else len(response.steps)
                ),
            },
        )
        return response

    def should_continue(
        self, state: ContextState, response: ReviewResponse | DebugResponse
    ) -> bool:
        """Decide whether another loop iteration should run."""
        has_pending_tools = (
            False
            if self._permission_mode == "plan"
            else bool(self._last_plan and self._last_plan.needs_tools)
        )
        defer_review_submit = (
            self._is_review_mode(state)
            and self._iteration < self._review_min_tool_iterations
            and not self._blocking_error
            and self._permission_mode != "plan"
        )
        self._model_completed = (
            not has_pending_tools
            and not self._blocking_error
            and not defer_review_submit
            and not self._model_incomplete_seen
        )
        reached_limit = (self._iteration + 1) >= self._max_iterations
        run_timed_out = self._run_timeout_exceeded()

        stop = (
            self._model_incomplete_seen
            or self._model_completed
            or reached_limit
            or self._budget_exhausted
            or run_timed_out
        )
        if self._budget_exhausted:
            state.errors.append(
                ErrorDetail(
                    file="",
                    message="Token budget exhausted; returning partial result.",
                    category="runtime",
                )
            )
        if run_timed_out:
            state.errors.append(
                ErrorDetail(
                    file="",
                    message="Run wall-clock timeout reached; returning partial result.",
                    category="runtime",
                )
            )

        if self._budget_state == "hard_capped":
            reason = "budget_hard_capped"
        elif self._budget_state == "soft_capped":
            reason = "budget_soft_capped"
        elif self._model_incomplete_seen:
            reason = "model_incomplete"
        elif run_timed_out:
            reason = "run_timeout"
        elif reached_limit:
            reason = "max_iterations"
        elif self._model_completed:
            reason = "model_completed"
        else:
            reason = "continue"
        self._last_decision_reason = reason
        state.decisions.append(
            DecisionStep(
                phase="continue",
                action="Evaluate continue conditions",
                result=("continue" if reason == "continue" else f"stop:{reason}"),
            )
        )
        self._record_event(
            EventType.DECISION,
            "continue",
            {
                "iteration": self._iteration,
                "max_iterations": self._max_iterations,
                "has_pending_tools": has_pending_tools,
                "model_completed": self._model_completed,
                "model_incomplete": self._model_incomplete_seen,
                "reached_limit": reached_limit,
                "run_timed_out": run_timed_out,
                "elapsed_ms": int(self._run_elapsed_seconds() * 1000),
                "budget_exhausted": self._budget_exhausted,
                "budget_state": self._budget_state,
                "reason": reason,
                "defer_review_submit": defer_review_submit,
                "submit_review_seen_any": self._submit_review_seen_any,
                "submit_debug_seen_any": self._submit_debug_seen_any,
                "run_id": response.run_id,
            },
        )
        return not stop

    def _reset_run(self, max_iterations: int, repo_path: str) -> None:
        self._run_id = str(uuid4())
        self._workspace_root = Path(repo_path).resolve()
        configured_log_dir = Path(self._settings.event_log_dir)
        if not configured_log_dir.is_absolute():
            configured_log_dir = Path(repo_path) / configured_log_dir
        self._event_log = EventLog(
            run_id=self._run_id,
            log_dir=configured_log_dir,
        )
        self._run_journal = RunJournal(
            run_id=self._run_id,
            path=(
                self._workspace_root
                / ".mergewarden"
                / "runs"
                / self._run_id
                / "journal.jsonl"
            ),
        )
        self._last_plan = None
        self._draft_finding_store = DraftFindingStore()
        self._tool_feedback = []
        self._feedback_digest_index = {}
        self._tool_dedup_cache = {}
        self._submit_review_seen_any = False
        self._submit_debug_seen_any = False
        self._latest_tokens = 0
        self._model_conversation = ModelConversation()
        self._total_tokens = 0
        self._iteration = 0
        self._max_iterations = max_iterations
        self._blocking_error = False
        self._budget_exhausted = False
        self._budget_state = "none"
        self._model_completed = False
        self._last_decision_reason = ""
        self._run_started_at = perf_counter()
        self._run_timeout_seconds = self._settings.agent_run_timeout_seconds
        self._model_timeout_seen = False
        self._model_incomplete_seen = False
        self._model_length_finish_seen = False
        self._length_recovery_required = False
        self._length_recovery_attempted = 0
        self._length_recovery_succeeded = 0
        self._length_recovery_failed = 0
        self._length_recovery_source_response_ids = []
        self._final_submit_evidence_included_count = 0
        self._final_submit_evidence_token_count = 0
        self._final_submit_evidence_truncated_count = 0
        self._pre_budget_submit_attempted = False
        self._review_workflow = ReviewWorkflowTracker()
        self._workflow_reprompt_count = 0
        self._model_raw_issue_count = 0
        self._submitted_issue_count = 0
        self._policy_passed_issue_count = 0
        self._policy_rejected_issue_count = 0
        self._non_risk_issue_count = 0
        self._verifier_candidate_count = 0
        self._risk_candidate_count = 0
        self._filter_rescue_candidate_count = 0
        self._severity_calibration_candidate_count = 0
        self._semantic_rejected_count = 0
        self._deterministic_rejected_count = 0
        self._verifier_accepted_count = 0
        self._verifier_rejected_count = 0
        self._verifier_needs_evidence_count = 0
        self._verifier_downgraded_count = 0
        self._high_confidence_info_issue_count = 0
        self._severity_reviewed_count = 0
        self._severity_promoted_count = 0
        self._consolidator_block_count = 0
        self._consolidator_proposal_count = 0
        self._consolidator_accepted_cluster_count = 0
        self._consolidator_rejected_cluster_count = 0
        self._final_root_cause_count = 0
        self._finding_inflation_ratio = 0.0
        self._verifier_tool_evidence = []
        self._tool_call_count = 0
        self._tool_name_counts = {}
        self._reviewer_latency_seconds = 0.0
        self._verifier_latency_seconds = 0.0
        self._consolidation_latency_seconds = 0.0
        self._model_response_journal_writes = 0
        self._tool_result_journal_writes = 0
        self._draft_findings_created = 0
        self._draft_findings_from_visible_content = 0
        self._record_event(
            EventType.PHASE_START,
            "prepare",
            {
                "run_id": self._run_id,
                "token_budget": self._settings.token_budget,
                "token_hard_budget": self._settings.token_hard_budget,
                "analysis_token_ceiling": self._analysis_token_ceiling(),
                "final_submit_reserve_tokens": self._settings.final_submit_reserve_tokens,
                "final_submit_prompt_token_budget": (
                    self._settings.final_submit_prompt_token_budget
                ),
                "final_submit_feedback_token_budget": (
                    self._settings.final_submit_feedback_token_budget
                ),
                "prompt_input_token_budget": self._settings.prompt_input_token_budget,
                "model_request_timeout_seconds": self._settings.model_request_timeout_seconds,
                "agent_run_timeout_seconds": self._settings.agent_run_timeout_seconds,
                "agent_tool_timeout_seconds": self._settings.agent_tool_timeout_seconds,
                "agent_max_tool_calls": self._settings.agent_max_tool_calls,
                "model_max_tokens": self._settings.model_max_tokens,
                "pre_budget_submit_token_ratio": self._settings.pre_budget_submit_token_ratio,
                "review_diff_first_changed_files": self._review_diff_first_changed_files,
                "review_diff_first_changed_files_max": self._settings.review_diff_first_changed_files_max,
                "root_cause_consolidation_enabled": (
                    self._settings.root_cause_consolidation_enabled
                ),
                # Report the effective runtime mode, not the deprecated compatibility
                # setting.  Agent-search runs must never look Graph-enabled in audit logs.
                "relation_graph_enabled": self._context_mode == "graph_hybrid",
                "context_mode": self._context_mode,
                "relation_graph_persistence_enabled": (
                    self._settings.relation_graph_persistence_enabled
                ),
            },
        )

    def _run_elapsed_seconds(self) -> float:
        if self._run_started_at <= 0:
            return 0.0
        return perf_counter() - self._run_started_at

    def _run_timeout_exceeded(self) -> bool:
        return self._run_elapsed_seconds() >= self._run_timeout_seconds

    def _has_useful_tool_feedback(self) -> bool:
        """Return True when at least one tool call returned usable data."""
        if not self._tool_feedback:
            return False
        return any(
            isinstance(item.get("result"), ToolResult) and item["result"].ok
            for item in self._tool_feedback
        )

    def _should_pre_budget_submit(self, state: ContextState) -> bool:
        """Decide whether to trigger a bounded submit-only call before the next analysis turn."""
        if self._permission_mode == "plan":
            return False
        if self._pre_budget_submit_attempted:
            return False
        if self._budget_state == "hard_capped":
            return False
        if self._model_timeout_seen:
            return False
        plan = self._last_plan
        if plan is not None and (
            self._has_review_business_output(plan.draft_review)
            or plan.draft_debug is not None
        ):
            return False
        analysis_ceiling = self._analysis_token_ceiling()
        if self._total_tokens >= analysis_ceiling:
            return True
        if not self._has_useful_tool_feedback():
            return False
        ratio = self._settings.pre_budget_submit_token_ratio
        threshold = min(
            int(self._settings.token_budget * ratio),
            analysis_ceiling,
        )
        return self._total_tokens >= threshold

    def _analysis_token_ceiling(self) -> int:
        """Return the cumulative-token ceiling available to non-final model calls."""

        reserve_ceiling = max(
            0,
            self._settings.token_hard_budget
            - self._settings.final_submit_reserve_tokens,
        )
        return min(self._settings.token_budget, reserve_ceiling)

    def _record_pre_budget_submit(
        self,
        stage: str,
        state: ContextState,
        plan: AnalysisPlan | None = None,
    ) -> None:
        """Log a pre-budget-submit decision and its outcome."""
        payload: dict[str, Any] = {
            "iteration": self._iteration,
            "stage": stage,
            "total_tokens": self._total_tokens,
            "token_budget": self._settings.token_budget,
            "token_hard_budget": self._settings.token_hard_budget,
            "analysis_token_ceiling": self._analysis_token_ceiling(),
            "final_submit_reserve_tokens": self._settings.final_submit_reserve_tokens,
            "final_submit_prompt_token_budget": (
                self._settings.final_submit_prompt_token_budget
            ),
            "final_submit_feedback_token_budget": (
                self._settings.final_submit_feedback_token_budget
            ),
            "budget_state": self._budget_state,
            "has_tool_feedback": bool(self._tool_feedback),
            "prior_length_finish_seen": self._model_length_finish_seen,
            "pre_budget_submit_ratio": self._settings.pre_budget_submit_token_ratio,
            "pre_budget_submit_threshold": min(
                int(
                    self._settings.token_budget
                    * self._settings.pre_budget_submit_token_ratio
                ),
                self._analysis_token_ceiling(),
            ),
        }
        if plan is not None:
            payload["draft_present"] = (
                plan.draft_review is not None or plan.draft_debug is not None
            )
            payload["model_finish_reason"] = plan.model_finish_reason
            payload["final_submit_evidence_included_count"] = (
                plan.final_submit_evidence_included_count
            )
            payload["final_submit_evidence_token_count"] = (
                plan.final_submit_evidence_token_count
            )
            payload["final_submit_evidence_truncated_count"] = (
                plan.final_submit_evidence_truncated_count
            )
        self._record_event(EventType.DECISION, "pre_budget_submit", payload)

    def _finalize_skip_reason(self) -> str:
        if self._model_timeout_seen:
            return "model_timeout"
        if self._model_incomplete_seen:
            return "model_incomplete"
        if self._pre_budget_submit_attempted:
            return "pre_budget_submit_attempted"
        if self._budget_state == "hard_capped":
            return "budget_hard_capped"
        if self._run_timeout_exceeded():
            return "run_timeout"
        return ""

    def _length_recovery_block_reason(self) -> str:
        """Return a hard runtime reason that prevents a recovery model call."""

        if self._model_timeout_seen:
            return "model_timeout"
        if self._budget_state == "hard_capped":
            return "budget_hard_capped"
        if self._run_timeout_exceeded():
            return "run_timeout"
        return ""

    @staticmethod
    def _has_review_business_output(report: ReviewReport | None) -> bool:
        if report is None:
            return False
        return bool(report.summary.strip() or report.issues)

    def _record_finalize_skipped(self, skip_reason: str) -> None:
        self._record_event(
            EventType.DECISION,
            "finalize",
            {
                "iteration": self._iteration,
                "finalize_attempt": False,
                "skip_reason": skip_reason,
                "budget_state": self._budget_state,
                "run_timed_out": self._run_timeout_exceeded(),
                "elapsed_ms": int(self._run_elapsed_seconds() * 1000),
            },
        )

    def _record_length_recovery_transition(
        self,
        status: Literal["required", "attempted", "succeeded", "failed"],
        *,
        reason: str = "",
        submit_response_id: str = "",
    ) -> None:
        """Persist and observe one length-recovery state transition."""

        draft_ids = [draft.id for draft in self._draft_finding_store.all()]
        payload = LengthRecoveryJournalPayload(
            status=status,
            source_response_ids=list(self._length_recovery_source_response_ids),
            draft_finding_ids=draft_ids,
            submit_response_id=submit_response_id,
            reason=reason,
        )
        if self._run_journal is not None:
            self._run_journal.append(
                PendingRunJournalEntry(
                    type="length_recovery",
                    payload=payload.model_dump(mode="json"),
                )
            )
        self._record_event(
            EventType.DECISION,
            "length_recovery",
            {
                "iteration": self._iteration,
                **payload.model_dump(mode="json"),
            },
        )

    def _mark_length_recovery_failed(
        self,
        state: ContextState,
        reason: str,
    ) -> None:
        """Mark recovery terminally failed and expose a blocking runtime error."""

        if self._length_recovery_failed:
            return
        self._length_recovery_failed = 1
        self._record_length_recovery_transition("failed", reason=reason)
        state.errors.append(
            ErrorDetail(
                file="",
                message=(
                    "Model response incomplete: finish_reason=length recovery failed "
                    f"without a valid submit_review ({reason})."
                ),
                category="runtime",
            )
        )

    def _observe_incomplete_plan(
        self,
        plan: AnalysisPlan,
        state: ContextState,
    ) -> None:
        if plan.model_finish_reason == "length":
            self._model_length_finish_seen = True
        self._final_submit_evidence_included_count = max(
            self._final_submit_evidence_included_count,
            plan.final_submit_evidence_included_count,
        )
        self._final_submit_evidence_token_count = max(
            self._final_submit_evidence_token_count,
            plan.final_submit_evidence_token_count,
        )
        self._final_submit_evidence_truncated_count = max(
            self._final_submit_evidence_truncated_count,
            plan.final_submit_evidence_truncated_count,
        )
        reason = str(plan.incomplete_reason or "").strip()
        if not plan.recovery_required or not reason:
            return
        if not self._is_review_mode(state):
            if self._model_incomplete_seen:
                return
            self._model_incomplete_seen = True
            message = (
                "Model response incomplete: finish_reason=length produced no valid "
                f"submit_debug ({reason})."
            )
            state.errors.append(
                ErrorDetail(file="", message=message, category="runtime")
            )
            self._record_event(
                EventType.ERROR,
                "analyze",
                {
                    "iteration": self._iteration,
                    "reason": reason,
                    "message": message,
                },
            )
            return
        if (
            plan.source_response_id
            and plan.source_response_id not in self._length_recovery_source_response_ids
        ):
            self._length_recovery_source_response_ids.append(plan.source_response_id)
        first_incomplete = not self._model_incomplete_seen
        self._model_incomplete_seen = True
        if not self._length_recovery_required:
            self._length_recovery_required = True
            self._record_length_recovery_transition("required", reason=reason)
        if not first_incomplete:
            return
        message = (
            "Model response incomplete: finish_reason=length produced no valid "
            f"submit_review; finalize-only recovery is required ({reason})."
        )
        self._record_event(
            EventType.ERROR,
            "analyze",
            {
                "iteration": self._iteration,
                "reason": reason,
                "message": message,
            },
        )

    async def _maybe_prefetch_review_changed_files(
        self,
        state: ContextState,
        request: ReviewRequest,
        *,
        force: bool = False,
        trigger: str = "initial",
    ) -> None:
        if not force and not self._review_diff_first_changed_files:
            return
        if self._permission_mode == "plan" or not request.diff_mode:
            return
        diff_text = request.diff_text or ""
        selected_files = self._select_changed_files_for_prefetch(diff_text)
        self._record_event(
            EventType.DECISION,
            "diff_first_prefetch",
            {
                "iteration": self._iteration,
                "enabled": True,
                "forced": force,
                "trigger": trigger,
                "selected_files": selected_files,
                "max_files": self._settings.review_diff_first_changed_files_max,
            },
        )
        if not selected_files:
            return
        plan = AnalysisPlan(
            needs_tools=True,
            tool_calls=[
                {
                    "id": f"prefetch-read-file-{index}",
                    "type": "function",
                    "synthetic_context": True,
                    "function": {
                        "name": "read_file",
                        "arguments": _json.dumps(
                            self._prefetch_read_args(path, diff_text),
                            ensure_ascii=True,
                        ),
                    },
                }
                for index, path in enumerate(selected_files)
            ],
        )
        results = await self.execute_tools(plan, self._registry, state)
        self._observe_workflow_tools(plan, results)

    def _select_changed_files_for_prefetch(self, diff_text: str) -> list[str]:
        selected: list[str] = []
        seen: set[str] = set()
        for path in self._context_builder._extract_diff_paths(diff_text):
            if path in seen:
                continue
            seen.add(path)
            selected.append(path)
            if len(selected) >= self._settings.review_diff_first_changed_files_max:
                break
        return selected

    @staticmethod
    def _prefetch_read_args(path: str, diff_text: str) -> dict[str, Any]:
        changed_lines = AgentOrchestrator._changed_new_lines_for_file(diff_text).get(
            path
        )
        if not changed_lines:
            return {"file_path": path, "offset": 0, "limit": 80}
        start_line = max(1, min(changed_lines) - 40)
        return {"file_path": path, "offset": start_line - 1, "limit": 80}

    @staticmethod
    def _changed_new_lines_for_file(diff_text: str) -> dict[str, set[int]]:
        return changed_new_lines_by_file(diff_text)

    def _build_review_tool_context(
        self, request: ReviewRequest
    ) -> ReviewToolContext | None:
        diff_text = request.diff_text or ""
        if request.diff_mode and not diff_text:
            diff_text = self._context_builder.load_diff(request.repo_path)
        if not diff_text.strip():
            return None
        return ReviewToolContext.from_diff(request.repo_path, diff_text)

    async def _execute_one_tool(
        self,
        *,
        tool_name: str,
        tool: BaseTool,
        args: dict[str, Any],
    ) -> tuple[ToolResult, ErrorDetail | None, int]:
        started = perf_counter()
        dedup_key = None
        if tool.spec().safety == ToolSafety.READONLY:
            dedup_key = self._tool_dedup_key(tool_name, args)
            cached = self._tool_dedup_cache.get(dedup_key)
            if cached is not None:
                hint = {
                    "ok": True,
                    "dedup_hit": True,
                    "message": (
                        f"Tool '{tool_name}' already executed earlier in this run with identical "
                        f"arguments; reuse the prior result from tool_feedback. "
                        f"Do not re-request the same read; synthesize now."
                    ),
                }
                self._record_event(
                    EventType.TOOL_CALL,
                    "execute_tools",
                    {
                        "iteration": self._iteration,
                        "name": tool_name,
                        "ok": True,
                        "dedup_hit": True,
                        "elapsed_ms": 0,
                    },
                )
                return ToolResult(ok=True, data=hint), None, 0
        with tool_workspace_root(self._workspace_root):
            try:
                data = await asyncio.wait_for(
                    tool.execute(**args),
                    timeout=self._settings.agent_tool_timeout_seconds,
                )
                result = ToolResult(ok=True, data=data)
                if dedup_key is not None:
                    self._tool_dedup_cache[dedup_key] = result
                return result, None, int((perf_counter() - started) * 1000)
            except TimeoutError:
                timeout = self._settings.agent_tool_timeout_seconds
                elapsed_ms = max(1, int((perf_counter() - started) * 1000))
                err = f"Tool execution timed out for {tool_name} after {timeout:g}s"
                data = {
                    "ok": False,
                    "error_type": "ToolTimeoutError",
                    "tool_name": tool_name,
                    "timeout_seconds": timeout,
                    "skip_reason": "tool_timeout",
                    "message": err,
                }
                return (
                    ToolResult(ok=False, error=err, data=data),
                    ErrorDetail(file="", message=err, category="runtime"),
                    elapsed_ms,
                )
            except ToolError as exc:
                err = f"Tool execution failed for {tool_name}: {exc}"
                hint = self._tool_error_hint(tool_name=tool_name, message=str(exc))
                return (
                    ToolResult(ok=False, error=err, data=hint),
                    ErrorDetail(file=exc.path, message=err, category="runtime"),
                    int((perf_counter() - started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                err = f"Tool execution failed for {tool_name}: {exc}"
                return (
                    ToolResult(ok=False, error=err),
                    ErrorDetail(file="", message=err, category="runtime"),
                    int((perf_counter() - started) * 1000),
                )

    async def _execute_one_tool_and_journal(
        self,
        *,
        plan: AnalysisPlan,
        raw_call: dict[str, Any],
        tool_name: str,
        tool: BaseTool,
        args: dict[str, Any],
    ) -> tuple[ToolResult, ErrorDetail | None, int]:
        """Execute one model-requested tool and persist its result immediately."""

        result, error_detail, elapsed_ms = await self._execute_one_tool(
            tool_name=tool_name,
            tool=tool,
            args=args,
        )
        self._journal_tool_result(plan, raw_call, result)
        return result, error_detail, elapsed_ms

    def _build_tool_call_event_payload(
        self,
        *,
        name: str,
        result: ToolResult,
        elapsed_ms: int,
    ) -> dict[str, Any]:
        self._tool_call_count += 1
        self._tool_name_counts[name] = self._tool_name_counts.get(name, 0) + 1
        payload: dict[str, Any] = {
            "iteration": self._iteration,
            "name": name,
            "ok": result.ok,
            "elapsed_ms": elapsed_ms,
        }
        if isinstance(result.data, dict):
            skip_reason = str(result.data.get("skip_reason", "")).strip()
            if skip_reason:
                payload["skip_reason"] = skip_reason
            error_type = str(result.data.get("error_type", "")).strip()
            if error_type:
                payload["error_type"] = error_type
        return payload

    def _record_review_telemetry(self, state: ContextState) -> None:
        """Emit one complete, mode-aware review telemetry envelope."""

        graph = dict(state.relation_graph_summary)
        payload: dict[str, Any] = {
            "context_mode": state.context_mode,
            "model": self._settings.model_name,
            "review_iterations": self._iteration + 1,
            "tool_call_count": self._tool_call_count,
            "model_response_journal_writes": self._model_response_journal_writes,
            "tool_result_journal_writes": self._tool_result_journal_writes,
            "draft_findings_created": self._draft_findings_created,
            "draft_findings_from_visible_content": (
                self._draft_findings_from_visible_content
            ),
            "length_recoveries_attempted": self._length_recovery_attempted,
            "length_recoveries_succeeded": self._length_recovery_succeeded,
            "length_recoveries_failed": self._length_recovery_failed,
            "submit_review_seen_any": self._submit_review_seen_any,
            "budget_exhausted": self._budget_exhausted,
            "budget_state": self._budget_state,
            "grep_calls": self._tool_name_counts.get("grep_files", 0),
            "read_file_calls": self._tool_name_counts.get("read_file", 0),
            "symbol_lookup_calls": sum(
                self._tool_name_counts.get(name, 0)
                for name in ("find_symbol_context", "symbol_context")
            ),
            "reviewer_latency_seconds": self._reviewer_latency_seconds,
            "verifier_latency_seconds": self._verifier_latency_seconds,
            "consolidation_latency_seconds": self._consolidation_latency_seconds,
            "end_to_end_latency_seconds": self._run_elapsed_seconds(),
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": self._total_tokens,
            "candidate_finding_count": self._verifier_candidate_count,
            "accepted_finding_count": self._verifier_accepted_count,
            "verifier_rejection_count": self._verifier_rejected_count,
            "final_root_cause_finding_count": self._final_root_cause_count,
            "graph_status": graph.get("graph_status", graph.get("status")),
            "graph_cache_mode": graph.get("graph_cache_mode", "not_applicable"),
            "graph_build_latency_seconds": graph.get("build_latency_seconds"),
            "incremental_update_latency_seconds": graph.get(
                "incremental_update_latency_seconds"
            ),
            "parsed_file_count": graph.get("parsed_file_count"),
            "graph_node_count": graph.get("node_count"),
            "graph_edge_count": graph.get("edge_count"),
            "manifest_count": graph.get("manifest_count", 0),
            "manifest_token_cost": graph.get(
                "manifest_token_cost", graph.get("context_token_cost", 0)
            ),
            "cache_hit": graph.get("cache_hit"),
            "cache_hit_rate": graph.get("cache_hit_rate"),
            "fallback_reason": graph.get("fallback_reason", ""),
        }
        self._record_event(EventType.PHASE_END, "review_complete", payload)

    def _record_finding_funnel(self, response: ReviewResponse) -> None:
        """Emit one mutually inspectable finding funnel after all output gates."""

        calibration_rescue = (
            self._filter_rescue_candidate_count
            + self._severity_calibration_candidate_count
        )
        payload = {
            "submitted_finding_count": self._submitted_issue_count,
            "no_finding_run_count": int(self._submitted_issue_count == 0),
            "non_risk_not_routed_count": max(
                0,
                self._non_risk_issue_count - self._severity_calibration_candidate_count,
            ),
            "pre_verifier_rejected_count": max(
                0,
                self._policy_rejected_issue_count - self._filter_rescue_candidate_count,
            ),
            "risk_candidate_count": self._risk_candidate_count,
            "filter_rescue_candidate_count": self._filter_rescue_candidate_count,
            "severity_calibration_candidate_count": (
                self._severity_calibration_candidate_count
            ),
            "calibration_rescue_candidate_count": calibration_rescue,
            "semantic_rejected_count": self._semantic_rejected_count,
            "deterministic_rejected_count": self._deterministic_rejected_count,
            "final_risk_finding_count": sum(
                issue.severity.value in {"critical", "warning"}
                for issue in response.report.issues
            ),
            "final_effective_issue_count": len(response.report.issues),
            "mode": self._finding_verifier_mode,
        }
        self._record_event(
            EventType.FINDING_FUNNEL_COMPLETED,
            "finding_funnel",
            payload,
        )

    @staticmethod
    def _tool_error_hint(*, tool_name: str, message: str) -> dict[str, Any]:
        lower = message.lower()
        recommendation = "Inspect arguments and retry."
        if "not found" in lower or "outside the allowed workspace" in lower:
            recommendation = "Run list_dir on the parent directory to verify paths, then retry with a workspace-relative path."
        elif "not a directory" in lower:
            recommendation = "Validate parent directory with list_dir before calling glob_files/grep_files."
        elif "invalid glob pattern" in lower or "invalid regex pattern" in lower:
            recommendation = "Fix the search pattern syntax and retry."
        return {
            "ok": False,
            "error_type": "tool_execution_failed",
            "tool_name": tool_name,
            "message": message,
            "recommended_next_step": recommendation,
        }

    @staticmethod
    def _tool_dedup_key(tool_name: str, args: dict[str, Any]) -> str:
        try:
            serialized = _json.dumps(
                args, ensure_ascii=True, sort_keys=True, default=str
            )
        except Exception:  # noqa: BLE001
            serialized = str(args)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"{tool_name}:{digest}"

    def _append_tool_feedback(self, entries: list[dict[str, Any]]) -> None:
        """Append feedback entries with iteration metadata, maintain ring-buffer window
        and digest index for folded-summary injection."""
        self._verifier_tool_evidence.extend(
            capture_verifier_tool_evidence(entries, self._workspace_root)
        )
        window = max(1, self._settings.feedback_window_iterations)
        for entry in entries:
            tool_call = entry.get("tool_call", {})
            result = entry.get("result")
            enriched = {
                "iteration": self._iteration,
                "tool_call": tool_call,
                "result": result,
            }
            self._tool_feedback.append(enriched)
            digest = self._compute_feedback_digest(tool_call)
            if digest:
                self._feedback_digest_index[digest] = self._build_digest_record(
                    iteration=self._iteration,
                    tool_call=tool_call,
                    result=result,
                )
        if not self._tool_feedback:
            return
        max_iter = self._tool_feedback[-1].get("iteration", self._iteration)
        min_keep = max_iter - window + 1
        self._tool_feedback = [
            item for item in self._tool_feedback if item.get("iteration", 0) >= min_keep
        ]

    @staticmethod
    def _compute_feedback_digest(tool_call: dict[str, Any]) -> str:
        function_block = (
            tool_call.get("function") if isinstance(tool_call, dict) else None
        )
        if not isinstance(function_block, dict):
            return ""
        name = str(function_block.get("name", "")).strip()
        args = function_block.get("arguments", "{}")
        if isinstance(args, str):
            try:
                parsed = _json.loads(args)
            except Exception:  # noqa: BLE001
                parsed = {"raw": args}
        else:
            parsed = args
        try:
            serialized = _json.dumps(
                parsed, ensure_ascii=True, sort_keys=True, default=str
            )
        except Exception:  # noqa: BLE001
            serialized = str(parsed)
        return f"{name}:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _build_digest_record(
        *,
        iteration: int,
        tool_call: dict[str, Any],
        result: Any,
    ) -> dict[str, Any]:
        function_block = (
            tool_call.get("function") if isinstance(tool_call, dict) else {}
        )
        name = ""
        args_preview: Any = ""
        if isinstance(function_block, dict):
            name = str(function_block.get("name", "")).strip()
            args_raw = function_block.get("arguments", "{}")
            if isinstance(args_raw, str):
                args_preview = args_raw[:200]
            else:
                try:
                    args_preview = _json.dumps(args_raw, ensure_ascii=True)[:200]
                except Exception:  # noqa: BLE001
                    args_preview = str(args_raw)[:200]
        ok = True
        result_preview = ""
        if hasattr(result, "model_dump"):
            payload = result.model_dump()
            ok = bool(payload.get("ok", False))
            try:
                result_preview = _json.dumps(payload, ensure_ascii=True)[:400]
            except Exception:  # noqa: BLE001
                result_preview = str(payload)[:400]
        elif isinstance(result, dict):
            ok = bool(result.get("ok", False))
            try:
                result_preview = _json.dumps(result, ensure_ascii=True)[:400]
            except Exception:  # noqa: BLE001
                result_preview = str(result)[:400]
        return {
            "iteration": iteration,
            "name": name,
            "args_preview": args_preview,
            "ok": ok,
            "result_preview": result_preview,
        }

    async def _is_high_risk_allowed(
        self,
        tool_spec: ToolSpec,
        arguments: dict[str, Any],
    ) -> bool:
        if os.getenv("CI", "").strip().lower() in {"1", "true", "yes"}:
            return False
        if self._confirm_high_risk is None:
            return False
        decision = self._confirm_high_risk(tool_spec, arguments)
        if inspect.isawaitable(decision):
            return bool(await decision)
        return bool(decision)

    def _build_engine(self) -> InferenceEngine | None:
        if self._model_client is not None:
            return InferenceEngine(
                self._model_client,
                trace_recorder=self._trace_recorder,
                trace_event_writer=self._record_event,
                model_response_writer=self._journal_model_response,
                conversation=self._model_conversation,
            )
        try:
            self._model_client = ModelClient(temperature=self._temperature)
            return InferenceEngine(
                self._model_client,
                trace_recorder=self._trace_recorder,
                trace_event_writer=self._record_event,
                model_response_writer=self._journal_model_response,
                conversation=self._model_conversation,
            )
        except Exception:  # noqa: BLE001
            return None

    def _journal_model_response(
        self,
        response: ModelResponse,
        iteration: int,
    ) -> str:
        """Persist visible provider output before any response parsing occurs."""

        if self._run_journal is None:
            return ""
        payload = ModelResponseJournalPayload(
            iteration=iteration,
            model=response.model,
            finish_reason=response.finish_reason,
            content=response.content,
            tool_calls=response.tool_calls,
            usage=response.usage,
        )
        entry = self._run_journal.append(
            PendingRunJournalEntry(
                type="model_response",
                payload=payload.model_dump(mode="json"),
            )
        )
        self._model_response_journal_writes += 1
        return entry.id

    def _persist_draft_finding_calls(self, plan: AnalysisPlan) -> None:
        """Journal runtime-bound drafts before making them available in memory."""

        for draft_input in plan.draft_finding_calls:
            draft = self._persist_one_draft_finding(
                draft_input,
                source_response_id=(
                    plan.draft_finding_source_response_id or plan.source_response_id
                ),
                origin="pseudo_tool",
            )
            self._model_conversation.add_tool_result_for_name(
                "record_draft_finding",
                {
                    "ok": True,
                    "recorded": True,
                    "draft_id": draft.id,
                },
            )
        if (
            not plan.recovery_required
            or len(self._draft_finding_store) > 0
            or not plan.source_response_id
        ):
            return
        visible_content = self._journaled_model_response_content(
            plan.source_response_id
        )
        extracted = extract_visible_draft_finding(visible_content)
        if extracted is None:
            return
        self._persist_one_draft_finding(
            extracted,
            source_response_id=plan.source_response_id,
            origin="visible_content_recovery",
        )
        self._draft_findings_from_visible_content += 1

    def _persist_one_draft_finding(
        self,
        draft_input: DraftFindingInput,
        *,
        source_response_id: str,
        origin: str,
    ) -> DraftFinding:
        """Persist one trusted draft, then expose it through the in-memory store."""

        if self._run_journal is None:
            raise RunJournalError(
                "Draft finding cannot be persisted without a run journal"
            )
        if not source_response_id:
            raise RunJournalError(
                "Draft finding cannot be bound without a source model-response id"
            )
        draft = self._draft_finding_store.bind(
            draft_input,
            source_response_id=source_response_id,
        )
        self._run_journal.append(
            PendingRunJournalEntry(
                type="draft_finding",
                payload=draft.model_dump(mode="json"),
            )
        )
        self._draft_finding_store.add(draft)
        self._draft_findings_created += 1
        self._record_event(
            EventType.DECISION,
            "draft_finding",
            {
                "iteration": self._iteration,
                "draft_id": draft.id,
                "source_response_id": draft.source_response_id,
                "file": draft.file,
                "line": draft.line,
                "symbol": draft.symbol,
                "origin": origin,
            },
        )
        return draft

    def _journaled_model_response_content(self, response_id: str) -> str:
        """Read visible content for one response from the durable journal."""

        if self._run_journal is None:
            return ""
        for entry in reversed(self._run_journal.replay()):
            if entry.id == response_id and entry.type == "model_response":
                return str(entry.payload.get("content", ""))
        return ""

    def _journal_tool_result(
        self,
        plan: AnalysisPlan,
        raw_call: dict[str, Any],
        result: ToolResult,
    ) -> None:
        """Persist a full structured ToolResult bound to its source response."""

        function_block = raw_call.get("function") if isinstance(raw_call, dict) else {}
        if not isinstance(function_block, dict):
            function_block = {}
        tool_name = str(function_block.get("name", "")).strip() or "unknown"
        raw_arguments = function_block.get("arguments", {})
        if isinstance(raw_arguments, str):
            try:
                parsed_arguments = _json.loads(raw_arguments, strict=False)
            except (TypeError, ValueError):
                parsed_arguments = {"raw": raw_arguments}
        else:
            parsed_arguments = raw_arguments
        if not isinstance(parsed_arguments, dict):
            parsed_arguments = {"value": parsed_arguments}
        redacted_arguments = redact_sensitive_values(parsed_arguments)
        redacted_result = redact_sensitive_values(result.model_dump(mode="json"))
        call_id = str(raw_call.get("id", "")).strip()
        if not call_id:
            signature = _json.dumps(
                function_block,
                ensure_ascii=True,
                sort_keys=True,
                default=str,
            )
            call_id = (
                f"runtime_{plan.source_response_id}_"
                f"{hashlib.sha256(signature.encode('utf-8')).hexdigest()[:12]}"
            )
        if str(raw_call.get("id", "")).strip():
            self._model_conversation.add_tool_result(call_id, result)
        else:
            self._model_conversation.add_tool_result_for_name(tool_name, result)
        if self._run_journal is None or not plan.source_response_id:
            return
        payload = ToolResultJournalPayload(
            source_response_id=plan.source_response_id,
            tool_call_id=call_id,
            tool=tool_name,
            arguments=redacted_arguments,
            result=redacted_result,
        )
        self._run_journal.append(
            PendingRunJournalEntry(
                type="tool_result",
                payload=payload.model_dump(mode="json"),
            )
        )
        self._tool_result_journal_writes += 1

    @staticmethod
    def _parse_tool_call(raw_call: dict[str, Any]) -> dict[str, Any]:
        function_block = raw_call.get("function") if isinstance(raw_call, dict) else {}
        if not isinstance(function_block, dict):
            return {"name": "", "arguments": {}}
        name = str(function_block.get("name", "")).strip()
        arguments = function_block.get("arguments", "{}")
        if isinstance(arguments, str):
            try:
                import json

                parsed = json.loads(arguments)
            except Exception:  # noqa: BLE001
                parsed = {}
        elif isinstance(arguments, dict):
            parsed = arguments
        else:
            parsed = {}
        return {"name": name, "arguments": parsed}

    @staticmethod
    def _is_review_mode(state: ContextState) -> bool:
        return "review" in state.goal.lower()

    @staticmethod
    def _fallback_plan(request: ReviewRequest | DebugRequest) -> AnalysisPlan:
        return AnalysisPlan(needs_tools=False, tool_calls=[])

    def _record_event(
        self, event_type: EventType, phase: str, payload: dict[str, Any]
    ) -> None:
        if self._event_log is None:
            return
        self._event_log.record(
            EventEntry(
                run_id=self._run_id,
                event_type=event_type,
                phase=phase,
                payload=payload,
            )
        )

    def _close_event_log(self) -> None:
        if self._event_log is not None:
            self._event_log.close()
