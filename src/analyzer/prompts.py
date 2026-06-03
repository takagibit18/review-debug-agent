"""Prompt templates and message builders for inference."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from src.analyzer.context_builder import ContextBuilder
from src.analyzer.context_compressor import ContextCompressor
from src.analyzer.context_priority import (
    assemble_debug_payload,
    assemble_review_payload,
    build_debug_context_parts,
    build_review_context_parts,
)
from src.analyzer.context_state import ContextState
from src.analyzer.schemas import DebugRequest, ReviewRequest
from src.models.schemas import Message

if TYPE_CHECKING:
    from src.models.client import ModelClient

SYSTEM_PROMPT_REVIEW = (
    "You are a senior code reviewer. Analyze the provided diff/files and return structured, "
    "actionable findings. The final answer must be submitted via the submit_review tool. "
    "Use only these severity values: critical, warning, info, style. "
    "Each issue must include severity, location, evidence, suggestion, and confidence between 0 and 1. "
    "Location must be canonical: path[:line[-end_line]], using repo-relative forward-slash paths. "
    "Do not use free-form natural language for location. "
    "Evidence must cite the concrete changed diff lines or hunk that support the claim. "
    "The summary must not mention bugs, regressions, breaking changes, compatibility risks, "
    "or user-visible behavior change unless there is a corresponding issue in issues[] "
    "with concrete location and evidence. "
    "For concrete changed-code bugs, regressions, compatibility breaks, or user-visible behavior changes, "
    "use confidence >= 0.85; reserve lower confidence for speculative or non-blocking concerns. "
    "Do not label something critical unless the diff itself clearly supports it. "
    "Check for silent behavior changes: fallback paths, coercion, or exception handling "
    "can make inputs that previously failed, errored, or were rejected quietly succeed. "
    "Pay special attention to cross-type comparison, precision semantics, boundary values, "
    "and error exposure semantics. Tests can show intent, but if users receive changed "
    "behavior without a user-visible warning or explicit documentation, report at least "
    "a warning with concrete changed-line evidence. "
    "Report one issue per independent root cause. If a downstream symptom, cache/keying "
    "effect, or behavioral consequence comes from the same defect, include it as evidence "
    "or suggestion in the same issue instead of creating another warning. "
    "Do not promote consequences of a hypothetical fix into a separate issue; keep them "
    "inside the suggestion unless the current diff already creates that independent risk. "
    "When paths are uncertain, use list_dir first before glob/grep/read_file. "
    "After any Directory/File not found error, validate parent directory first and avoid blind retries."
)
SYSTEM_PROMPT_DEBUG = (
    "You are a senior debugging assistant. Produce structured hypotheses and steps. "
    "When paths are uncertain, use list_dir first before glob/grep/read_file. "
    "After any Directory/File not found error, validate parent directory first and avoid blind retries. "
    "Use canonical step locations: path[:line[-end_line]] whenever location is provided."
)

USER_PREFIX_REVIEW = (
    "Review the payload and call submit_review exactly once with final JSON. "
    "Prioritize concrete bugs/regressions over optimization advice. "
    "If evidence cannot point to a specific diff snippet, lower the severity instead of forcing a bug claim. "
    "If you mention a bug, regression, compatibility break, or user-visible behavior change in the summary, "
    "also include the same finding as an issue in issues[]. "
    "If that issue is backed by a concrete changed line or hunk, use confidence >= 0.85. "
    "If the payload shows truncated files/context, prioritize path exploration and targeted reads. "
    "Do not return plain-text-only final answers.\n"
)
USER_PREFIX_DEBUG = (
    "Return tool calls if needed, then submit_debug with final JSON. "
    "If path is unknown, call list_dir first.\n"
)

FINALIZE_REVIEW_NOTICE = (
    "FINAL CALL — this is your last opportunity to respond. You MUST call submit_review "
    "as your FIRST and ONLY action. Do NOT output any reasoning, analysis, or prose text "
    "before the tool call — go directly to submit_review with the best conclusions you can "
    "derive from the accumulated tool feedback. Do NOT request any additional tools. "
    "If the accumulated evidence only supports speculative, info/style/design, or "
    "non-blocking suggestions, submit issues: [] with an honest summary. "
    "The summary must not mention bugs, regressions, breaking changes, compatibility risks, "
    "or user-visible behavior change unless there is a corresponding issue in issues[]. "
    "For concrete changed-code bugs, regressions, compatibility breaks, or user-visible behavior changes, "
    "use confidence >= 0.85; reserve lower confidence for speculative or non-blocking concerns. "
    "Before submitting an empty review, re-check whether any fallback path, coercion, or exception handling "
    "creates a silent behavior change in cross-type comparison, precision, boundary-value, "
    "or error exposure semantics. If users receive changed behavior without a user-visible warning "
    "or explicit documentation, report at least a warning with concrete changed-line evidence. "
    "Before submitting multiple issues, merge findings that share the same independent root cause; "
    "a downstream symptom or cache/keying effect should stay in the same issue. "
    "Do not promote a hypothetical fix side effect into its own warning unless the current diff "
    "already creates that separate risk. "
    "If uncertain, return whatever partial findings are supported by what was already read; "
    "an empty issues list is acceptable with an honest summary."
)
FINALIZE_DEBUG_NOTICE = (
    "FINAL CALL — this is your last opportunity to respond. You MUST call submit_debug "
    "as your FIRST and ONLY action. Do NOT output any reasoning, analysis, or prose text "
    "before the tool call — go directly to submit_debug with the best hypotheses and steps "
    "you can derive from the accumulated tool feedback. Do NOT request any additional tools."
)


def build_review_messages(
    request: ReviewRequest,
    context: ContextState,
    diff: str,
    file_contents: dict[str, str],
    *,
    prompt_token_budget: int | None = None,
    context_builder: ContextBuilder | None = None,
    project_structure: str | None = None,
) -> list[Message]:
    """Build review-mode messages with optional priority truncation of payload parts."""
    cb = context_builder or ContextBuilder()
    all_parts = build_review_context_parts(
        request, context, diff, file_contents, project_structure
    )
    if prompt_token_budget is not None:
        selected = cb.truncate_context(all_parts, prompt_token_budget)
    else:
        selected = all_parts
    payload = assemble_review_payload(request, context, all_parts, selected)
    return [
        Message(role="system", content=SYSTEM_PROMPT_REVIEW),
        Message(
            role="user",
            content=USER_PREFIX_REVIEW + json.dumps(payload, ensure_ascii=True),
        ),
    ]


async def build_review_messages_async(
    request: ReviewRequest,
    context: ContextState,
    diff: str,
    file_contents: dict[str, str],
    *,
    prompt_token_budget: int | None = None,
    context_builder: ContextBuilder | None = None,
    project_structure: str | None = None,
    compressor_model_client: ModelClient | None = None,
    summary_enabled: bool = False,
    summary_max_tokens_per_part: int = 1000,
    summary_model_name: str = "",
) -> list[Message]:
    """Build review-mode messages with optional second-layer summary compaction."""
    cb = context_builder or ContextBuilder()
    all_parts = build_review_context_parts(
        request, context, diff, file_contents, project_structure
    )
    if prompt_token_budget is None:
        selected = all_parts
    elif summary_enabled and compressor_model_client is not None:
        selected, _ = await cb.truncate_with_summary(
            all_parts,
            prompt_token_budget,
            compressor=ContextCompressor(compressor_model_client),
            model_name=summary_model_name,
            max_summary_tokens=summary_max_tokens_per_part,
        )
    else:
        selected = cb.truncate_context(all_parts, prompt_token_budget)
    payload = assemble_review_payload(request, context, all_parts, selected)
    return [
        Message(role="system", content=SYSTEM_PROMPT_REVIEW),
        Message(
            role="user",
            content=USER_PREFIX_REVIEW + json.dumps(payload, ensure_ascii=True),
        ),
    ]


def build_debug_messages(
    request: DebugRequest,
    context: ContextState,
    error_log: str,
    file_contents: dict[str, str],
    *,
    prompt_token_budget: int | None = None,
    context_builder: ContextBuilder | None = None,
    project_structure: str | None = None,
) -> list[Message]:
    """Build debug-mode messages with optional priority truncation of payload parts."""
    cb = context_builder or ContextBuilder()
    all_parts = build_debug_context_parts(
        request, context, error_log, file_contents, project_structure
    )
    if prompt_token_budget is not None:
        selected = cb.truncate_context(all_parts, prompt_token_budget)
    else:
        selected = all_parts
    payload = assemble_debug_payload(request, context, all_parts, selected)
    return [
        Message(role="system", content=SYSTEM_PROMPT_DEBUG),
        Message(
            role="user",
            content=USER_PREFIX_DEBUG + json.dumps(payload, ensure_ascii=True),
        ),
    ]


async def build_debug_messages_async(
    request: DebugRequest,
    context: ContextState,
    error_log: str,
    file_contents: dict[str, str],
    *,
    prompt_token_budget: int | None = None,
    context_builder: ContextBuilder | None = None,
    project_structure: str | None = None,
    compressor_model_client: ModelClient | None = None,
    summary_enabled: bool = False,
    summary_max_tokens_per_part: int = 1000,
    summary_model_name: str = "",
) -> list[Message]:
    """Build debug-mode messages with optional second-layer summary compaction."""
    cb = context_builder or ContextBuilder()
    all_parts = build_debug_context_parts(
        request, context, error_log, file_contents, project_structure
    )
    if prompt_token_budget is None:
        selected = all_parts
    elif summary_enabled and compressor_model_client is not None:
        selected, _ = await cb.truncate_with_summary(
            all_parts,
            prompt_token_budget,
            compressor=ContextCompressor(compressor_model_client),
            model_name=summary_model_name,
            max_summary_tokens=summary_max_tokens_per_part,
        )
    else:
        selected = cb.truncate_context(all_parts, prompt_token_budget)
    payload = assemble_debug_payload(request, context, all_parts, selected)
    return [
        Message(role="system", content=SYSTEM_PROMPT_DEBUG),
        Message(
            role="user",
            content=USER_PREFIX_DEBUG + json.dumps(payload, ensure_ascii=True),
        ),
    ]
