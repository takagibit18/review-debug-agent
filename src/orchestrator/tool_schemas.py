"""Orchestrator-owned conversion from ToolSpec to model tool schemas."""

from __future__ import annotations

from typing import Any

from src.tools.base import ToolSpec


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
                                    "severity": {
                                        "type": "string",
                                        "enum": ["critical", "warning", "info", "style"],
                                    },
                                    "location": {
                                        "type": "string",
                                        "description": "Canonical location: path[:line[-end_line]]",
                                        "pattern": r"^[^:\s][^:]*(:\d+(-\d+)?)?$",
                                    },
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
                                    "location": {
                                        "type": "string",
                                        "description": "Canonical location: path[:line[-end_line]]",
                                        "pattern": r"^[^:\s][^:]*(:\d+(-\d+)?)?$",
                                    },
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
