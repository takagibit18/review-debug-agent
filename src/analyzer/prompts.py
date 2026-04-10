"""Prompt templates and message builders for inference."""

from __future__ import annotations

import json
from typing import Any

from src.analyzer.context_state import ContextState
from src.analyzer.schemas import DebugRequest, ReviewRequest
from src.models.schemas import Message
from src.tools.base import ToolSpec

SYSTEM_PROMPT_REVIEW = (
    "You are a senior code reviewer. Produce structured, actionable findings."
)
SYSTEM_PROMPT_DEBUG = (
    "You are a senior debugging assistant. Produce structured hypotheses and steps."
)


def build_tool_schemas(specs: list[ToolSpec]) -> list[dict[str, Any]]:
    """Convert ToolSpec objects to OpenAI function-calling schema."""
    schemas: list[dict[str, Any]] = []
    for spec in specs:
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters or {"type": "object", "properties": {}},
                },
            }
        )
    return schemas


def build_submit_tool_schemas() -> list[dict[str, Any]]:
    """Pseudo-tools used for structured final output submission."""
    return [
        {
            "type": "function",
            "function": {
                "name": "submit_review",
                "description": "Submit structured review output.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "issues": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "severity": {"type": "string"},
                                    "location": {"type": "string"},
                                    "evidence": {"type": "string"},
                                    "suggestion": {"type": "string"},
                                    "confidence": {"type": "number"},
                                },
                                "required": [
                                    "severity",
                                    "location",
                                    "evidence",
                                    "suggestion",
                                ],
                            },
                        },
                    },
                    "required": ["summary"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "submit_debug",
                "description": "Submit structured debug output.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "hypotheses": {"type": "array", "items": {"type": "string"}},
                        "steps": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "detail": {"type": "string"},
                                    "location": {"type": "string"},
                                    "evidence": {"type": "string"},
                                    "confidence": {"type": "number"},
                                },
                                "required": ["title", "detail"],
                            },
                        },
                        "suggested_commands": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "command": {"type": "string"},
                                    "rationale": {"type": "string"},
                                    "risk": {"type": "string"},
                                },
                                "required": ["command", "rationale"],
                            },
                        },
                        "suggested_patch": {"type": "string"},
                    },
                    "required": ["summary", "hypotheses", "steps"],
                },
            },
        },
    ]


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
