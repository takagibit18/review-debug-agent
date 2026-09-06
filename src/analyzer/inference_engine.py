"""LLM inference engine — model reasoning and plan formulation."""

from __future__ import annotations

import hashlib
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
from src.analyzer.review_skills import SkillSelection
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
from src.models.compat import ModelCallPolicy
from src.models.conversation import ModelConversation
from src.models.schemas import (
    DraftFinding,
    DraftFindingInput,
    Message,
    ModelConfig,
    ModelResponse,
    TokenUsage,
)
from src.models.token_telemetry import estimate_tokens, serialize_json, token_component
from src.tools.base import ToolResult, ToolSpec

logger = logging.getLogger(__name__)
_SUBMIT_MAX_TOKENS = 4096
_EXPLORATION_MAX_TOKENS = 12288
_SYNTHETIC_CONTEXT_MAX_CHARS = 3600
_FINAL_EVIDENCE_ENTRY_MAX_CHARS = 2400
_FINAL_EVIDENCE_TOOL_NAMES = {
    "changed_context",
    "get_changed_context",
    "read_file",
    "symbol_context",
    "find_symbol_context",
}
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


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


class InferenceEngine:
    """Build messages, call model client, and parse structured plan."""

    def __init__(
        self,
        model_client: ModelClient,
        trace_recorder: TraceRecorder | None = None,
        trace_event_writer: Callable[[EventType, str, dict[str, Any]], None]
        | None = None,
        model_response_writer: Callable[[ModelResponse, int], str] | None = None,
        conversation: ModelConversation | None = None,
    ) -> None:
        self._model_client = model_client
        self._trace_recorder = trace_recorder
        self._trace_event_writer = trace_event_writer
        self._model_response_writer = model_response_writer
        self._conversation = conversation or ModelConversation()

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
        draft_findings: list[DraftFinding] | None = None,
        validator_result: dict[str, Any] | None = None,
        prompt_input_token_budget: int | None = None,
        iteration: int = 0,
        force_submit: bool = False,
        near_last_iteration: bool = False,
        defer_submit: bool = False,
        stage: str | None = None,
        skill_selection: SkillSelection | None = None,
        skill_telemetry: dict[str, Any] | None = None,
    ) -> tuple[AnalysisPlan, TokenUsage]:
        file_contents = file_contents or {}
        settings = get_settings()
        submit_only = force_submit or near_last_iteration or stage == "submit_only"
        inferred_stage = (
            "validate"
            if any(
                str(item.get("function", {}).get("name", ""))
                == "validate_review_draft"
                for item in (tool_schemas or [])
                if isinstance(item, dict)
                and isinstance(item.get("function"), dict)
            )
            else "explore"
        )
        # A final/near-limit call is submit-only even when an older caller did
        # not pass the newer explicit stage label.
        call_stage = "submit_only" if submit_only else (stage or inferred_stage)
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
        summary_enabled = settings.context_summary_enabled and not submit_only
        prompt_context = state
        prompt_diff_text = diff_text
        prompt_file_contents = file_contents
        prompt_project_structure = project_structure
        if submit_only and isinstance(request, ReviewRequest):
            # Submit-only keeps evidence handoff explicit and bounded.  The full
            # reviewer projection was already available to the previous turn;
            # only the validated/minimal spans below are reintroduced.
            prompt_context = state.model_copy(deep=True)
            prompt_context.candidate_context_manifests = []
            prompt_diff_text = ""
            prompt_file_contents = {}
            prompt_project_structure = ""
        if isinstance(request, ReviewRequest):
            if summary_enabled:
                messages = await build_review_messages_async(
                    request,
                    prompt_context,
                    prompt_diff_text,
                    prompt_file_contents,
                    prompt_token_budget=budget,
                    context_builder=cb,
                    compressor_model_client=self._model_client,
                    summary_enabled=True,
                    summary_max_tokens_per_part=get_settings().summary_max_tokens_per_part,
                    summary_model_name=request.model_name or get_settings().model_name,
                    project_structure=prompt_project_structure,
                    telemetry_sink=context_telemetry,
                    skill_selection=skill_selection,
                )
            else:
                messages = build_review_messages(
                    request,
                    prompt_context,
                    prompt_diff_text,
                    prompt_file_contents,
                    prompt_token_budget=budget,
                    context_builder=cb,
                    project_structure=prompt_project_structure,
                    telemetry_sink=context_telemetry,
                    skill_selection=skill_selection,
                )
        else:
            if summary_enabled:
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

        if skill_telemetry is not None:
            context_telemetry["review_skills"] = dict(skill_telemetry)

        final_evidence_telemetry = self._empty_final_evidence_telemetry(
            final_feedback_budget
        )
        finalize_conversation_insert_at = len(messages) if submit_only else None
        if submit_only:
            final_evidence, final_evidence_telemetry = (
                self._build_final_submit_evidence_summary(
                    tool_feedback or [],
                    feedback_digest_index or {},
                    draft_findings or [],
                    validator_result=validator_result,
                    candidate_context_manifests=state.candidate_context_manifests,
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
                        "Do not call submit_review yet. Submission is temporarily "
                        "unavailable during the initial exploration stage. Use the "
                        "available read-only tools to resolve the most important evidence "
                        "gap. Do not assume this is the only exploration round. In later "
                        "rounds, continue targeted investigation whenever material "
                        "evidence gaps remain."
                    ),
                )
            )
        if tool_feedback and not submit_only:
            messages.extend(
                self._build_tool_feedback_messages(
                    tool_feedback,
                    selected_file_complete_lines=context_telemetry.get(
                        "selected_file_complete_lines", {}
                    ),
                )
            )
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
        config = None
        if submit_only:
            config = self._build_submit_config(request)
        else:
            config = self._model_client.default_config.model_copy(
                update={"max_tokens": _EXPLORATION_MAX_TOKENS}
            )
        if request.model_name:
            if config is None:
                config = self._model_client.default_config.model_copy(
                    update={"model": request.model_name}
                )
            else:
                config.model = request.model_name
        policy = ModelCallPolicy(
            thinking="off" if submit_only else "high",
            forced_tool=self._submit_tool_name(request) if submit_only else None,
        )
        # Once deterministic validation has passed, the submit-only call is a
        # fresh, bounded handoff.  Replaying every prior assistant/tool turn
        # would re-send repeated source/tool feedback.  Legacy forced-finalize
        # callers without validator state retain the provider replay contract.
        minimal_submit_only = bool(
            submit_only
            and isinstance(validator_result, dict)
            and validator_result.get("submit_allowed") is True
        )
        conversation_messages = (
            [] if minimal_submit_only else self._conversation.messages()
        )
        conversation_history_count = len(conversation_messages)
        if submit_only:
            assert finalize_conversation_insert_at is not None
            conversation_history_start = finalize_conversation_insert_at
            messages[conversation_history_start:conversation_history_start] = (
                conversation_messages
            )
        else:
            conversation_history_start = len(messages)
            messages.extend(conversation_messages)
        self._record_context_telemetry(
            context_telemetry=context_telemetry,
            messages=messages,
            tools=tools,
            config=config,
            policy=policy,
            file_contents=file_contents,
            tool_feedback=tool_feedback or [],
            conversation_history_count=conversation_history_count,
            iteration=iteration,
            prompt_input_token_budget=total_prompt_budget,
            base_context_token_budget=budget,
            final_submit_feedback_token_budget=final_feedback_budget,
            final_evidence_telemetry=final_evidence_telemetry,
            force_submit=submit_only,
            stage=call_stage,
            relation_graph_summary=state.relation_graph_summary,
        )
        response = await self._chat_with_telemetry(
            messages=messages,
            config=config,
            tools=tools,
            policy=policy,
            iteration=iteration,
            stage=call_stage,
            force_submit=submit_only,
        )
        response_id = self._persist_model_response(response, iteration)
        self._record_length_finish(response, iteration, config)
        plan, parse_meta = self._parse_tool_calls(
            response.tool_calls, request, force_submit=submit_only
        )
        self._complete_invalid_draft_tool_calls(response.tool_calls, parse_meta)
        if plan.draft_finding_calls:
            plan.draft_finding_source_response_id = response_id
        parse_meta["tool_choice"] = self._trace_tool_choice(config)
        parse_meta["thinking_disabled"] = policy.thinking == "off"
        if (
            isinstance(request, ReviewRequest)
            and plan.draft_review is None
            and response.finish_reason != "length"
            and parse_meta.get("submit_review_seen")
            and parse_meta.get("submit_review_validation_error")
        ):
            initial_usage = response.usage
            (
                repair_plan,
                repair_response,
                repair_meta,
                repair_response_id,
            ) = await self._retry_submit_review_validation_repair(
                messages=messages,
                request=request,
                tool_schemas=tool_schemas or [],
                validation_error=str(parse_meta["submit_review_validation_error"]),
                iteration=iteration,
                prior_history_start=conversation_history_start,
                prior_history_count=conversation_history_count,
                invalid_tool_calls=response.tool_calls,
                stage=call_stage,
            )
            repair_response.usage.total_tokens += initial_usage.total_tokens
            repair_response.usage.prompt_tokens += initial_usage.prompt_tokens
            repair_response.usage.completion_tokens += initial_usage.completion_tokens
            repair_response.usage.reasoning_tokens += initial_usage.reasoning_tokens
            repair_response.usage_present = (
                repair_response.usage_present or response.usage_present
            )
            if repair_plan.draft_review is not None:
                repair_plan.draft_finding_calls = plan.draft_finding_calls
                repair_plan.draft_finding_source_response_id = (
                    plan.draft_finding_source_response_id
                )
                plan = repair_plan
                response = repair_response
                parse_meta = repair_meta
                response_id = repair_response_id
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
                    parsed.draft_finding_calls = plan.draft_finding_calls
                    parsed.draft_finding_source_response_id = (
                        plan.draft_finding_source_response_id
                    )
                    plan = parsed
        plan.source_response_id = response_id
        incomplete_reason = self._length_incomplete_reason(response, plan)
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
            plan.recovery_required = True
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
        return plan, response.usage

    async def _chat_with_telemetry(
        self,
        *,
        messages: list[Message],
        config: ModelConfig,
        tools: list[dict[str, Any]],
        policy: ModelCallPolicy,
        iteration: int,
        stage: str,
        force_submit: bool,
    ) -> ModelResponse:
        """Call the provider and emit one safe event for every provider attempt."""

        try:
            response = await self._model_client.chat(
                messages=messages,
                config=config,
                tools=tools,
                policy=policy,
                conversation=self._conversation,
            )
        except Exception as exc:
            self._record_provider_attempts(
                attempts=self._consume_provider_attempts(),
                response=None,
                error=exc,
                iteration=iteration,
                stage=stage,
                force_submit=force_submit,
                policy=policy,
                tool_schema_count=len(tools),
            )
            raise

        self._record_provider_attempts(
            attempts=self._consume_provider_attempts(),
            response=response,
            error=None,
            iteration=iteration,
            stage=stage,
            force_submit=force_submit,
            policy=policy,
            tool_schema_count=len(tools),
        )
        return response

    def _consume_provider_attempts(self) -> list[dict[str, Any]]:
        consumer = getattr(self._model_client, "consume_call_telemetry", None)
        if not callable(consumer):
            return []
        try:
            attempts = consumer()
        except Exception:  # noqa: BLE001
            return []
        return [item for item in attempts if isinstance(item, dict)]

    def _record_provider_attempts(
        self,
        *,
        attempts: list[dict[str, Any]],
        response: ModelResponse | None,
        error: Exception | None,
        iteration: int,
        stage: str,
        force_submit: bool,
        policy: ModelCallPolicy,
        tool_schema_count: int,
    ) -> None:
        if self._trace_event_writer is None:
            return
        if not attempts:
            attempts = [
                {
                    "provider_attempt": 1,
                    "success": response is not None,
                    "usage_present": bool(response and response.usage_present),
                    "prompt_tokens": response.usage.prompt_tokens if response else 0,
                    "completion_tokens": response.usage.completion_tokens
                    if response
                    else 0,
                    "total_tokens": response.usage.total_tokens if response else 0,
                    "reasoning_tokens": response.usage.reasoning_tokens
                    if response
                    else 0,
                    "cached_prompt_tokens": response.usage.cached_prompt_tokens
                    if response
                    else None,
                    "actual_reasoning_effort": response.actual_reasoning_effort
                    if response
                    else "unknown",
                    "provider_request_id": response.provider_request_id
                    if response
                    else "",
                    "usage_unknown": response is None,
                }
            ]
        for raw in attempts:
            success = bool(raw.get("success", response is not None))
            usage_present = bool(raw.get("usage_present", success))
            payload: dict[str, Any] = {
                "iteration": iteration,
                "provider_attempt": int(raw.get("provider_attempt", 1) or 1),
                "stage": stage,
                "force_submit": force_submit,
                "thinking": str(raw.get("thinking", policy.thinking)),
                "actual_reasoning_effort": str(
                    raw.get(
                        "actual_reasoning_effort",
                        response.actual_reasoning_effort
                        if response is not None
                        else "unknown",
                    )
                ),
                "forced_tool": str(
                    raw.get("forced_tool", policy.forced_tool or "none")
                ),
                "tool_schema_count": int(
                    raw.get("tool_schema_count", tool_schema_count) or 0
                ),
                "prompt_tokens": max(0, int(raw.get("prompt_tokens", 0) or 0)),
                "completion_tokens": max(
                    0, int(raw.get("completion_tokens", 0) or 0)
                ),
                "total_tokens": max(0, int(raw.get("total_tokens", 0) or 0)),
                "reasoning_tokens": max(
                    0, int(raw.get("reasoning_tokens", 0) or 0)
                ),
                "cached_prompt_tokens": _optional_non_negative_int(
                    raw.get("cached_prompt_tokens")
                ),
                "request_hash": str(raw.get("request_hash", "") or ""),
                "request_estimated_tokens": max(
                    0, int(raw.get("request_estimated_tokens", 0) or 0)
                ),
                "adjacent_common_prefix_tokens": max(
                    0, int(raw.get("adjacent_common_prefix_tokens", 0) or 0)
                ),
                "adjacent_prefix_hash": str(
                    raw.get("adjacent_prefix_hash", "") or ""
                ),
                "provider_cache_hit": bool(
                    raw.get("cached_prompt_tokens") is not None
                    and int(raw.get("cached_prompt_tokens", 0) or 0) > 0
                ),
                "usage_present": usage_present,
                "success": success,
                "provider_request_id": str(raw.get("provider_request_id", "") or ""),
                "usage_unknown": bool(
                    raw.get("usage_unknown", not success and not usage_present)
                ),
            }
            if not success:
                if error is not None:
                    payload.setdefault("failure_type", error.__class__.__name__)
                    payload.setdefault(
                        "failure_status", getattr(error, "status_code", None)
                    )
                    payload.setdefault(
                        "provider_code", str(getattr(error, "code", "") or "")
                    )
                else:
                    payload.setdefault("failure_type", str(raw.get("failure_type", "")))
                    payload.setdefault("failure_status", raw.get("failure_status"))
                    payload.setdefault("provider_code", str(raw.get("provider_code", "")))
            self._trace_event_writer(EventType.MODEL_CALL, "provider_attempt", payload)

    async def _retry_submit_review_validation_repair(
        self,
        *,
        messages: list[Message],
        request: ReviewRequest,
        tool_schemas: list[dict[str, Any]],
        validation_error: str,
        iteration: int,
        prior_history_start: int,
        prior_history_count: int,
        invalid_tool_calls: list[dict[str, Any]],
        stage: str = "submit_only",
    ) -> tuple[AnalysisPlan, ModelResponse, dict[str, Any], str]:
        for raw_call in invalid_tool_calls:
            call_id = str(raw_call.get("id", "")).strip()
            if call_id:
                self._conversation.add_tool_result(
                    call_id,
                    {
                        "ok": False,
                        "error_type": "validation_error",
                        "message": validation_error,
                    },
                )
        prior_history_end = prior_history_start + prior_history_count
        repair_messages = [
            *messages[:prior_history_start],
            *self._conversation.messages(),
            *messages[prior_history_end:],
            Message(
                role="user",
                content=(
                    "Your previous submit_review tool call was rejected by schema validation. "
                    "Call submit_review again as your only action, preserving supported findings "
                    "but fixing this exact validation error:\n"
                    f"{validation_error}\n"
                    "The submit_review function arguments must directly contain top-level "
                    "summary and issues fields; do not wrap them inside an arguments object."
                ),
            ),
        ]
        config = self._build_submit_config(request)
        policy = ModelCallPolicy(thinking="off", forced_tool="submit_review")
        response = await self._chat_with_telemetry(
            messages=repair_messages,
            config=config,
            tools=self._submit_only_tools(tool_schemas, request),
            policy=policy,
            iteration=iteration,
            stage=stage,
            force_submit=True,
        )
        response_id = self._persist_model_response(response, iteration)
        plan, parse_meta = self._parse_tool_calls(
            response.tool_calls, request, force_submit=True
        )
        parse_meta["tool_choice"] = self._trace_tool_choice(config)
        parse_meta["thinking_disabled"] = True
        return plan, response, parse_meta, response_id

    def _persist_model_response(self, response: ModelResponse, iteration: int) -> str:
        """Persist a provider response before parsing, fallback, or validation."""

        if self._model_response_writer is None:
            return ""
        return self._model_response_writer(response, iteration)

    def _build_submit_config(
        self, request: ReviewRequest | DebugRequest
    ) -> ModelConfig:
        return self._model_client.default_config.model_copy(
            update={
                "max_tokens": _SUBMIT_MAX_TOKENS,
            }
        )

    @staticmethod
    def _submit_tool_name(request: ReviewRequest | DebugRequest) -> str:
        return "submit_review" if isinstance(request, ReviewRequest) else "submit_debug"

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
        draft_finding_calls: list[DraftFindingInput] = []
        draft_review: ReviewReport | None = None
        draft_debug: DebugResponse | None = None
        parse_meta: dict[str, Any] = {
            "submit_review_seen": False,
            "submit_debug_seen": False,
            "submit_review_validation_error": "",
            "submit_review_arguments_normalized": False,
            "submit_debug_validation_error": "",
            "draft_finding_validation_errors": [],
            "valid_draft_call_ids": [],
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

            if name == "record_draft_finding":
                if force_submit or not isinstance(request, ReviewRequest):
                    parse_meta["force_submit_discarded_count"] += int(force_submit)
                    continue
                if argument_error or not isinstance(payload, dict):
                    error = argument_error or (
                        "Invalid record_draft_finding arguments type: "
                        f"{type(payload).__name__}"
                    )
                    parse_meta["draft_finding_validation_errors"].append(error)
                    logger.warning("Invalid draft finding ignored: %s", error)
                    continue
                try:
                    draft_finding_calls.append(
                        DraftFindingInput.model_validate(payload)
                    )
                    parse_meta["valid_draft_call_ids"].append(
                        str(raw.get("id", "")).strip()
                    )
                except ValidationError as exc:
                    parse_meta["draft_finding_validation_errors"].append(str(exc))
                    logger.warning("Invalid draft finding ignored: %s", exc)
                continue
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
                payload, arguments_normalized = (
                    self._normalize_nested_submit_review_arguments(payload)
                )
                parse_meta["submit_review_arguments_normalized"] = bool(
                    parse_meta["submit_review_arguments_normalized"]
                    or arguments_normalized
                )
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
                    draft_finding_calls=draft_finding_calls,
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

    def _complete_invalid_draft_tool_calls(
        self,
        raw_calls: list[dict[str, Any]],
        parse_meta: dict[str, Any],
    ) -> None:
        """Satisfy rejected pseudo-calls so provider replay remains complete."""

        valid_ids = set(parse_meta.get("valid_draft_call_ids", []))
        for raw in raw_calls:
            function = raw.get("function") if isinstance(raw, dict) else None
            if not isinstance(function, dict):
                continue
            if function.get("name") != "record_draft_finding":
                continue
            call_id = str(raw.get("id", "")).strip()
            if not call_id or call_id in valid_ids:
                continue
            self._conversation.add_tool_result(
                call_id,
                {
                    "ok": False,
                    "recorded": False,
                    "error_type": "validation_error",
                },
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
            payload_error = self._validate_submit_review_payload(payload)
            if payload_error:
                logger.warning("Invalid fallback review JSON ignored: %s", payload_error)
                return None
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
    def _normalize_nested_submit_review_arguments(
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Unwrap one exact provider-style arguments envelope for submit_review."""

        if set(payload) != {"arguments"}:
            return payload, False
        nested = payload.get("arguments")
        if not isinstance(nested, dict) or not {"summary", "issues"}.issubset(nested):
            return payload, False
        return nested, True

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
        for index, issue in enumerate(payload["issues"]):
            if isinstance(issue, dict) and "confidence" not in issue:
                return (
                    "Invalid submit_review payload: "
                    f"issues[{index}] missing required confidence"
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

    @classmethod
    def _build_tool_feedback_messages(
        cls,
        tool_feedback: list[dict[str, Any]],
        *,
        selected_file_complete_lines: dict[str, int] | None = None,
    ) -> list[Message]:
        messages: list[Message] = []
        selected_file_complete_lines = selected_file_complete_lines or {}
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
                if cls._prefetch_covered_by_selected_file(
                    raw_tool_call,
                    result_payload,
                    selected_file_complete_lines,
                ):
                    continue
                result_payload = InferenceEngine._compact_synthetic_context_payload(
                    result_payload
                )
                messages.append(
                    Message(
                        role="user",
                        content=(
                            f"{iter_tag}prefetched_tool_context: "
                            + serialize_json(
                                {
                                    "tool": function_block.get("name", "unknown"),
                                    "arguments": function_block.get("arguments", "{}"),
                                    "result": result_payload,
                                }
                            )
                        ),
                    )
                )
                continue

        return messages

    @classmethod
    def _prefetch_covered_by_selected_file(
        cls,
        raw_tool_call: dict[str, Any],
        result_payload: dict[str, Any],
        selected_file_complete_lines: dict[str, int],
    ) -> bool:
        entry = cls._prefetch_coverage_entry(
            raw_tool_call,
            result_payload,
            selected_file_complete_lines,
        )
        return bool(entry and entry["covered_by_file_context"])

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
            "available_draft_finding_count": 0,
            "included_draft_finding_count": 0,
            "available_tool_result_count": 0,
            "included_tool_result_count": 0,
            "available_concern_count": 0,
            "included_concern_count": 0,
            "validator_result_included": 0,
            "manifest_span_count": 0,
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
        draft_findings: list[DraftFinding],
        *,
        validator_result: dict[str, Any] | None = None,
        candidate_context_manifests: list[dict[str, Any]] | None = None,
        token_budget: int,
    ) -> tuple[Message | None, dict[str, int]]:
        """Build a bounded, deduplicated evidence handoff for submit-only calls."""

        telemetry = cls._empty_final_evidence_telemetry(token_budget)
        candidates: list[tuple[str, str]] = []
        seen_tools: set[str] = set()

        for draft in draft_findings:
            telemetry["available_draft_finding_count"] += 1
            location = draft.file
            if draft.line is not None:
                location += f":{draft.line}"
            if draft.symbol:
                location += f" ({draft.symbol})"
            candidates.append(
                (
                    "draft",
                    f"- {draft.id}: {location}\n  claim: {draft.claim}",
                )
            )

        if validator_result:
            candidates.append(
                (
                    "validator",
                    "- validator_result: "
                    + cls._json_preview(
                        cls._compact_validator_result(validator_result), 1800
                    ),
                )
            )

        manifest_evidence = cls._manifest_evidence_for_drafts(
            candidate_context_manifests or [], draft_findings
        )
        for evidence in manifest_evidence:
            candidates.append(("manifest", "- " + evidence))

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
            "Known draft findings are investigation hypotheses, not automatic final "
            "findings. Decide whether retained evidence supports submitting each one. "
            "Do not discard a supported concern merely because the exploration turn "
            "ended at the length limit.",
        ]
        if draft_findings:
            lines.append("Known draft findings:")
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
            if kind == "draft":
                telemetry["included_draft_finding_count"] += 1
            elif kind == "tool":
                telemetry["included_tool_result_count"] += 1
            elif kind == "validator":
                telemetry["validator_result_included"] += 1
            elif kind == "manifest":
                telemetry["manifest_span_count"] += 1
            else:
                telemetry["included_concern_count"] += 1
            if shortened:
                break

        telemetry["truncated_count"] = len(candidates) - full_included
        content = "\n".join(lines)
        telemetry["estimated_tokens"] = builder.estimate_tokens(content)
        return Message(role="user", content=content), telemetry

    @staticmethod
    def _compact_validator_result(result: dict[str, Any]) -> dict[str, Any]:
        """Keep only validator decisions needed by a submit-only reviewer."""

        compact: dict[str, Any] = {}
        for key in (
            "validated_draft_ids",
            "validated_finding_ids",
            "validator_passed",
            "submit_allowed",
            "effective_issue_count",
            "unresolved_evidence_gaps",
            "policy_warnings",
        ):
            if key in result:
                compact[key] = result[key]
        issue_results = result.get("issue_results")
        if isinstance(issue_results, list):
            compact["issue_results"] = [
                {
                    key: item[key]
                    for key in (
                        "original_index",
                        "normalized_location",
                        "severity",
                        "passes_current_filter",
                        "fail_reasons",
                    )
                    if key in item
                }
                for item in issue_results[:16]
                if isinstance(item, dict)
            ]
        return compact

    @staticmethod
    def _manifest_evidence_for_drafts(
        manifests: list[dict[str, Any]], drafts: list[DraftFinding]
    ) -> list[str]:
        """Select small manifest id/hash-bound spans relevant to known drafts."""

        if not manifests or not drafts:
            return []
        output: list[str] = []
        for manifest in manifests:
            manifest_id = str(manifest.get("candidate_id", "")).strip()
            if not manifest_id:
                continue
            raw_spans = manifest.get("included_spans", [])
            if not isinstance(raw_spans, list):
                continue
            selected: list[dict[str, Any]] = []
            for span in raw_spans:
                if not isinstance(span, dict):
                    continue
                path = str(span.get("file", "")).replace("\\", "/").lstrip("./")
                try:
                    start = int(span.get("start_line", 0) or 0)
                    end = int(span.get("end_line", start) or start)
                except (TypeError, ValueError):
                    continue
                if any(
                    draft.file.replace("\\", "/").lstrip("./") == path
                    and (draft.line is None or start <= draft.line <= end)
                    for draft in drafts
                ):
                    content = str(span.get("content", "") or "")
                    if len(content) > 640:
                        content = content[:640].rstrip() + "\n...[truncated]"
                    selected.append(
                        {
                            "file": path,
                            "start_line": start,
                            "end_line": end,
                            "symbol_id": span.get("symbol_id", ""),
                            "role": span.get("role", ""),
                            "content": content,
                            "retrieval_source": span.get("retrieval_source", ""),
                            "context_hash": span.get("context_hash", ""),
                        }
                    )
                if len(selected) >= 3:
                    break
            if selected:
                output.append(
                    "manifest_evidence manifest_id="
                    + manifest_id
                    + " spans="
                    + serialize_json(selected)
                )
        return output

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
        serialized = serialize_json(parsed)
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
            serialized = serialize_json(parsed)
        except Exception:  # noqa: BLE001
            serialized = str(parsed)
        if len(serialized) <= max_chars:
            return serialized
        return serialized[: max(0, max_chars - 14)] + "...[truncated]"

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
                "draft_finding_call_count": len(plan.draft_finding_calls),
                "draft_finding_validation_errors": parse_meta.get(
                    "draft_finding_validation_errors", []
                ),
                "submit_review_seen": bool(parse_meta.get("submit_review_seen")),
                "submit_debug_seen": bool(parse_meta.get("submit_debug_seen")),
                "submit_review_validation_error": self._trace_recorder.build_text_preview(
                    str(parse_meta.get("submit_review_validation_error", ""))
                ),
                "submit_review_arguments_normalized": bool(
                    parse_meta.get("submit_review_arguments_normalized")
                ),
                "submit_debug_validation_error": self._trace_recorder.build_text_preview(
                    str(parse_meta.get("submit_debug_validation_error", ""))
                ),
                "location_warnings": parse_meta.get("location_warnings", []),
                "fallback_json_found": fallback_json_found,
                "fallback_parse_valid": fallback_parse_valid,
                "incomplete_reason": parse_meta.get("incomplete_reason", ""),
                "recovery_required": plan.recovery_required,
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
        config: ModelConfig,
        policy: ModelCallPolicy,
        file_contents: dict[str, str],
        tool_feedback: list[dict[str, Any]],
        conversation_history_count: int,
        iteration: int,
        prompt_input_token_budget: int,
        base_context_token_budget: int,
        final_submit_feedback_token_budget: int,
        final_evidence_telemetry: dict[str, int],
        force_submit: bool,
        stage: str = "explore",
        relation_graph_summary: dict[str, Any] | None = None,
    ) -> None:
        if self._trace_event_writer is None:
            return
        builder = ContextBuilder()
        message_tokens = sum(builder.estimate_tokens(item.content) for item in messages)
        tool_schema_json = serialize_json(tools)
        tool_schema_tokens = builder.estimate_tokens(tool_schema_json)
        message_shapes = [
            {
                "index": index,
                "role": item.role,
                "chars": len(item.content),
                "estimated_tokens": builder.estimate_tokens(item.content),
                "component": self._message_component(item),
            }
            for index, item in enumerate(messages)
        ]
        role_counts = {
            role: sum(item.role == role for item in messages)
            for role in ("system", "user", "assistant", "tool")
        }
        role_chars = {
            role: sum(len(item.content) for item in messages if item.role == role)
            for role in role_counts
        }
        tool_shapes = [self._tool_schema_shape(item, builder) for item in tools]
        wire_messages = [self._safe_wire_message(item) for item in messages]
        assembled_request_text = serialize_json(
            {
                "model": config.model,
                "messages": wire_messages,
                "temperature": config.temperature,
                "max_tokens": config.max_tokens,
                "top_p": config.top_p,
                "tools": tools,
                "tool_choice": config.tool_choice,
                "extra_body": config.extra_body,
                "thinking": policy.thinking,
                "forced_tool": policy.forced_tool,
            }
        )
        assembled_request_chars = len(assembled_request_text)
        assembled_request_tokens = estimate_tokens(assembled_request_text)
        component_records = self._build_component_records(
            messages=messages,
            tools=tools,
            tool_feedback=tool_feedback,
            relation_graph_summary=relation_graph_summary or {},
            wire_messages=wire_messages,
            assembled_request_text=assembled_request_text,
        )
        component_token_sum = sum(
            int(item["estimated_tokens"])
            for item in component_records
            if item["component"] != "assembled_request_total"
        )
        prefetch_coverage = self._measure_prefetch_coverage(
            file_contents,
            tool_feedback,
            selected_file_complete_lines=context_telemetry.get(
                "selected_file_complete_lines", {}
            ),
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
                "assembled_request_estimated_tokens": assembled_request_tokens,
                "component_token_sum": component_token_sum,
                "assembled_envelope_overhead_tokens": max(
                    0, assembled_request_tokens - component_token_sum
                ),
                "tokenizer": "cl100k_base",
                "message_count": len(messages),
                "message_count_by_role": role_counts,
                "message_chars": sum(len(item.content) for item in messages),
                "message_chars_by_role": role_chars,
                "message_shapes": message_shapes,
                "conversation_history_count": conversation_history_count,
                "tool_schema_chars": len(tool_schema_json),
                "tool_schema_shapes": tool_shapes,
                "assembled_request_chars": assembled_request_chars,
                "component_records": component_records,
                "max_output_tokens": config.max_tokens,
                "thinking": policy.thinking,
                "stage": stage,
                "forced_tool": policy.forced_tool or "none",
                "prefetch_coverage": prefetch_coverage,
                "force_submit": force_submit,
                "final_submit_evidence": final_evidence_telemetry,
                "tool_schema_count": len(tools),
                **context_telemetry,
            },
        )

    @staticmethod
    def _safe_wire_message(message: Message) -> dict[str, Any]:
        """Serialize message fields used for input sizing without transient thinking."""

        item: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }
        if message.tool_call_id is not None:
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = message.tool_calls
        return item

    @classmethod
    def _build_component_records(
        cls,
        *,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_feedback: list[dict[str, Any]],
        relation_graph_summary: dict[str, Any],
        wire_messages: list[dict[str, Any]],
        assembled_request_text: str,
    ) -> list[dict[str, Any]]:
        """Build size/hash records for all visible request components."""

        by_component: dict[str, list[str]] = {}
        for message in messages:
            component = cls._message_component(message)
            by_component.setdefault(component, []).append(message.content)

        records: list[dict[str, Any]] = []

        def add(name: str, text: str) -> None:
            records.append(token_component(name, text))

        add("system", "\n".join(by_component.get("system", [])))
        review_payload = "\n".join(by_component.get("review_payload", []))
        if review_payload:
            payload_start = review_payload.find("{")
            if payload_start >= 0:
                review_payload = review_payload[payload_start:]
        add("review_payload", review_payload)

        graph_manifests: list[Any] = []
        graph_paths: list[Any] = []
        if review_payload:
            try:
                decoded = json.loads(review_payload)
            except json.JSONDecodeError:
                decoded = {}
            if isinstance(decoded, dict):
                raw_manifests = decoded.get("candidate_context_manifests", [])
                if isinstance(raw_manifests, list):
                    for manifest in raw_manifests:
                        if not isinstance(manifest, dict):
                            continue
                        graph_manifests.append(manifest)
                        raw_paths = manifest.get("included_graph_paths", [])
                        if isinstance(raw_paths, list):
                            graph_paths.extend(
                                {"candidate_id": manifest.get("candidate_id"), "path": path}
                                for path in raw_paths
                                if isinstance(path, dict)
                            )
        add("graph_manifest_projection", serialize_json(graph_manifests))
        add("graph_path_projection", serialize_json(graph_paths))
        add("relation_graph_summary", serialize_json(relation_graph_summary))
        add(
            "conversation_history",
            serialize_json(
                [
                    message
                    for message in wire_messages
                    if message.get("role") in {"assistant", "tool"}
                ]
            ),
        )
        add("tool_feedback", serialize_json(tool_feedback))
        add(
            "final_submit_evidence",
            "\n".join(by_component.get("final_submit_evidence", [])),
        )
        add(
            "defer_notice",
            "\n".join(by_component.get("defer_submit_notice", [])),
        )
        add(
            "finalize_notice",
            "\n".join(by_component.get("finalize_notice", [])),
        )
        add(
            "near_last_notice",
            "\n".join(by_component.get("near_last_notice", [])),
        )
        add("tool_schemas", serialize_json(tools))
        add("assembled_request_total", assembled_request_text)
        return records

    @staticmethod
    def _message_component(message: Message) -> str:
        if message.role == "system":
            return "system"
        content = message.content
        if "prefetched_tool_context:" in content:
            return "prefetch_context"
        if content.startswith("Do not call submit_review yet."):
            return "defer_submit_notice"
        if content.startswith("final_submit_evidence_summary:"):
            return "final_submit_evidence"
        if content.startswith("FINAL CALL"):
            return "finalize_notice"
        if content.startswith("Note: you are at the last allowed iteration"):
            return "near_last_notice"
        if content.startswith("Review the payload"):
            return "review_payload"
        if content.startswith("Return tool calls if needed"):
            return "debug_payload"
        return "conversation_or_feedback"

    @staticmethod
    def _tool_schema_shape(
        schema: dict[str, Any], builder: ContextBuilder
    ) -> dict[str, Any]:
        serialized = serialize_json(schema)
        function = schema.get("function", {})
        name = (
            function.get("name", "unknown") if isinstance(function, dict) else "unknown"
        )
        return {
            "name": str(name),
            "chars": len(serialized),
            "estimated_tokens": builder.estimate_tokens(serialized),
        }

    @classmethod
    def _measure_prefetch_coverage(
        cls,
        file_contents: dict[str, str],
        tool_feedback: list[dict[str, Any]],
        *,
        selected_file_complete_lines: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        selected_file_complete_lines = selected_file_complete_lines or {}
        for item in tool_feedback:
            raw_call = item.get("tool_call")
            if (
                not isinstance(raw_call, dict)
                or raw_call.get("synthetic_context") is not True
            ):
                continue
            function = raw_call.get("function")
            if not isinstance(function, dict) or function.get("name") != "read_file":
                continue
            result_payload = cls._tool_result_payload(item.get("result"))
            entry = cls._prefetch_coverage_entry(
                raw_call,
                result_payload,
                selected_file_complete_lines,
                file_contents=file_contents,
            )
            if entry is not None:
                entries.append(entry)
        return {
            "entry_count": len(entries),
            "covered_entry_count": sum(
                bool(item["covered_by_file_context"]) for item in entries
            ),
            "covered_prefetch_content_chars": sum(
                int(item["prefetch_content_chars"])
                for item in entries
                if item["covered_by_file_context"]
            ),
            "suppressed_entry_count": sum(
                bool(item["covered_by_file_context"]) for item in entries
            ),
            "entries": entries,
        }

    @staticmethod
    def _prefetch_coverage_entry(
        raw_tool_call: dict[str, Any],
        result_payload: dict[str, Any],
        selected_file_complete_lines: dict[str, int],
        *,
        file_contents: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        function = raw_tool_call.get("function")
        if not isinstance(function, dict) or function.get("name") != "read_file":
            return None
        try:
            arguments = json.loads(str(function.get("arguments", "{}")))
        except json.JSONDecodeError:
            arguments = {}
        path = str(arguments.get("file_path", "")).replace("\\", "/")
        while path.startswith("./"):
            path = path[2:]
        path = path.lstrip("/")
        data = result_payload.get("data")
        if result_payload.get("ok") is not True or not isinstance(data, dict):
            return None
        start_line = int(data.get("start_line", 0) or 0)
        line_count = int(data.get("line_count", 0) or 0)
        end_line = start_line + line_count - 1 if start_line and line_count else 0
        loaded_complete_lines = int(selected_file_complete_lines.get(path, 0) or 0)
        covered = bool(end_line and end_line <= loaded_complete_lines)
        content = data.get("content")
        loaded = (file_contents or {}).get(path, "")
        return {
            "file": path,
            "start_line": start_line,
            "end_line": end_line,
            "prefetch_content_chars": len(content) if isinstance(content, str) else 0,
            "loaded_file_chars": len(loaded),
            "loaded_complete_lines": loaded_complete_lines,
            "covered_by_file_context": covered,
        }

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
            },
        )

    @staticmethod
    def _length_incomplete_reason(
        response: ModelResponse,
        plan: AnalysisPlan,
    ) -> str:
        if response.finish_reason != "length":
            return ""
        if plan.draft_review is not None and (
            plan.draft_review.summary.strip() or plan.draft_review.issues
        ):
            return ""
        if plan.draft_debug is not None:
            return ""
        return "model_finish_reason_length_no_submit"

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
            },
        )
