"""Prompt templates and message builders for inference."""

from __future__ import annotations

import json

from src.analyzer.context_builder import ContextBuilder
from src.analyzer.context_priority import (
    assemble_debug_payload,
    assemble_review_payload,
    build_debug_context_parts,
    build_review_context_parts,
)
from src.analyzer.context_state import ContextState
from src.analyzer.schemas import DebugRequest, ReviewRequest
from src.models.schemas import Message

SYSTEM_PROMPT_REVIEW = (
    "You are a senior code reviewer. Produce structured, actionable findings."
)
SYSTEM_PROMPT_DEBUG = (
    "You are a senior debugging assistant. Produce structured hypotheses and steps."
)

USER_PREFIX_REVIEW = (
    "Return tool calls if needed, then submit_review with final JSON.\n"
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
