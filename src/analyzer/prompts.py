"""Prompt templates and message builders for inference."""

from __future__ import annotations

import json

from src.analyzer.context_state import ContextState
from src.analyzer.schemas import DebugRequest, ReviewRequest
from src.models.schemas import Message

SYSTEM_PROMPT_REVIEW = (
    "You are a senior code reviewer. Produce structured, actionable findings."
)
SYSTEM_PROMPT_DEBUG = (
    "You are a senior debugging assistant. Produce structured hypotheses and steps."
)

def build_review_messages(
    request: ReviewRequest,
    context: ContextState,
    diff: str,
    file_contents: dict[str, str],
) -> list[Message]:
    """Build review-mode messages."""
    payload = {
        "repo_path": request.repo_path,
        "diff_mode": request.diff_mode,
        "diff_text": request.diff_text,
        "diff_loaded": diff,
        "files": file_contents,
        "constraints": context.constraints,
    }
    return [
        Message(role="system", content=SYSTEM_PROMPT_REVIEW),
        Message(
            role="user",
            content="Return tool calls if needed, then submit_review with final JSON.\n"
            + json.dumps(payload, ensure_ascii=True),
        ),
    ]


def build_debug_messages(
    request: DebugRequest,
    context: ContextState,
    error_log: str,
    file_contents: dict[str, str],
) -> list[Message]:
    """Build debug-mode messages."""
    payload = {
        "repo_path": request.repo_path,
        "error_log_path": request.error_log_path,
        "error_log_text": request.error_log_text,
        "error_log_loaded": error_log,
        "files": file_contents,
        "constraints": context.constraints,
    }
    return [
        Message(role="system", content=SYSTEM_PROMPT_DEBUG),
        Message(
            role="user",
            content="Return tool calls if needed, then submit_debug with final JSON.\n"
            + json.dumps(payload, ensure_ascii=True),
        ),
    ]
