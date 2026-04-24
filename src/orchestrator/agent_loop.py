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
from src.analyzer.context_state import ContextState, DecisionStep, ErrorDetail, RunDiagnostics
from src.analyzer.event_log import EventEntry, EventLog, EventType
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.schemas import AnalysisPlan, DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.analyzer.trace import TraceRecorder
from src.analyzer.result_processor import ResultProcessor
from src.config import get_settings
from src.models.client import ModelClient
from src.models.exceptions import ModelClientError
from src.orchestrator.tool_schemas import build_submit_tool_schemas, build_tool_schemas
from src.tools import create_default_registry
from src.tools.base import BaseTool, ToolRegistry, ToolResult, ToolSafety, ToolSpec
from src.tools.exceptions import FileNotFoundToolError, PathNotAllowedError, ToolError
from src.tools.path_utils import tool_workspace_root


class AgentOrchestrator:
    """5-phase orchestrator for review/debug sessions."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        confirm_high_risk: Any | None = None,
        permission_mode: Literal["default", "plan"] | None = None,
        temperature: float | None = None,
    ) -> None:
        self._settings = get_settings()
        self._external_registry: ToolRegistry | None = registry
        self._registry = registry or create_default_registry(include_execute=False)
        self._context_builder = ContextBuilder()
        self._result_processor = ResultProcessor(token_budget=self._settings.token_budget)
        self._model_client: ModelClient | None = None
        self._confirm_high_risk = confirm_high_risk
        self._permission_mode: Literal["default", "plan"] = (
            permission_mode or self._settings.permission_mode
        )
        self._run_id = ""
        self._event_log: EventLog | None = None
        self._last_plan: AnalysisPlan | None = None
        self._tool_feedback: list[dict[str, Any]] = []
        self._feedback_digest_index: dict[str, dict[str, Any]] = {}
        self._tool_dedup_cache: dict[str, ToolResult] = {}
        self._submit_review_seen_any = False
        self._submit_debug_seen_any = False
        self._latest_tokens = 0
        self._total_tokens = 0
        self._iteration = 0
        self._max_iterations = 1
        self._blocking_error = False
        self._budget_exhausted = False
        self._budget_state: str = "none"
        self._model_completed = False
        self._last_decision_reason: str = ""
        self._tool_error_count = 0
        self._had_blocking_tool_error = False
        self._finalize_attempted = False
        self._finalize_submit_seen = False
        self._workspace_root: Path | None = None
        self._temperature = temperature
        self._trace_recorder = TraceRecorder(
            detail_mode=self._settings.agent_trace_detail,
            max_chars=self._settings.agent_trace_max_chars,
            log_tool_body=self._settings.agent_trace_log_tool_body,
        )

    async def run_review(self, request: ReviewRequest) -> ReviewResponse:
        """Run review mode through the orchestrator loop."""
        self._reset_run(
            max_iterations=self._settings.review_max_iterations,
            repo_path=request.repo_path,
        )
        if self._external_registry is None:
            self._registry = create_default_registry(include_execute=False)
        state = self.prepare_context(request)
        response: ReviewResponse | DebugResponse | None = None
        while True:
            tool_specs = [] if self._permission_mode == "plan" else self._registry.list_specs()
            plan = await self.analyze(state, request, tool_specs)
            self._last_plan = plan
            tool_results = await self.execute_tools(plan, self._registry, state)
            response = self.format_result(state, tool_results)
            if not self.should_continue(state, response):
                break
            self._iteration += 1
        response = await self._maybe_force_submit_review(state, request, response)
        assert isinstance(response, ReviewResponse)
        response = self._attach_run_diagnostics(state, response)
        self._close_event_log()
        return response

    async def run_debug(self, request: DebugRequest) -> DebugResponse:
        """Run debug mode through the orchestrator loop."""
        self._reset_run(
            max_iterations=self._settings.debug_max_iterations,
            repo_path=request.repo_path,
        )
        if self._external_registry is None:
            self._registry = create_default_registry(include_execute=True)
        state = self.prepare_context(request)
        response: ReviewResponse | DebugResponse | None = None
        while True:
            tool_specs = [] if self._permission_mode == "plan" else self._registry.list_specs()
            plan = await self.analyze(state, request, tool_specs)
            self._last_plan = plan
            tool_results = await self.execute_tools(plan, self._registry, state)
            response = self.format_result(state, tool_results)
            if not self.should_continue(state, response):
                break
            self._iteration += 1
        response = await self._maybe_force_submit_debug(state, request, response)
        assert isinstance(response, DebugResponse)
        response = self._attach_run_diagnostics(state, response)
        self._close_event_log()
        return response

    async def _maybe_force_submit_review(
        self,
        state: ContextState,
        request: ReviewRequest,
        response: ReviewResponse | DebugResponse | None,
    ) -> ReviewResponse | DebugResponse:
        """If loop exited without a draft_review and budget is not hard-capped, issue one
        finalize-only analyze call and re-format."""
        assert response is not None
        if self._permission_mode == "plan":
            return response
        if self._budget_state == "hard_capped":
            return response
        if not isinstance(response, ReviewResponse):
            return response
        plan = self._last_plan
        if plan is None or plan.draft_review is not None:
            return response
        self._finalize_attempted = True
        finalize_plan = await self.analyze(state, request, tool_specs=[], force_submit=True)
        self._last_plan = finalize_plan
        self._finalize_submit_seen = finalize_plan.draft_review is not None
        response = self.format_result(state, tool_results=[])
        self._record_event(
            EventType.DECISION,
            "finalize",
            {
                "iteration": self._iteration,
                "finalize_attempt": True,
                "finalize_submit_seen": finalize_plan.draft_review is not None,
                "budget_state": self._budget_state,
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
        if self._budget_state == "hard_capped":
            return response
        if not isinstance(response, DebugResponse):
            return response
        plan = self._last_plan
        if plan is None or plan.draft_debug is not None:
            return response
        self._finalize_attempted = True
        finalize_plan = await self.analyze(state, request, tool_specs=[], force_submit=True)
        self._last_plan = finalize_plan
        self._finalize_submit_seen = finalize_plan.draft_debug is not None
        response = self.format_result(state, tool_results=[])
        self._record_event(
            EventType.DECISION,
            "finalize",
            {
                "iteration": self._iteration,
                "finalize_attempt": True,
                "finalize_submit_seen": finalize_plan.draft_debug is not None,
                "budget_state": self._budget_state,
            },
        )
        return response

    def prepare_context(self, request: ReviewRequest | DebugRequest) -> ContextState:
        """Create the initial context state for one run."""
        start = perf_counter()
        state = self._context_builder.prepare_context(request)
        if isinstance(request, ReviewRequest) and request.diff_mode:
            state.constraints.append("diff_mode")
        if isinstance(request, DebugRequest) and (request.error_log_path or request.error_log_text):
            state.constraints.append("error_log_provided")
        if self._permission_mode == "plan":
            state.constraints.append("plan_mode")
        self._record_event(EventType.PHASE_END, "prepare", {"elapsed_ms": int((perf_counter() - start) * 1000)})
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
        file_contents: dict[str, str] = {}
        project_structure = self._context_builder.describe_project_structure(
            request.repo_path
        )
        if isinstance(request, ReviewRequest):
            diff_text = request.diff_text or ""
            if request.diff_mode and not diff_text:
                diff_text = self._context_builder.load_diff(request.repo_path)
            file_contents = self._context_builder.build_review_file_context(
                request.repo_path,
                diff_text,
            )
        else:
            error_log_text = self._context_builder.load_error_log(
                request.error_log_path, request.error_log_text
            )
            file_contents = self._context_builder.build_debug_file_context(
                request.repo_path,
                error_log_text,
            )

        if file_contents:
            state.current_files = self._merge_current_files(
                request.repo_path,
                list(file_contents.keys()),
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
                if force_submit:
                    serialized_tools = build_submit_tool_schemas()
                else:
                    serialized_tools = build_tool_schemas(tool_specs) + build_submit_tool_schemas()
                result, total_tokens = await engine.analyze(
                    state=state,
                    request=request,
                    tool_specs=tool_specs,
                    tool_schemas=serialized_tools,
                    diff_text=diff_text,
                    error_log=error_log_text,
                    file_contents=file_contents,
                    project_structure=project_structure,
                    tool_feedback=self._tool_feedback,
                    feedback_digest_index=self._feedback_digest_index,
                    prompt_input_token_budget=self._settings.prompt_input_token_budget,
                    iteration=self._iteration,
                    force_submit=force_submit,
                    near_last_iteration=(self._iteration + 1) >= self._max_iterations,
                )
                self._latest_tokens = total_tokens
                if result.draft_review is not None:
                    self._submit_review_seen_any = True
                if result.draft_debug is not None:
                    self._submit_debug_seen_any = True
            except ModelClientError as exc:
                state.errors.append(
                    ErrorDetail(
                        file=request.repo_path,
                        message=f"Model analysis failed: {exc}",
                        category="runtime",
                    )
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
            raw_call = plan.tool_calls[index]
            call = self._parse_tool_call(raw_call)
            tool_name = call["name"]
            args = call["arguments"]
            tool = registry.get(tool_name)
            if tool is None:
                err = f"Tool not found: {tool_name}"
                state.errors.append(
                    ErrorDetail(file="", message=err, category="runtime")
                )
                results.append(ToolResult(ok=False, error=err))
                executed_feedback.append({"tool_call": raw_call, "result": results[-1]})
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
                        {"tool_call": raw_call, "result": results[-1]}
                    )
                    index += 1
                    continue

                result, error_detail = await self._execute_one_tool(
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
                    {"iteration": self._iteration, "name": tool_name, "ok": result.ok},
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
                        "args_digest": self._trace_recorder.build_tool_result_preview(args).get(
                            "digest", {}
                        ),
                        "result_preview": self._trace_recorder.build_tool_result_preview(result),
                    },
                )
                executed_feedback.append({"tool_call": raw_call, "result": result})
                index += 1
                continue

            if not tool.is_concurrency_safe():
                result, error_detail = await self._execute_one_tool(
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
                    {"iteration": self._iteration, "name": tool_name, "ok": result.ok},
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
                        "args_digest": self._trace_recorder.build_tool_result_preview(args).get(
                            "digest", {}
                        ),
                        "result_preview": self._trace_recorder.build_tool_result_preview(result),
                    },
                )
                executed_feedback.append({"tool_call": raw_call, "result": result})
                index += 1
                continue

            batch_calls: list[tuple[dict[str, Any], str, BaseTool, dict[str, Any]]] = [
                (raw_call, tool_name, tool, args)
            ]
            scan = index + 1
            while scan < len(plan.tool_calls):
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
                    self._execute_one_tool(
                        tool_name=batch_name,
                        tool=batch_tool,
                        args=batch_args,
                    )
                    for (_, batch_name, batch_tool, batch_args) in batch_calls
                ]
            )
            for (batch_raw, batch_name, _, _), (batch_result, batch_error) in zip(
                batch_calls, batch_results
            ):
                if batch_error is not None:
                    state.errors.append(batch_error)
                results.append(batch_result)
                self._record_event(
                    EventType.TOOL_CALL,
                    "execute_tools",
                    {
                        "iteration": self._iteration,
                        "name": batch_name,
                        "ok": batch_result.ok,
                    },
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
                executed_feedback.append({"tool_call": batch_raw, "result": batch_result})
            index += len(batch_calls)

        self._append_tool_feedback(executed_feedback)
        self._tool_error_count += sum(1 for result in results if not result.ok)
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
        self._had_blocking_tool_error = self._had_blocking_tool_error or blocking_error
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
                "submit_review_validation_error": plan.submit_review_validation_error,
                "submit_debug_validation_error": plan.submit_debug_validation_error,
                "issues_count": (
                    len(response.report.issues)
                    if isinstance(response, ReviewResponse)
                    else len(response.steps)
                ),
            },
        )
        return response

    def should_continue(self, state: ContextState, response: ReviewResponse | DebugResponse) -> bool:
        """Decide whether another loop iteration should run."""
        has_pending_tools = (
            False
            if self._permission_mode == "plan"
            else bool(self._last_plan and self._last_plan.needs_tools)
        )
        self._model_completed = not has_pending_tools and not self._blocking_error
        reached_limit = (self._iteration + 1) >= self._max_iterations

        stop = self._model_completed or reached_limit or self._budget_exhausted
        if self._budget_exhausted:
            state.errors.append(
                ErrorDetail(
                    file="",
                    message="Token budget exhausted; returning partial result.",
                    category="runtime",
                )
            )

        if self._model_completed:
            reason = "model_completed"
        elif reached_limit:
            reason = "max_iterations"
        elif self._budget_state == "hard_capped":
            reason = "budget_hard_capped"
        elif self._budget_state == "soft_capped":
            reason = "budget_soft_capped"
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
                "reached_limit": reached_limit,
                "budget_exhausted": self._budget_exhausted,
                "budget_state": self._budget_state,
                "reason": reason,
                "submit_review_seen_any": self._submit_review_seen_any,
                "submit_debug_seen_any": self._submit_debug_seen_any,
                "run_id": response.run_id,
            },
        )
        return not stop

    def _attach_run_diagnostics(
        self,
        state: ContextState,
        response: ReviewResponse | DebugResponse,
    ) -> ReviewResponse | DebugResponse:
        diagnostics = self._build_run_diagnostics(state, response)
        state.run_diagnostics = diagnostics
        if isinstance(response, ReviewResponse):
            if (
                diagnostics.headline
                and response.report.summary
                == "Review pipeline completed with placeholder summary."
            ):
                response.report.summary = diagnostics.headline
        elif (
            diagnostics.headline
            and response.summary == "Debug pipeline completed with placeholder summary."
        ):
            response.summary = diagnostics.headline
        self._record_event(
            EventType.DECISION,
            "final_status",
            {
                "iteration": self._iteration,
                "run_id": response.run_id,
                **diagnostics.model_dump(),
            },
        )
        return response

    def _build_run_diagnostics(
        self,
        state: ContextState,
        response: ReviewResponse | DebugResponse,
    ) -> RunDiagnostics:
        plan = self._last_plan or AnalysisPlan(needs_tools=False, tool_calls=[])
        is_review = isinstance(response, ReviewResponse)
        submit_seen = (
            self._submit_review_seen_any or plan.submit_review_seen
            if is_review
            else self._submit_debug_seen_any or plan.submit_debug_seen
        )
        validation_error = (
            plan.submit_review_validation_error
            if is_review
            else plan.submit_debug_validation_error
        )
        used_placeholder_summary = self._uses_placeholder_summary(response)
        fallback_plan_used = any(
            error.message.startswith("Model client unavailable")
            or error.message.startswith("Model analysis failed:")
            for error in state.errors
        )
        reasons: list[str] = []
        if self._last_decision_reason == "budget_hard_capped":
            reasons.append("Token budget hard cap reached before the run could finish.")
        elif self._last_decision_reason == "budget_soft_capped":
            reasons.append("Token budget soft cap reached; returning the latest partial result.")
        elif self._last_decision_reason == "max_iterations":
            reasons.append("Iteration limit reached before the run could continue.")
        elif self._last_decision_reason == "model_completed" and used_placeholder_summary:
            reasons.append("The model stopped without producing a valid final structured submission.")

        if self._tool_error_count:
            count = self._tool_error_count
            reasons.append(f"{count} tool call{'s' if count != 1 else ''} failed during execution.")

        if fallback_plan_used:
            reasons.append("The orchestrator had to fall back because model analysis was unavailable or failed.")

        if validation_error:
            submit_name = "submit_review" if is_review else "submit_debug"
            reasons.append(f"Model called {submit_name}, but the payload failed validation.")

        if plan.fallback_json_found and not plan.fallback_parse_valid:
            reasons.append("Assistant text contained fallback JSON, but it did not match the expected schema.")

        if used_placeholder_summary:
            if self._finalize_attempted and not self._finalize_submit_seen:
                reasons.append("A finalize-only retry still did not produce a valid final submission.")
            elif not submit_seen:
                reasons.append("The model never produced a valid final submission.")
        elif self._had_blocking_tool_error:
            reasons.append("A structured result was produced, but some earlier tool executions failed.")

        if self._permission_mode == "plan":
            reasons.append("Plan mode disabled tool execution and final submission.")

        reasons = list(dict.fromkeys(reason for reason in reasons if reason))
        if used_placeholder_summary or validation_error or fallback_plan_used:
            status = "degraded"
        elif self._budget_state != "none" or self._tool_error_count > 0:
            status = "partial"
        else:
            status = "completed"

        return RunDiagnostics(
            status=status,
            stop_reason=self._last_decision_reason,
            budget_state=self._budget_state,
            blocking_tool_error=self._had_blocking_tool_error,
            tool_error_count=self._tool_error_count,
            error_count=len(state.errors),
            used_placeholder_summary=used_placeholder_summary,
            finalize_attempted=self._finalize_attempted,
            finalize_submit_seen=self._finalize_submit_seen,
            submit_review_seen=self._submit_review_seen_any or plan.submit_review_seen,
            submit_debug_seen=self._submit_debug_seen_any or plan.submit_debug_seen,
            submit_review_validation_error=plan.submit_review_validation_error,
            submit_debug_validation_error=plan.submit_debug_validation_error,
            fallback_json_found=plan.fallback_json_found,
            fallback_parse_valid=plan.fallback_parse_valid,
            fallback_plan_used=fallback_plan_used,
            headline=self._build_diagnostic_headline(
                is_review=is_review,
                used_placeholder_summary=used_placeholder_summary,
                validation_error=validation_error,
                submit_seen=submit_seen,
                fallback_plan_used=fallback_plan_used,
            ),
            reasons=reasons,
        )

    def _build_diagnostic_headline(
        self,
        *,
        is_review: bool,
        used_placeholder_summary: bool,
        validation_error: str,
        submit_seen: bool,
        fallback_plan_used: bool,
    ) -> str:
        label = "Review" if is_review else "Debug"
        if fallback_plan_used:
            return f"{label} fell back because model analysis was unavailable or failed."
        if validation_error:
            return f"{label} stopped because the final structured submission was invalid."
        if used_placeholder_summary:
            if self._last_decision_reason in {"budget_soft_capped", "budget_hard_capped"}:
                return f"{label} stopped because the token budget was exhausted before a final submission."
            if self._last_decision_reason == "max_iterations":
                return f"{label} stopped at the iteration limit before a final submission."
            if self._finalize_attempted and not self._finalize_submit_seen:
                return f"{label} retried final submission, but still did not get a valid final result."
            if not submit_seen:
                return f"{label} finished without producing a valid final submission."
            return f"{label} returned placeholder output because no valid final submission was available."
        if self._tool_error_count > 0 and self._budget_state != "none":
            return f"{label} returned the latest partial result after tool failures and budget pressure."
        if self._tool_error_count > 0:
            return f"{label} returned a partial result because some tool calls failed."
        if self._budget_state != "none":
            return f"{label} returned the latest partial result before the budget limit."
        return ""

    @staticmethod
    def _uses_placeholder_summary(response: ReviewResponse | DebugResponse) -> bool:
        if isinstance(response, ReviewResponse):
            return response.report.summary == "Review pipeline completed with placeholder summary."
        return response.summary == "Debug pipeline completed with placeholder summary."

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
        self._last_plan = None
        self._tool_feedback = []
        self._feedback_digest_index = {}
        self._tool_dedup_cache = {}
        self._submit_review_seen_any = False
        self._submit_debug_seen_any = False
        self._latest_tokens = 0
        self._total_tokens = 0
        self._iteration = 0
        self._max_iterations = max_iterations
        self._blocking_error = False
        self._budget_exhausted = False
        self._budget_state = "none"
        self._model_completed = False
        self._last_decision_reason = ""
        self._tool_error_count = 0
        self._had_blocking_tool_error = False
        self._finalize_attempted = False
        self._finalize_submit_seen = False
        self._record_event(EventType.PHASE_START, "prepare", {"run_id": self._run_id})

    async def _execute_one_tool(
        self,
        *,
        tool_name: str,
        tool: BaseTool,
        args: dict[str, Any],
    ) -> tuple[ToolResult, ErrorDetail | None]:
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
                    },
                )
                return ToolResult(ok=True, data=hint), None
        with tool_workspace_root(self._workspace_root):
            try:
                data = await tool.execute(**args)
                result = ToolResult(ok=True, data=data)
                if dedup_key is not None:
                    self._tool_dedup_cache[dedup_key] = result
                return result, None
            except ToolError as exc:
                err = (
                    f"Tool execution failed for {tool_name}: {exc}"
                    f"{self._tool_recovery_hint(exc)}"
                )
                return (
                    ToolResult(ok=False, error=err),
                    ErrorDetail(file=exc.path, message=err, category="runtime"),
                )
            except Exception as exc:  # noqa: BLE001
                err = f"Tool execution failed for {tool_name}: {exc}"
                return (
                    ToolResult(ok=False, error=err),
                    ErrorDetail(file="", message=err, category="runtime"),
                )

    @staticmethod
    def _tool_dedup_key(tool_name: str, args: dict[str, Any]) -> str:
        try:
            serialized = _json.dumps(args, ensure_ascii=True, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            serialized = str(args)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"{tool_name}:{digest}"

    @staticmethod
    def _tool_recovery_hint(exc: ToolError) -> str:
        if isinstance(exc, FileNotFoundToolError):
            return " Hint: verify the path under repo_path with list_dir before retrying."
        if isinstance(exc, PathNotAllowedError):
            return " Hint: stay under repo_path and use list_dir to discover allowed directories."
        return ""

    def _append_tool_feedback(self, entries: list[dict[str, Any]]) -> None:
        """Append feedback entries with iteration metadata, maintain ring-buffer window
        and digest index for folded-summary injection."""
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
        function_block = tool_call.get("function") if isinstance(tool_call, dict) else None
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
            serialized = _json.dumps(parsed, ensure_ascii=True, sort_keys=True, default=str)
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
        function_block = tool_call.get("function") if isinstance(tool_call, dict) else {}
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
            )
        try:
            self._model_client = ModelClient(temperature=self._temperature)
            return InferenceEngine(
                self._model_client,
                trace_recorder=self._trace_recorder,
                trace_event_writer=self._record_event,
            )
        except Exception:  # noqa: BLE001
            return None

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
        if isinstance(request, ReviewRequest):
            from src.analyzer.output_formatter import ReviewReport

            return AnalysisPlan(
                needs_tools=False,
                tool_calls=[],
                draft_review=ReviewReport(
                    summary="Review pipeline initialized. Detailed analysis is not implemented yet."
                ),
            )
        return AnalysisPlan(
            needs_tools=False,
            tool_calls=[],
            draft_debug=DebugResponse(
                run_id="",
                summary="Debug pipeline initialized. Detailed analysis is not implemented yet.",
                hypotheses=[],
                steps=[],
                context=ContextState(),
            ),
        )

    def _record_event(self, event_type: EventType, phase: str, payload: dict[str, Any]) -> None:
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

    @staticmethod
    def _merge_current_files(repo_path: str, file_paths: list[str]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for item in [repo_path, *file_paths]:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
        return merged
