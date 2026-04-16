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
    "Each issue must include severity, location, evidence, and suggestion."
)
SYSTEM_PROMPT_DEBUG = (
    "You are a senior debugging assistant. Produce structured hypotheses and steps."
)

USER_PREFIX_REVIEW = (
    "Review the payload and call submit_review exactly once with final JSON. "
    "Do not return plain-text-only final answers.\n"
)
USER_PREFIX_DEBUG = (
    "Return tool calls if needed, then submit_debug with final JSON.\n"
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
