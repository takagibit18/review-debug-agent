"""LLM inference engine — model reasoning and plan formulation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from pydantic import ValidationError

from src.analyzer.context_builder import ContextBuilder
from src.analyzer.context_state import ContextState
from src.analyzer.event_log import EventType
from src.analyzer.location import normalize_location
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.prompts import (
    FINALIZE_REVIEW_NOTICE,
    FINALIZE_DEBUG_NOTICE,
    build_debug_messages,
    build_debug_messages_async,
    build_review_messages,
    build_review_messages_async,
)
from src.analyzer.schemas import AnalysisPlan, DebugRequest, DebugResponse, ReviewRequest
from src.analyzer.trace import TraceRecorder
from src.config import get_settings
from src.models.client import ModelClient
from src.models.schemas import Message, ModelResponse
from src.tools.base import ToolResult, ToolSpec

logger = logging.getLogger(__name__)


class InferenceEngine:
    """Build messages, call model client, and parse structured plan."""

    def __init__(
        self,
        model_client: ModelClient,
        trace_recorder: TraceRecorder | None = None,
        trace_event_writer: Callable[[EventType, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._model_client = model_client
        self._trace_recorder = trace_recorder
        self._trace_event_writer = trace_event_writer

    async def analyze(
        self,
        state: ContextState,
        request: ReviewRequest | DebugRequest,
        tool_specs: list[ToolSpec],
        tool_schemas: list[dict[str, Any]] | None = None,
        diff_text: str = "",
        error_log: str = "",
        project_structure: str = "",
        file_contents: dict[str, str] | None = None,
        tool_feedback: list[dict[str, Any]] | None = None,
        feedback_digest_index: dict[str, dict[str, Any]] | None = None,
        prompt_input_token_budget: int | None = None,
        iteration: int = 0,
        force_submit: bool = False,
        near_last_iteration: bool = False,
    ) -> tuple[AnalysisPlan, int]:
        file_contents = file_contents or {}
        budget = (
            prompt_input_token_budget
            if prompt_input_token_budget is not None
            else get_settings().prompt_input_token_budget
        )
        cb = ContextBuilder()
        if isinstance(request, ReviewRequest):
            if get_settings().context_summary_enabled:
                messages = await build_review_messages_async(
                    request,
                    state,
                    diff_text,
                    file_contents,
                    prompt_token_budget=budget,
                    context_builder=cb,
                    compressor_model_client=self._model_client,
                    summary_enabled=True,
                    summary_max_tokens_per_part=get_settings().summary_max_tokens_per_part,
                    summary_model_name=request.model_name or get_settings().model_name,
                    project_structure=project_structure,
                )
            else:
                messages = build_review_messages(
                    request,
                    state,
                    diff_text,
                    file_contents,
                    prompt_token_budget=budget,
                    context_builder=cb,
                    project_structure=project_structure,
                )
        else:
            if get_settings().context_summary_enabled:
                messages = await build_debug_messages_async(
                    request,
                    state,
                    error_log,
                    file_contents,
                    prompt_token_budget=budget,
                    context_builder=cb,
                    compressor_model_client=self._model_client,
                    summary_enabled=True,
                    summary_max_tokens_per_part=get_settings().summary_max_tokens_per_part,
                    summary_model_name=request.model_name or get_settings().model_name,
                    project_structure=project_structure,
                )
            else:
                messages = build_debug_messages(
                    request,
                    state,
                    error_log,
                    file_contents,
                    prompt_token_budget=budget,
                    context_builder=cb,
                    project_structure=project_structure,
                )

        window_iterations = {
            item.get("iteration") for item in (tool_feedback or []) if isinstance(item, dict)
        }
        folded = self._build_folded_feedback_summary(
            feedback_digest_index or {}, window_iterations
        )
        if folded is not None:
            messages.append(folded)
        if tool_feedback:
            messages.extend(self._build_tool_feedback_messages(tool_feedback))
            failure_guidance = self._build_failure_guidance_message(tool_feedback)
            if failure_guidance is not None:
                messages.append(failure_guidance)
        if force_submit:
            notice = (
                FINALIZE_REVIEW_NOTICE
                if isinstance(request, ReviewRequest)
                else FINALIZE_DEBUG_NOTICE
            )
            messages.append(Message(role="user", content=notice))
        elif near_last_iteration:
            messages.append(
                Message(
                    role="user",
                    content=(
                        "Note: you are at the last allowed iteration. Prefer submitting now via "
                        "submit_review/submit_debug using what you already have, unless a tool "
                        "call is strictly necessary and has not been made with identical args."
                    ),
                )
            )

        tools = tool_schemas or []
        config = None
        if request.model_name:
            config = self._model_client.default_config.model_copy(
                update={"model": request.model_name}
            )
        response = await self._model_client.chat(messages=messages, config=config, tools=tools)
        plan, parse_meta = self._parse_tool_calls(response.tool_calls, request)
        fallback_json_found = False
        fallback_parse_valid = False
        if not plan.draft_review and not plan.draft_debug:
            fallback = self._fallback_extract_json(response.content)
            if fallback:
                fallback_json_found = True
                parsed = self._try_parse_submit_payload_from_json(fallback, request)
                if parsed:
                    fallback_parse_valid = True
                    plan = parsed
        self._record_trace(response, plan, parse_meta, iteration, fallback_json_found, fallback_parse_valid)
        return plan, response.usage.total_tokens

    def _parse_tool_calls(
        self, raw_calls: list[dict[str, Any]], request: ReviewRequest | DebugRequest
    ) -> tuple[AnalysisPlan, dict[str, Any]]:
        tool_calls: list[dict[str, Any]] = []
        draft_review: ReviewReport | None = None
        draft_debug: DebugResponse | None = None
        parse_meta: dict[str, Any] = {
            "submit_review_seen": False,
            "submit_debug_seen": False,
            "submit_review_validation_error": "",
            "submit_debug_validation_error": "",
            "location_warnings": [],
        }

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
                parse_meta["submit_review_seen"] = True
                normalized_payload, warnings = self._normalize_review_payload(payload)
                parse_meta["location_warnings"] = warnings
                try:
                    draft_review = ReviewReport.model_validate(normalized_payload)
                except ValidationError as exc:
                    logger.warning("Invalid submit_review payload ignored: %s", exc)
                    parse_meta["submit_review_validation_error"] = str(exc)
                    continue
                continue
            if name == "submit_debug":
                parse_meta["submit_debug_seen"] = True
                try:
                    draft_debug = DebugResponse.model_validate(
                        {
                            **payload,
                            "run_id": "",
                            "context": {"goal": "", "constraints": [], "decisions": []},
                        }
                    )
                except ValidationError as exc:
                    parse_meta["submit_debug_validation_error"] = str(exc)
                    continue
                continue
            tool_calls.append(raw)

        if isinstance(request, ReviewRequest):
            return (
                AnalysisPlan(
                    needs_tools=bool(tool_calls),
                    tool_calls=tool_calls,
                    draft_review=draft_review,
                ),
                parse_meta,
            )
        return (
            AnalysisPlan(
                needs_tools=bool(tool_calls),
                tool_calls=tool_calls,
                draft_debug=draft_debug,
            ),
            parse_meta,
        )

    def _try_parse_submit_payload_from_json(
        self, payload: dict[str, Any], request: ReviewRequest | DebugRequest
    ) -> AnalysisPlan | None:
        if isinstance(request, ReviewRequest):
            normalized_payload, _ = self._normalize_review_payload(payload)
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
    def _normalize_review_payload(payload: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
        if not isinstance(payload, dict):
            return {}, []
        normalized = dict(payload)
        issues = normalized.get("issues")
        if not isinstance(issues, list):
            return normalized, []
        normalized_issues: list[Any] = []
        warnings: list[dict[str, str]] = []
        for issue in issues:
            if not isinstance(issue, dict):
                normalized_issues.append(issue)
                continue
            issue_dict = dict(issue)
            raw_severity = str(issue_dict.get("severity", "")).strip().lower()
            mapped = InferenceEngine._normalize_severity(raw_severity)
            if mapped:
                issue_dict["severity"] = mapped
            raw_location = str(issue_dict.get("location", "")).strip()
            if raw_location:
                parsed_location = normalize_location(raw_location)
                issue_dict["location"] = parsed_location.canonical
                if parsed_location.warning:
                    warnings.append(
                        {
                            "location": raw_location,
                            "warning": parsed_location.warning,
                        }
                    )
            normalized_issues.append(issue_dict)
        normalized["issues"] = normalized_issues
        return normalized, warnings

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

            iteration = item.get("iteration")
            iter_tag = f"[iter={iteration}] " if iteration is not None else ""

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
                    content=iter_tag + json.dumps(result_payload, ensure_ascii=True),
                    tool_call_id=str(raw_tool_call.get("id", "")).strip(),
                )
            )
        return messages

    @staticmethod
    def _build_folded_feedback_summary(
        digest_index: dict[str, dict[str, Any]],
        window_iterations: set[Any],
    ) -> Message | None:
        """Produce a compact summary of prior tool results whose iterations are no longer
        part of the in-window feedback (so the model remembers them without reloading)."""
        if not digest_index:
            return None
        folded = [
            record
            for record in digest_index.values()
            if record.get("iteration") not in window_iterations
        ]
        if not folded:
            return None
        folded.sort(key=lambda item: (item.get("iteration", 0), item.get("name", "")))
        lines = [
            "prior_tool_results_summary: the following tool calls were already executed in earlier "
            "iterations of this run. Their full results are no longer in context, but you must NOT "
            "re-request them with the same arguments — synthesize using these summaries.",
        ]
        for record in folded:
            lines.append(
                f"- iter={record.get('iteration')} name={record.get('name')} "
                f"ok={record.get('ok')} args={record.get('args_preview')} "
                f"result={record.get('result_preview')}"
            )
        return Message(role="user", content="\n".join(lines))

    @staticmethod
    def _build_failure_guidance_message(
        tool_feedback: list[dict[str, Any]],
    ) -> Message | None:
        failed: list[str] = []
        for item in tool_feedback:
            result = item.get("result")
            payload: dict[str, Any]
            if isinstance(result, ToolResult):
                payload = result.model_dump()
            elif isinstance(result, dict):
                payload = result
            else:
                continue
            if payload.get("ok") is not False:
                continue
            call = item.get("tool_call", {}) if isinstance(item, dict) else {}
            fn = ""
            if isinstance(call, dict):
                fn_block = call.get("function", {})
                if isinstance(fn_block, dict):
                    fn = str(fn_block.get("name", "")).strip()
            error = str(payload.get("error") or "")
            recommendation = ""
            data = payload.get("data")
            if isinstance(data, dict):
                recommendation = str(data.get("recommended_next_step", "")).strip()
            failed.append(f"- tool={fn or 'unknown'} error={error} next={recommendation or 'inspect args'}")
        if not failed:
            return None
        return Message(
            role="user",
            content=(
                "Tool failures observed. Do not blindly retry the same path/args. "
                "If path is uncertain, run list_dir on parent directory first.\n"
                + "\n".join(failed[:8])
            ),
        )

    def _record_trace(
        self,
        response: ModelResponse,
        plan: AnalysisPlan,
        parse_meta: dict[str, Any],
        iteration: int,
        fallback_json_found: bool,
        fallback_parse_valid: bool,
    ) -> None:
        if (
            self._trace_recorder is None
            or self._trace_event_writer is None
            or not self._trace_recorder.allows_detail()
        ):
            return
        self._trace_recorder.record(
            self._trace_event_writer,
            EventType.MODEL_RESPONSE_DETAIL,
            "analyze",
            {
                "iteration": iteration,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "usage": response.usage.model_dump(),
                "assistant_content_preview": self._trace_recorder.build_text_preview(
                    response.content
                ),
                "tool_call_summaries": self._trace_recorder.build_tool_call_summaries(
                    response.tool_calls
                ),
            },
        )
        self._trace_recorder.record(
            self._trace_event_writer,
            EventType.PLAN_PARSED,
            "analyze",
            {
                "iteration": iteration,
                "needs_tools": plan.needs_tools,
                "tool_calls_count": len(plan.tool_calls),
                "has_draft_review": plan.draft_review is not None,
                "has_draft_debug": plan.draft_debug is not None,
                "submit_review_seen": bool(parse_meta.get("submit_review_seen")),
                "submit_debug_seen": bool(parse_meta.get("submit_debug_seen")),
                "submit_review_validation_error": self._trace_recorder.build_text_preview(
                    str(parse_meta.get("submit_review_validation_error", ""))
                ),
                "submit_debug_validation_error": self._trace_recorder.build_text_preview(
                    str(parse_meta.get("submit_debug_validation_error", ""))
                ),
                "location_warnings": parse_meta.get("location_warnings", []),
                "fallback_json_found": fallback_json_found,
                "fallback_parse_valid": fallback_parse_valid,
            },
        )
