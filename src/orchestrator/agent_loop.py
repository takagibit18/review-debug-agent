"""Agent main loop — orchestrates the 5-phase cycle."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

from src.analyzer.context_builder import ContextBuilder
from src.analyzer.context_state import ContextState, DecisionStep, ErrorDetail
from src.analyzer.event_log import EventEntry, EventLog
from src.analyzer.inference_engine import InferenceEngine
from src.analyzer.schemas import AnalysisPlan, DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.analyzer.result_processor import ResultProcessor
from src.models.client import ModelClient
from src.models.exceptions import ModelClientError
from src.tools import create_default_registry
from src.tools.base import ToolRegistry, ToolResult
from src.tools.exceptions import ToolError


class AgentOrchestrator:
    """5-phase orchestrator for review/debug sessions."""

    def __init__(self, registry: ToolRegistry | None = None) -> None:
        self._registry = registry or create_default_registry()
        self._context_builder = ContextBuilder()
        self._result_processor = ResultProcessor()
        self._model_client: ModelClient | None = None
        self._run_id = ""
        self._event_log: EventLog | None = None
        self._last_plan: AnalysisPlan | None = None
        self._latest_tokens = 0
        self._total_tokens = 0
        self._iteration = 0
        self._max_iterations = 1
        self._blocking_error = False
        self._budget_exhausted = False
        self._model_completed = False

    async def run_review(self, request: ReviewRequest) -> ReviewResponse:
        """Run review mode through the orchestrator loop."""
        self._reset_run(max_iterations=1)
        state = self.prepare_context(request)
        response: ReviewResponse | DebugResponse | None = None
        while True:
            plan = await self.analyze(state, request, self._registry.list_specs())
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
        self._reset_run(max_iterations=3)
        state = self.prepare_context(request)
        response: ReviewResponse | DebugResponse | None = None
        while True:
            plan = await self.analyze(state, request, self._registry.list_specs())
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
        self._record_event("phase_end", "prepare", {"elapsed_ms": int((perf_counter() - start) * 1000)})
        return state

    async def analyze(
        self,
        state: ContextState,
        request: ReviewRequest | DebugRequest,
        tool_specs: list[Any],
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
                result, total_tokens = await engine.analyze(
                    state=state,
                    request=request,
                    tool_specs=tool_specs,
                    diff_text=diff_text,
                    error_log=error_log_text,
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
            "model_call",
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
                result="No tools requested" if not plan.needs_tools else "Executing requested tools",
            )
        )
        if not plan.needs_tools:
            return []

        results: list[ToolResult] = []
        for raw_call in plan.tool_calls:
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
                continue
            try:
                data = await tool.execute(**args)
                results.append(ToolResult(ok=True, data=data))
            except ToolError as exc:
                err = f"Tool execution failed for {tool_name}: {exc}"
                state.errors.append(
                    ErrorDetail(file=exc.path, message=err, category="runtime")
                )
                results.append(ToolResult(ok=False, error=err))
            except Exception as exc:  # noqa: BLE001
                err = f"Tool execution failed for {tool_name}: {exc}"
                state.errors.append(
                    ErrorDetail(file="", message=err, category="runtime")
                )
                results.append(ToolResult(ok=False, error=err))
            self._record_event(
                "tool_call",
                "execute_tools",
                {"name": tool_name, "ok": results[-1].ok},
            )
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
            "phase_end",
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
        has_pending_tools = bool(self._last_plan and self._last_plan.needs_tools)
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
            "decision",
            "continue",
            {
                "model_completed": self._model_completed,
                "reached_limit": reached_limit,
                "budget_exhausted": self._budget_exhausted,
                "run_id": response.run_id,
            },
        )
        return not stop

    def _reset_run(self, max_iterations: int) -> None:
        self._run_id = str(uuid4())
        self._event_log = EventLog(
            run_id=self._run_id,
            log_dir=Path(".cr-debug-agent") / "logs",
        )
        self._last_plan = None
        self._latest_tokens = 0
        self._total_tokens = 0
        self._iteration = 0
        self._max_iterations = max_iterations
        self._blocking_error = False
        self._budget_exhausted = False
        self._model_completed = False
        self._record_event("phase_start", "prepare", {"run_id": self._run_id})

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

    def _record_event(self, event_type: str, phase: str, payload: dict[str, Any]) -> None:
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
