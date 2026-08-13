"""LLM inference engine — model reasoning and plan formulation."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Callable
from uuid import uuid4

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
from src.analyzer.schemas import (
    AnalysisPlan,
    DebugRequest,
    DebugResponse,
    ReviewRequest,
)
from src.analyzer.trace import TraceRecorder
from src.config import get_settings
from src.models.client import ModelClient
from src.models.schemas import Message, ModelConfig, ModelResponse
from src.tools.base import ToolResult, ToolSpec

logger = logging.getLogger(__name__)
_SUBMIT_MAX_TOKENS = 2048
_SYNTHETIC_CONTEXT_MAX_CHARS = 3600
_FINAL_EVIDENCE_ENTRY_MAX_CHARS = 2400
_FINAL_EVIDENCE_TOOL_NAMES = {
    "changed_context",
    "get_changed_context",
    "read_file",
    "symbol_context",
    "find_symbol_context",
}
_PRIOR_ANALYSIS_CONCERN_PATTERN = re.compile(
    r"\b("
    r"bug|concern|regression|break(?:s|ing)?|compatibility|incorrect|error|"
    r"fail(?:s|ure)?|exception|user-visible|fallback|wrapper|truncate(?:s|d|ion)?"
    r")\b",
    re.IGNORECASE,
)
_DSML_ISSUES_PARAMETER_PATTERN = re.compile(
    r"parameter\s+name\s*=\s*\\?[\"']issues\\?[\"']",
    re.IGNORECASE,
)
_EMPTY_ISSUES_SUMMARY_CONCERN_PATTERN = re.compile(
    r"\b("
    r"one concern|concerns? noted|subtle logic change|logic change|"
    r"behavioral modification|behavior(?:al)? change|compatibility risk"
    r")\b",
    re.IGNORECASE,
)


class InferenceEngine:
    """Build messages, call model client, and parse structured plan."""

    def __init__(
        self,
        model_client: ModelClient,
        trace_recorder: TraceRecorder | None = None,
        trace_event_writer: Callable[[EventType, str, dict[str, Any]], None]
        | None = None,
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
        defer_submit: bool = False,
    ) -> tuple[AnalysisPlan, int, str]:
        file_contents = file_contents or {}
        settings = get_settings()
        submit_only = force_submit or near_last_iteration
        requested_budget = (
            prompt_input_token_budget
            if prompt_input_token_budget is not None
            else settings.prompt_input_token_budget
        )
        total_prompt_budget = requested_budget
        final_feedback_budget = 0
        if submit_only:
            total_prompt_budget = min(
                requested_budget, settings.final_submit_prompt_token_budget
            )
            final_feedback_budget = min(
                settings.final_submit_feedback_token_budget,
                max(0, total_prompt_budget - 1),
            )
        budget = max(1, total_prompt_budget - final_feedback_budget)
        cb = ContextBuilder()
        context_telemetry: dict[str, Any] = {}
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
                    telemetry_sink=context_telemetry,
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
                    telemetry_sink=context_telemetry,
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
                    telemetry_sink=context_telemetry,
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
                    telemetry_sink=context_telemetry,
                )

        final_evidence_telemetry = self._empty_final_evidence_telemetry(
            final_feedback_budget
        )
        if submit_only:
            final_evidence, final_evidence_telemetry = (
                self._build_final_submit_evidence_summary(
                    tool_feedback or [],
                    feedback_digest_index or {},
                    token_budget=final_feedback_budget,
                )
            )
            if final_evidence is not None:
                messages.append(final_evidence)
        else:
            window_iterations = {
                item.get("iteration")
                for item in (tool_feedback or [])
                if isinstance(item, dict)
            }
            folded = self._build_folded_feedback_summary(
                feedback_digest_index or {}, window_iterations
            )
            if folded is not None:
                messages.append(folded)
        if defer_submit:
            messages.append(
                Message(
                    role="user",
                    content=(
                        "Do not call submit_review yet. This evaluation run requires "
                        "one context-gathering round before final review. Use the "
                        "available read-only tools to inspect the most relevant changed "
                        "file, test, snapshot, or adjacent implementation context."
                    ),
                )
            )
        if tool_feedback and not submit_only:
            messages.extend(self._build_tool_feedback_messages(tool_feedback))
            failure_guidance = self._build_failure_guidance_message(tool_feedback)
            if failure_guidance is not None:
                messages.append(failure_guidance)
        if submit_only:
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

        tools = (
            self._submit_only_tools(tool_schemas or [], request)
            if submit_only
            else tool_schemas or []
        )
        self._record_context_telemetry(
            context_telemetry=context_telemetry,
            messages=messages,
            tools=tools,
            iteration=iteration,
            prompt_input_token_budget=total_prompt_budget,
            base_context_token_budget=budget,
            final_submit_feedback_token_budget=final_feedback_budget,
            final_evidence_telemetry=final_evidence_telemetry,
            force_submit=submit_only,
        )
        config = None
        if submit_only:
            config = self._build_submit_config(request)
        if request.model_name:
            if config is None:
                config = self._model_client.default_config.model_copy(
                    update={"model": request.model_name}
                )
            else:
                config.model = request.model_name
        config = self._with_thinking_disabled_if_needed(config)
        response = await self._model_client.chat(
            messages=messages, config=config, tools=tools
        )
        self._record_length_finish(response, iteration, config)
        plan, parse_meta = self._parse_tool_calls(
            response.tool_calls, request, force_submit=submit_only
        )
        parse_meta["tool_choice"] = self._trace_tool_choice(config)
        parse_meta["thinking_disabled"] = self._is_thinking_disabled(config)
        if (
            isinstance(request, ReviewRequest)
            and plan.draft_review is None
            and parse_meta.get("submit_review_seen")
            and parse_meta.get("submit_review_validation_error")
        ):
            initial_usage = response.usage
            (
                repair_plan,
                repair_response,
                repair_meta,
            ) = await self._retry_submit_review_validation_repair(
                messages=messages,
                request=request,
                tool_schemas=tool_schemas or [],
                validation_error=str(parse_meta["submit_review_validation_error"]),
            )
            repair_response.usage.total_tokens += initial_usage.total_tokens
            repair_response.usage.prompt_tokens += initial_usage.prompt_tokens
            repair_response.usage.completion_tokens += initial_usage.completion_tokens
            if repair_plan.draft_review is not None:
                plan = repair_plan
                response = repair_response
                parse_meta = repair_meta
            else:
                response.usage = repair_response.usage
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
        incomplete_reason = self._length_incomplete_reason(
            response, plan, fallback_parse_valid
        )
        plan.model_finish_reason = response.finish_reason
        if submit_only:
            plan.final_submit_evidence_included_count = int(
                final_evidence_telemetry["included_count"]
            )
            plan.final_submit_evidence_token_count = int(
                final_evidence_telemetry["estimated_tokens"]
            )
            plan.final_submit_evidence_truncated_count = int(
                final_evidence_telemetry["truncated_count"]
            )
        if incomplete_reason:
            plan.incomplete_reason = incomplete_reason
            parse_meta["incomplete_reason"] = incomplete_reason
            self._record_incomplete_response(
                response, iteration, config, incomplete_reason
            )
        self._record_trace(
            response,
            plan,
            parse_meta,
            iteration,
            fallback_json_found,
            fallback_parse_valid,
        )
        return plan, response.usage.total_tokens, response.reasoning_content

    async def _retry_submit_review_validation_repair(
        self,
        *,
        messages: list[Message],
        request: ReviewRequest,
        tool_schemas: list[dict[str, Any]],
        validation_error: str,
    ) -> tuple[AnalysisPlan, ModelResponse, dict[str, Any]]:
        repair_messages = [
            *messages,
            Message(
                role="user",
                content=(
                    "Your previous submit_review tool call was rejected by schema validation. "
                    "Call submit_review again as your only action, preserving supported findings "
                    "but fixing this exact validation error:\n"
                    f"{validation_error}"
                ),
            ),
        ]
        config = self._build_submit_config(request)
        thinking_override = self._thinking_disable_extra_body(config)
        if thinking_override:
            config.extra_body = {
                **(config.extra_body or {}),
                **thinking_override,
            }
        response = await self._model_client.chat(
            messages=repair_messages,
            config=config,
            tools=self._submit_only_tools(tool_schemas, request),
        )
        plan, parse_meta = self._parse_tool_calls(
            response.tool_calls, request, force_submit=True
        )
        parse_meta["tool_choice"] = self._trace_tool_choice(config)
        parse_meta["thinking_disabled"] = self._is_thinking_disabled(config)
        return plan, response, parse_meta

    def _build_submit_config(
        self, request: ReviewRequest | DebugRequest
    ) -> ModelConfig:
        return self._model_client.default_config.model_copy(
            update={
                "max_tokens": _SUBMIT_MAX_TOKENS,
                "tool_choice": self._forced_submit_tool_choice(request),
            }
        )

    def _with_thinking_disabled_if_needed(
        self,
        config: ModelConfig | None,
    ) -> ModelConfig | None:
        candidate = config or self._model_client.default_config
        thinking_override = self._thinking_disable_extra_body(candidate)
        if not thinking_override:
            return config
        updated = config or candidate.model_copy(deep=True)
        updated.extra_body = {
            **(updated.extra_body or {}),
            **thinking_override,
        }
        return updated

    @staticmethod
    def _forced_submit_tool_choice(
        request: ReviewRequest | DebugRequest,
    ) -> dict[str, dict[str, str] | str]:
        name = "submit_review" if isinstance(request, ReviewRequest) else "submit_debug"
        return {"type": "function", "function": {"name": name}}

    @staticmethod
    def _submit_only_tools(
        tool_schemas: list[dict[str, Any]],
        request: ReviewRequest | DebugRequest,
    ) -> list[dict[str, Any]]:
        expected = (
            "submit_review" if isinstance(request, ReviewRequest) else "submit_debug"
        )
        return [
            tool
            for tool in tool_schemas
            if isinstance(tool.get("function"), dict)
            and tool["function"].get("name") == expected
        ]

    @staticmethod
    def _thinking_disable_extra_body(config: ModelConfig) -> dict[str, Any]:
        model = config.model.strip().lower()
        base_url = str(get_settings().openai_base_url).strip().lower()
        if model.startswith("deepseek") or "deepseek" in base_url:
            return {"thinking": {"type": "disabled"}}
        if "dashscope" in base_url or model.startswith(("qwen", "glm")):
            return {"enable_thinking": False}
        return {}

    @staticmethod
    def _requires_thinking_disabled(config: ModelConfig) -> bool:
        return bool(InferenceEngine._thinking_disable_extra_body(config))

    @staticmethod
    def _is_thinking_disabled(config: ModelConfig | None) -> bool:
        if config is None or not isinstance(config.extra_body, dict):
            return False
        if config.extra_body.get("enable_thinking") is False:
            return True
        thinking = config.extra_body.get("thinking")
        return isinstance(thinking, dict) and thinking.get("type") == "disabled"

    @staticmethod
    def _trace_tool_choice(config: ModelConfig | None) -> Any:
        if config is None:
            return None
        return config.tool_choice

    def _parse_tool_calls(
        self,
        raw_calls: list[dict[str, Any]],
        request: ReviewRequest | DebugRequest,
        *,
        force_submit: bool = False,
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
            "force_submit_discarded_count": 0,
        }

        for raw in raw_calls:
            function_block = raw.get("function") if isinstance(raw, dict) else None
            if not isinstance(function_block, dict):
                continue
            name = str(function_block.get("name", "")).strip()
            arguments = function_block.get("arguments", "{}")
            argument_error = ""
            try:
                payload = self._parse_tool_arguments(arguments)
            except json.JSONDecodeError as exc:
                payload = {}
                argument_error = f"Invalid JSON arguments for {name}: {exc}"
            except Exception as exc:  # noqa: BLE001
                payload = {}
                argument_error = f"Invalid arguments for {name}: {exc}"

            if name == "submit_review":
                parse_meta["submit_review_seen"] = True
                if argument_error or not isinstance(payload, dict):
                    error = (
                        argument_error
                        or f"Invalid submit_review arguments type: {type(payload).__name__}"
                    )
                    logger.warning("Invalid submit_review arguments ignored: %s", error)
                    parse_meta["submit_review_validation_error"] = error
                    continue
                payload_error = self._validate_submit_review_payload(payload)
                if payload_error:
                    logger.warning(
                        "Invalid submit_review payload ignored: %s", payload_error
                    )
                    parse_meta["submit_review_validation_error"] = payload_error
                    continue
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
                if argument_error or not isinstance(payload, dict):
                    error = (
                        argument_error
                        or f"Invalid submit_debug arguments type: {type(payload).__name__}"
                    )
                    logger.warning("Invalid submit_debug arguments ignored: %s", error)
                    parse_meta["submit_debug_validation_error"] = error
                    continue
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
            if force_submit:
                parse_meta["force_submit_discarded_count"] += 1
                logger.warning(
                    "Force-submit mode: discarding non-submit tool_call '%s' to force fallback JSON extraction",
                    name,
                )
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

    @staticmethod
    def _parse_tool_arguments(arguments: Any) -> Any:
        if not isinstance(arguments, str):
            return arguments
        try:
            return json.loads(arguments)
        except json.JSONDecodeError as exc:
            if "Invalid control character" not in exc.msg:
                raise
            return json.loads(arguments, strict=False)

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
            return AnalysisPlan(
                needs_tools=False, tool_calls=[], draft_debug=draft_debug
            )
        except ValidationError:
            return None

    @staticmethod
    def _fallback_extract_json(content: str) -> dict[str, Any] | None:
        if not content:
            return None
        # Scan { positions from end to start — the last JSON block is most likely the target
        decoder = json.JSONDecoder()
        positions = [i for i, c in enumerate(content) if c == "{"]  # noqa: RUF015
        for pos in reversed(positions):
            try:
                obj, _ = decoder.raw_decode(content, pos)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
        # Fallback: original greedy regex
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            return None
        try:
            candidate = json.loads(match.group(0))
            return candidate if isinstance(candidate, dict) else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _validate_submit_review_payload(payload: dict[str, Any]) -> str:
        summary = payload.get("summary")
        if isinstance(summary, str) and _DSML_ISSUES_PARAMETER_PATTERN.search(summary):
            return "Invalid submit_review payload: DSML parameter leak for issues in summary"
        if "issues" not in payload:
            return "Invalid submit_review payload: missing required issues list"
        if not isinstance(payload["issues"], list):
            return (
                "Invalid submit_review payload: issues must be a list, "
                f"got {type(payload['issues']).__name__}"
            )
        if (
            isinstance(summary, str)
            and not payload["issues"]
            and _EMPTY_ISSUES_SUMMARY_CONCERN_PATTERN.search(summary)
        ):
            return (
                "Invalid submit_review payload: summary mentions review concerns "
                "but issues is empty"
            )
        return ""

    @staticmethod
    def _normalize_review_payload(
        payload: Any,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
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
    def _build_tool_feedback_messages(
        tool_feedback: list[dict[str, Any]],
    ) -> list[Message]:
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
            if raw_tool_call.get("synthetic_context") is True:
                result_payload = InferenceEngine._compact_synthetic_context_payload(
                    result_payload
                )
                messages.append(
                    Message(
                        role="user",
                        content=(
                            f"{iter_tag}prefetched_tool_context: "
                            + json.dumps(
                                {
                                    "tool": function_block.get("name", "unknown"),
                                    "arguments": function_block.get("arguments", "{}"),
                                    "result": result_payload,
                                },
                                ensure_ascii=True,
                            )
                        ),
                    )
                )
                continue

            call_id = str(raw_tool_call.get("id", "")).strip()
            if not call_id:
                call_id = "fallback-" + uuid4().hex[:12]
                raw_tool_call = {**raw_tool_call, "id": call_id}

            reasoning = item.get("reasoning_content") or None
            messages.append(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[raw_tool_call],
                    reasoning_content=reasoning,
                )
            )
            messages.append(
                Message(
                    role="tool",
                    content=iter_tag + json.dumps(result_payload, ensure_ascii=True),
                    tool_call_id=call_id,
                )
            )
        return messages

    @staticmethod
    def _compact_synthetic_context_payload(payload: dict[str, Any]) -> dict[str, Any]:
        compacted = dict(payload)
        data = compacted.get("data")
        if not isinstance(data, dict):
            return compacted
        compacted_data = dict(data)
        content = compacted_data.get("content")
        if isinstance(content, str) and len(content) > _SYNTHETIC_CONTEXT_MAX_CHARS:
            compacted_data["content"] = content[:_SYNTHETIC_CONTEXT_MAX_CHARS]
            compacted_data["truncated_for_prompt"] = True
            compacted_data["original_content_chars"] = len(content)
        compacted["data"] = compacted_data
        return compacted

    @staticmethod
    def _empty_final_evidence_telemetry(token_budget: int) -> dict[str, int]:
        return {
            "token_budget": max(0, token_budget),
            "available_tool_result_count": 0,
            "included_tool_result_count": 0,
            "available_concern_count": 0,
            "included_concern_count": 0,
            "included_count": 0,
            "deduplicated_count": 0,
            "truncated_count": 0,
            "estimated_tokens": 0,
        }

    @classmethod
    def _build_final_submit_evidence_summary(
        cls,
        tool_feedback: list[dict[str, Any]],
        digest_index: dict[str, dict[str, Any]],
        *,
        token_budget: int,
    ) -> tuple[Message | None, dict[str, int]]:
        """Build a bounded, deduplicated evidence handoff for submit-only calls."""

        telemetry = cls._empty_final_evidence_telemetry(token_budget)
        candidates: list[tuple[str, str]] = []
        seen_tools: set[str] = set()
        seen_concerns: set[str] = set()

        for item in reversed(tool_feedback):
            if not isinstance(item, dict):
                continue
            tool_call = item.get("tool_call")
            function_block = (
                tool_call.get("function") if isinstance(tool_call, dict) else None
            )
            if isinstance(function_block, dict):
                name = str(function_block.get("name", "")).strip()
                result_payload = cls._tool_result_payload(item.get("result"))
                if (
                    name in _FINAL_EVIDENCE_TOOL_NAMES
                    and result_payload.get("ok") is True
                ):
                    signature = cls._final_evidence_tool_signature(function_block)
                    if signature in seen_tools:
                        telemetry["deduplicated_count"] += 1
                    else:
                        seen_tools.add(signature)
                        telemetry["available_tool_result_count"] += 1
                        arguments = cls._json_preview(
                            function_block.get("arguments", "{}"), 400
                        )
                        result = cls._json_preview(
                            result_payload, _FINAL_EVIDENCE_ENTRY_MAX_CHARS
                        )
                        candidates.append(
                            (
                                "tool",
                                f"- tool_evidence iter={item.get('iteration')} "
                                f"name={name} args={arguments} result={result}",
                            )
                        )

            for snippet in cls._prior_analysis_concern_snippets(
                item.get("reasoning_content")
            ):
                normalized = " ".join(snippet.split()).lower()
                if normalized in seen_concerns:
                    telemetry["deduplicated_count"] += 1
                    continue
                seen_concerns.add(normalized)
                telemetry["available_concern_count"] += 1
                candidates.append(("concern", f"- prior_analysis_concern: {snippet}"))

        folded = sorted(
            digest_index.items(),
            key=lambda item: (
                int(item[1].get("iteration", 0) or 0),
                str(item[1].get("name", "")),
            ),
            reverse=True,
        )
        for signature, record in folded:
            name = str(record.get("name", "")).strip()
            if name not in _FINAL_EVIDENCE_TOOL_NAMES or record.get("ok") is not True:
                continue
            if signature in seen_tools:
                telemetry["deduplicated_count"] += 1
                continue
            seen_tools.add(signature)
            telemetry["available_tool_result_count"] += 1
            candidates.append(
                (
                    "tool",
                    f"- tool_evidence iter={record.get('iteration')} name={name} "
                    f"args={record.get('args_preview', '')} "
                    f"result={record.get('result_preview', '')}",
                )
            )

        if token_budget <= 0 or not candidates:
            telemetry["truncated_count"] = len(candidates)
            return None, telemetry

        builder = ContextBuilder()
        lines = [
            "final_submit_evidence_summary:",
            "Use this retained evidence and prior analysis when submitting the final "
            "review. Do not discard a supported concern merely because the exploration "
            "turn ended at the length limit.",
        ]
        if builder.estimate_tokens("\n".join(lines)) > token_budget:
            telemetry["truncated_count"] = len(candidates)
            return None, telemetry

        full_included = 0
        for kind, candidate in candidates:
            proposed = "\n".join([*lines, candidate])
            shortened = False
            if builder.estimate_tokens(proposed) <= token_budget:
                lines.append(candidate)
                full_included += 1
            else:
                current_tokens = builder.estimate_tokens("\n".join(lines))
                remaining = max(0, token_budget - current_tokens)
                fitted = cls._truncate_text_to_tokens(candidate, remaining, builder)
                if not fitted:
                    break
                lines.append(fitted)
                shortened = True
            telemetry["included_count"] += 1
            telemetry[
                "included_tool_result_count"
                if kind == "tool"
                else "included_concern_count"
            ] += 1
            if shortened:
                break

        telemetry["truncated_count"] = len(candidates) - full_included
        content = "\n".join(lines)
        telemetry["estimated_tokens"] = builder.estimate_tokens(content)
        return Message(role="user", content=content), telemetry

    @staticmethod
    def _tool_result_payload(result: Any) -> dict[str, Any]:
        if isinstance(result, ToolResult):
            return result.model_dump()
        if isinstance(result, dict):
            return result
        return {"ok": False, "error": "invalid_tool_result"}

    @staticmethod
    def _final_evidence_tool_signature(function_block: dict[str, Any]) -> str:
        name = str(function_block.get("name", "")).strip()
        arguments = function_block.get("arguments", "{}")
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except Exception:  # noqa: BLE001
                parsed = {"raw": arguments}
        else:
            parsed = arguments
        serialized = json.dumps(
            parsed, ensure_ascii=True, sort_keys=True, default=str
        )
        return f"{name}:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _json_preview(value: Any, max_chars: int) -> str:
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:  # noqa: BLE001
                parsed = value
        else:
            parsed = value
        try:
            serialized = json.dumps(parsed, ensure_ascii=True, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            serialized = str(parsed)
        if len(serialized) <= max_chars:
            return serialized
        return serialized[: max(0, max_chars - 14)] + "...[truncated]"

    @staticmethod
    def _prior_analysis_concern_snippets(reasoning: Any) -> list[str]:
        if not isinstance(reasoning, str) or not reasoning.strip():
            return []
        snippets: list[str] = []
        for segment in re.split(r"(?<=[.!?])\s+|[\r\n]+", reasoning):
            compact = " ".join(segment.split()).strip()
            match = _PRIOR_ANALYSIS_CONCERN_PATTERN.search(compact)
            if match is None or len(compact) < 20:
                continue
            if len(compact) > 900:
                start = max(0, match.start() - 300)
                compact = compact[start : start + 900]
            snippets.append(compact)
            if len(snippets) >= 4:
                break
        return snippets

    @staticmethod
    def _truncate_text_to_tokens(
        text: str,
        token_budget: int,
        builder: ContextBuilder,
    ) -> str:
        if token_budget <= 0 or not text:
            return ""
        if builder.estimate_tokens(text) <= token_budget:
            return text
        low = 0
        high = len(text)
        suffix = "...[truncated]"
        while low < high:
            middle = (low + high + 1) // 2
            candidate = text[:middle].rstrip() + suffix
            if builder.estimate_tokens(candidate) <= token_budget:
                low = middle
            else:
                high = middle - 1
        if low < 32:
            return ""
        return text[:low].rstrip() + suffix

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
            failed.append(
                f"- tool={fn or 'unknown'} error={error} next={recommendation or 'inspect args'}"
            )
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
                "content_length": len(response.content),
                "reasoning_content_length": len(response.reasoning_content),
                "tool_choice": parse_meta.get("tool_choice"),
                "thinking_disabled": bool(parse_meta.get("thinking_disabled")),
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
                "incomplete_reason": parse_meta.get("incomplete_reason", ""),
                "model_finish_reason": plan.model_finish_reason,
                "final_submit_evidence_included_count": (
                    plan.final_submit_evidence_included_count
                ),
                "final_submit_evidence_token_count": (
                    plan.final_submit_evidence_token_count
                ),
                "final_submit_evidence_truncated_count": (
                    plan.final_submit_evidence_truncated_count
                ),
                "force_submit_discarded_count": parse_meta.get(
                    "force_submit_discarded_count", 0
                ),
            },
        )

    def _record_context_telemetry(
        self,
        *,
        context_telemetry: dict[str, Any],
        messages: list[Message],
        tools: list[dict[str, Any]],
        iteration: int,
        prompt_input_token_budget: int,
        base_context_token_budget: int,
        final_submit_feedback_token_budget: int,
        final_evidence_telemetry: dict[str, int],
        force_submit: bool,
    ) -> None:
        if self._trace_event_writer is None:
            return
        builder = ContextBuilder()
        message_tokens = sum(builder.estimate_tokens(item.content) for item in messages)
        tool_schema_tokens = builder.estimate_tokens(
            json.dumps(tools, ensure_ascii=True)
        )
        self._trace_event_writer(
            EventType.CONTEXT_TELEMETRY,
            "analyze",
            {
                "iteration": iteration,
                "prompt_input_token_budget": prompt_input_token_budget,
                "base_context_token_budget": base_context_token_budget,
                "final_submit_feedback_token_budget": (
                    final_submit_feedback_token_budget
                ),
                "estimated_message_tokens": message_tokens,
                "estimated_tool_schema_tokens": tool_schema_tokens,
                "estimated_prompt_tokens": message_tokens + tool_schema_tokens,
                "force_submit": force_submit,
                "final_submit_evidence": final_evidence_telemetry,
                "tool_schema_count": len(tools),
                **context_telemetry,
            },
        )

    def _record_length_finish(
        self,
        response: ModelResponse,
        iteration: int,
        config: ModelConfig | None,
    ) -> None:
        if response.finish_reason != "length" or self._trace_event_writer is None:
            return
        self._trace_event_writer(
            EventType.ERROR,
            "analyze",
            {
                "iteration": iteration,
                "reason": "model_finish_reason_length",
                "model": response.model,
                "usage": response.usage.model_dump(),
                "max_tokens": config.max_tokens if config is not None else None,
                "content_length": len(response.content),
                "reasoning_content_length": len(response.reasoning_content),
            },
        )

    @staticmethod
    def _length_incomplete_reason(
        response: ModelResponse,
        plan: AnalysisPlan,
        fallback_parse_valid: bool,
    ) -> str:
        if response.finish_reason != "length":
            return ""
        if (
            plan.draft_review is not None
            or plan.draft_debug is not None
            or plan.tool_calls
            or fallback_parse_valid
        ):
            return ""
        return "model_finish_reason_length_no_output"

    def _record_incomplete_response(
        self,
        response: ModelResponse,
        iteration: int,
        config: ModelConfig | None,
        reason: str,
    ) -> None:
        if self._trace_event_writer is None:
            return
        self._trace_event_writer(
            EventType.ERROR,
            "analyze",
            {
                "iteration": iteration,
                "reason": reason,
                "model": response.model,
                "finish_reason": response.finish_reason,
                "usage": response.usage.model_dump(),
                "max_tokens": config.max_tokens if config is not None else None,
                "content_length": len(response.content),
                "reasoning_content_length": len(response.reasoning_content),
            },
        )
