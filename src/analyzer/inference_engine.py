"""LLM inference engine — model reasoning and plan formulation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from src.analyzer.context_builder import ContextBuilder
from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.prompts import build_debug_messages, build_review_messages
from src.analyzer.schemas import AnalysisPlan, DebugRequest, DebugResponse, ReviewRequest
from src.config import get_settings
from src.models.client import ModelClient
from src.models.schemas import Message, ModelConfig
from src.tools.base import ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class InferenceEngine:
    """Build messages, call model client, and parse structured plan."""

    def __init__(self, model_client: ModelClient) -> None:
        self._model_client = model_client

    async def analyze(
        self,
        state: ContextState,
        request: ReviewRequest | DebugRequest,
        tool_specs: list[ToolSpec],
        tool_schemas: list[dict[str, Any]] | None = None,
        diff_text: str = "",
        error_log: str = "",
        file_contents: dict[str, str] | None = None,
        tool_feedback: list[dict[str, Any]] | None = None,
        prompt_input_token_budget: int | None = None,
    ) -> tuple[AnalysisPlan, int]:
        file_contents = file_contents or {}
        budget = (
            prompt_input_token_budget
            if prompt_input_token_budget is not None
            else get_settings().prompt_input_token_budget
        )
        cb = ContextBuilder()
        if isinstance(request, ReviewRequest):
            messages = build_review_messages(
                request,
                state,
                diff_text,
                file_contents,
                prompt_token_budget=budget,
                context_builder=cb,
            )
        else:
            messages = build_debug_messages(
                request,
                state,
                error_log,
                file_contents,
                prompt_token_budget=budget,
                context_builder=cb,
            )

        if tool_feedback:
            messages.extend(self._build_tool_feedback_messages(tool_feedback))

        tools = tool_schemas or []
        config = ModelConfig(model=request.model_name) if request.model_name else None
        response = await self._model_client.chat(messages=messages, config=config, tools=tools)
        plan = self._parse_tool_calls(response.tool_calls, request)
        if not plan.draft_review and not plan.draft_debug:
            fallback = self._fallback_extract_json(response.content)
            if fallback:
                parsed = self._try_parse_submit_payload_from_json(fallback, request)
                if parsed:
                    plan = parsed
        return plan, response.usage.total_tokens

    def _parse_tool_calls(
        self, raw_calls: list[dict[str, Any]], request: ReviewRequest | DebugRequest
    ) -> AnalysisPlan:
        tool_calls: list[dict[str, Any]] = []
        draft_review: ReviewReport | None = None
        draft_debug: DebugResponse | None = None

        for raw in raw_calls:
            function_block = raw.get("function") if isinstance(raw, dict) else None
            if not isinstance(function_block, dict):
                continue
            name = str(function_block.get("name", "")).strip()
            arguments = function_block.get("arguments", "{}")
            try:
                payload = json.loads(arguments) if isinstance(arguments, str) else arguments
            except Exception:  # noqa: BLE001
                payload = {}

            if name == "submit_review":
                normalized_payload = self._normalize_review_payload(payload)
                try:
                    draft_review = ReviewReport.model_validate(normalized_payload)
                except ValidationError as exc:
                    logger.warning("Invalid submit_review payload ignored: %s", exc)
                    continue
                continue
            if name == "submit_debug":
                try:
                    draft_debug = DebugResponse.model_validate(
                        {
                            **payload,
                            "run_id": "",
                            "context": {"goal": "", "constraints": [], "decisions": []},
                        }
                    )
                except ValidationError:
                    continue
                continue
            tool_calls.append(raw)

        if isinstance(request, ReviewRequest):
            return AnalysisPlan(
                needs_tools=bool(tool_calls),
                tool_calls=tool_calls,
                draft_review=draft_review,
            )
        return AnalysisPlan(
            needs_tools=bool(tool_calls),
            tool_calls=tool_calls,
            draft_debug=draft_debug,
        )

    def _try_parse_submit_payload_from_json(
        self, payload: dict[str, Any], request: ReviewRequest | DebugRequest
    ) -> AnalysisPlan | None:
        if isinstance(request, ReviewRequest):
            normalized_payload = self._normalize_review_payload(payload)
            try:
                report = ReviewReport.model_validate(normalized_payload)
                return AnalysisPlan(
                    needs_tools=False, tool_calls=[], draft_review=report
                )
            except ValidationError as exc:
                logger.warning("Invalid fallback review JSON ignored: %s", exc)
                return None
        try:
            draft_debug = DebugResponse.model_validate(
                {
                    **payload,
                    "run_id": "",
                    "context": {"goal": "", "constraints": [], "decisions": []},
                }
            )
            return AnalysisPlan(needs_tools=False, tool_calls=[], draft_debug=draft_debug)
        except ValidationError:
            return None

    @staticmethod
    def _fallback_extract_json(content: str) -> dict[str, Any] | None:
        match = re.search(r"\{[\s\S]*\}", content or "")
        if not match:
            return None
        try:
            candidate = json.loads(match.group(0))
            if isinstance(candidate, dict):
                return candidate
            return None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _normalize_review_payload(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        normalized = dict(payload)
        issues = normalized.get("issues")
        if not isinstance(issues, list):
            return normalized
        normalized_issues: list[Any] = []
        for issue in issues:
            if not isinstance(issue, dict):
                normalized_issues.append(issue)
                continue
            issue_dict = dict(issue)
            raw_severity = str(issue_dict.get("severity", "")).strip().lower()
            mapped = InferenceEngine._normalize_severity(raw_severity)
            if mapped:
                issue_dict["severity"] = mapped
            normalized_issues.append(issue_dict)
        normalized["issues"] = normalized_issues
        return normalized

    @staticmethod
    def _normalize_severity(value: str) -> str:
        mapping = {
            "critical": "critical",
            "high": "critical",
            "major": "critical",
            "warning": "warning",
            "warn": "warning",
            "medium": "warning",
            "info": "info",
            "informational": "info",
            "low": "info",
            "minor": "info",
            "style": "style",
            "nit": "style",
            "nits": "style",
        }
        return mapping.get(value, value)

    @staticmethod
    def _build_tool_feedback_messages(tool_feedback: list[dict[str, Any]]) -> list[Message]:
        messages: list[Message] = []
        for item in tool_feedback:
            raw_tool_call = item.get("tool_call", {})
            if not isinstance(raw_tool_call, dict):
                continue
            function_block = raw_tool_call.get("function", {})
            if not isinstance(function_block, dict):
                continue

            tool_result = item.get("result")
            if isinstance(tool_result, ToolResult):
                result_payload = tool_result.model_dump()
            elif isinstance(tool_result, dict):
                result_payload = tool_result
            else:
                result_payload = {"ok": False, "error": "invalid_tool_result"}

            messages.append(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[raw_tool_call],
                )
            )
            messages.append(
                Message(
                    role="tool",
                    content=json.dumps(result_payload, ensure_ascii=True),
                    tool_call_id=str(raw_tool_call.get("id", "")).strip(),
                )
            )
        return messages
