"""Agent main loop — orchestrates the 5-phase cycle.

Each invocation drives a single review or debug session through:
1. Context preparation (load diff, related files)
2. Model analysis (LLM reasoning)
3. Tool execution (read files, run tests, grep, etc.)
4. Result processing (aggregate, format)
5. Continue / terminate decision
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from src.analyzer.context_state import ContextState, DecisionStep
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.schemas import (
    DebugRequest,
    DebugResponse,
    ReviewRequest,
    ReviewResponse,
)


class AnalysisPlan(BaseModel):
    """Structured placeholder analysis plan produced by the analyze phase."""

    needs_tools: bool = Field(default=False)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    draft_review: ReviewReport | None = Field(default=None)
    draft_debug: DebugResponse | None = Field(default=None)


class ToolResult(BaseModel):
    """Structured placeholder tool result envelope."""

    ok: bool = Field(default=True)
    data: Any = Field(default=None)
    error: str | None = Field(default=None)


class AgentOrchestrator:
    """Minimal orchestrator entry points for review and debug sessions."""

    async def run_review(self, request: ReviewRequest) -> ReviewResponse:
        """Run the review entry point."""
        state = self.prepare_context(request)
        plan = self.analyze(state, request, [])
        tool_results = self.execute_tools(plan, None, state)
        response = self.format_result(state, tool_results, plan)
        self.should_continue(state, response)
        return response

    async def run_debug(self, request: DebugRequest) -> DebugResponse:
        """Run the debug entry point."""
        state = self.prepare_context(request)
        plan = self.analyze(state, request, [])
        tool_results = self.execute_tools(plan, None, state)
        response = self.format_result(state, tool_results, plan)
        self.should_continue(state, response)
        return response

    @staticmethod
    def prepare_context(request: ReviewRequest | DebugRequest) -> ContextState:
        """Create the initial context state for one run."""
        goal = "Run structured code review"
        if isinstance(request, DebugRequest):
            goal = "Run structured debug analysis"

        return ContextState(
            goal=goal,
            constraints=["cli_entrypoint", "placeholder_orchestrator"],
            decisions=[
                DecisionStep(
                    phase="prepare",
                    action="Initialize context state",
                    result=f"Tracking {request.repo_path}",
                )
            ],
            current_files=[request.repo_path],
        )

    @staticmethod
    def analyze(
        state: ContextState,
        request: ReviewRequest | DebugRequest,
        tool_specs: list[dict[str, Any]],
    ) -> AnalysisPlan:
        """Build a minimal placeholder analysis plan."""
        state.decisions.append(
            DecisionStep(
                phase="analyze",
                action="Build placeholder analysis plan",
                result="No tools requested in the initial scaffold",
            )
        )
        if "review" in state.goal.lower():
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
        )

    @staticmethod
    def execute_tools(
        plan: AnalysisPlan,
        registry: Any,
        state: ContextState,
    ) -> list[ToolResult]:
        """Return placeholder tool results for the execute_tools phase."""
        state.decisions.append(
            DecisionStep(
                phase="execute_tools",
                action="Execute placeholder tool phase",
                result="No tool execution required",
            )
        )
        if not plan.needs_tools:
            return []
        return [ToolResult(ok=True, data=tool_call) for tool_call in plan.tool_calls]

    @staticmethod
    def format_result(
        state: ContextState,
        tool_results: list[ToolResult],
        plan: AnalysisPlan,
    ) -> ReviewResponse | DebugResponse:
        """Build the final structured response for the current mode."""
        state.decisions.append(
            DecisionStep(
                phase="format",
                action="Build structured response",
                result="Formatted placeholder response",
            )
        )
        if "review" in state.goal.lower():
            report = plan.draft_review or ReviewReport(
                summary="Review pipeline initialized. Detailed analysis is not implemented yet."
            )
            return ReviewResponse(
                run_id=str(uuid4()),
                report=report,
                context=state,
            )

        return plan.draft_debug or DebugResponse(
            run_id=str(uuid4()),
            summary="Debug pipeline initialized. Detailed analysis is not implemented yet.",
            hypotheses=[],
            steps=[],
            context=state,
        )

    @staticmethod
    def should_continue(
        state: ContextState,
        response: ReviewResponse | DebugResponse,
    ) -> bool:
        """Return whether another loop iteration is required."""
        state.decisions.append(
            DecisionStep(
                phase="continue",
                action="Decide whether to continue the loop",
                result="Stop after one placeholder iteration",
            )
        )
        return False
