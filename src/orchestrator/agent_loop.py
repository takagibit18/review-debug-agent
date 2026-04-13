"""Agent main loop — orchestrates the 5-phase cycle."""

from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from src.analyzer.context_builder import ContextBuilder
from src.analyzer.context_state import ContextState, DecisionStep, ErrorDetail
from src.analyzer.event_log import EventEntry, EventLog, EventType
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.schemas import AnalysisPlan, DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.analyzer.result_processor import ResultProcessor
from src.config import get_settings
from src.models.client import ModelClient
from src.models.exceptions import ModelClientError
from src.orchestrator.tool_schemas import build_submit_tool_schemas, build_tool_schemas
from src.tools import create_default_registry
from src.tools.base import BaseTool, ToolRegistry, ToolResult, ToolSafety, ToolSpec
from src.tools.exceptions import ToolError
from src.tools.path_utils import tool_workspace_root


class AgentOrchestrator:
    """5-phase orchestrator for review/debug sessions."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        confirm_high_risk: Any | None = None,
        permission_mode: Literal["default", "plan"] | None = None,
    ) -> None:
        self._settings = get_settings()
        self._registry = registry or create_default_registry()
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
        self._latest_tokens = 0
        self._total_tokens = 0
        self._iteration = 0
        self._max_iterations = 1
        self._blocking_error = False
        self._budget_exhausted = False
        self._model_completed = False
        self._workspace_root: Path | None = None

    async def run_review(self, request: ReviewRequest) -> ReviewResponse:
        """Run review mode through the orchestrator loop."""
        self._reset_run(
            max_iterations=self._settings.review_max_iterations,
            repo_path=request.repo_path,
        )
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
        assert isinstance(response, ReviewResponse)
        self._close_event_log()
        return response

    async def run_debug(self, request: DebugRequest) -> DebugResponse:
        """Run debug mode through the orchestrator loop."""
        self._reset_run(
            max_iterations=self._settings.debug_max_iterations,
            repo_path=request.repo_path,
        )
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
        assert isinstance(response, DebugResponse)
        self._close_event_log()
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
        if isinstance(request, ReviewRequest):
            diff_text = request.diff_text or ""
            if request.diff_mode and not diff_text:
                diff_text = self._context_builder.load_diff(request.repo_path)
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
                serialized_tools = build_tool_schemas(tool_specs) + build_submit_tool_schemas()
                result, total_tokens = await engine.analyze(
                    state=state,
                    request=request,
                    tool_specs=tool_specs,
                    tool_schemas=serialized_tools,
                    diff_text=diff_text,
                    error_log=error_log_text,
                    tool_feedback=self._tool_feedback,
                )
                self._latest_tokens = total_tokens
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
                        {"name": tool_name, "category": "security"},
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
                    {"name": tool_name, "ok": result.ok},
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
                    {"name": tool_name, "ok": result.ok},
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
                    {"name": batch_name, "ok": batch_result.ok},
                )
                executed_feedback.append({"tool_call": batch_raw, "result": batch_result})
            index += len(batch_calls)

        self._tool_feedback = executed_feedback
        return results

    def format_result(
        self,
        state: ContextState,
        tool_results: list[ToolResult],
    ) -> ReviewResponse | DebugResponse:
        """Build final response according to run mode."""
        plan = self._last_plan or AnalysisPlan(needs_tools=False, tool_calls=[])
        self._total_tokens += self._latest_tokens
        self._budget_exhausted = self._result_processor.is_budget_exhausted(self._total_tokens)

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
                "blocking_error": blocking_error,
                "total_tokens": self._total_tokens,
                "budget_exhausted": self._budget_exhausted,
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

        state.decisions.append(
            DecisionStep(
                phase="continue",
                action="Evaluate continue conditions",
                result=(
                    "stop:model_completed"
                    if self._model_completed
                    else "stop:max_iterations"
                    if reached_limit
                    else "stop:budget_exhausted"
                    if self._budget_exhausted
                    else "continue"
                ),
            )
        )
        self._record_event(
            EventType.DECISION,
            "continue",
            {
                "model_completed": self._model_completed,
                "reached_limit": reached_limit,
                "budget_exhausted": self._budget_exhausted,
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
        self._last_plan = None
        self._tool_feedback = []
        self._latest_tokens = 0
        self._total_tokens = 0
        self._iteration = 0
        self._max_iterations = max_iterations
        self._blocking_error = False
        self._budget_exhausted = False
        self._model_completed = False
        self._record_event(EventType.PHASE_START, "prepare", {"run_id": self._run_id})

    async def _execute_one_tool(
        self,
        *,
        tool_name: str,
        tool: BaseTool,
        args: dict[str, Any],
    ) -> tuple[ToolResult, ErrorDetail | None]:
        with tool_workspace_root(self._workspace_root):
            try:
                data = await tool.execute(**args)
                return ToolResult(ok=True, data=data), None
            except ToolError as exc:
                err = f"Tool execution failed for {tool_name}: {exc}"
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
            return InferenceEngine(self._model_client)
        try:
            self._model_client = ModelClient()
            return InferenceEngine(self._model_client)
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
