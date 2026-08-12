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
                    "parameters": spec.parameters
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return schemas


def build_submit_tool_schemas() -> list[dict[str, Any]]:
    """Pseudo-tools used for structured final output submission."""
    anchor_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "symbol_id": {"type": "string"},
        },
        "required": ["file", "line", "symbol_id"],
    }
    evidence_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "candidate_id": {"type": "string", "minLength": 1},
            "context_manifest_id": {"type": "string", "minLength": 1},
            "retrieval_source": {"type": "string", "minLength": 1},
            "file": {"type": "string", "minLength": 1},
            "line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "symbol_id": {"type": "string", "minLength": 1},
            "context_hash": {"type": "string", "minLength": 1},
            "edge_kind": {"type": "string"},
            "edge_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "resolver": {"type": "string", "minLength": 1},
            "evidence_eligibility": {
                "type": "string",
                "enum": ["strong", "exploratory", "none"],
            },
            "statement": {"type": "string", "minLength": 1},
        },
        "required": [
            "candidate_id",
            "retrieval_source",
            "file",
            "line",
            "statement",
        ],
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "submit_review",
                "description": "Submit structured review output.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": (
                                "High-level result. The summary must not mention bugs, "
                                "regressions, breaking changes, compatibility risks, or "
                                "user-visible behavior changes unless the same finding is "
                                "present in issues."
                            ),
                        },
                        "issues": {
                            "type": "array",
                            "description": (
                                "Structured findings. Use [] only when there are no "
                                "supported bugs, regressions, breaking changes, "
                                "compatibility risks, or actionable review findings."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "severity": {
                                        "type": "string",
                                        "enum": [
                                            "critical",
                                            "warning",
                                            "info",
                                            "style",
                                        ],
                                    },
                                    "location": {
                                        "type": "string",
                                        "description": (
                                            "Canonical display location: "
                                            "path[:line[-end_line]]. It may be unchanged; "
                                            "cause_evidence owns PR causality."
                                        ),
                                        "pattern": r"^[^:\s][^:]*(:\d+(-\d+)?)?$",
                                    },
                                    "evidence": {"type": "string"},
                                    "suggestion": {"type": "string"},
                                    "confidence": {
                                        "type": "number",
                                        "description": (
                                            "Use >= 0.85 for concrete changed-code bugs, "
                                            "regressions, compatibility breaks, or user-visible "
                                            "behavior changes; use lower values for speculative "
                                            "or non-blocking concerns."
                                        ),
                                    },
                                    "schema_version": {
                                        "type": "string",
                                        "enum": ["2.0"],
                                    },
                                    "finding_id": {
                                        "type": "string",
                                        "description": "Reviewer-local id such as F-01. Do not emit root_cause_id.",
                                    },
                                    "primary_anchor": {
                                        **anchor_schema,
                                        "description": (
                                            "Primary display anchor matching location; it need "
                                            "not be changed when changed cause_evidence proves "
                                            "PR causality."
                                        ),
                                    },
                                    "related_locations": {
                                        "type": "array",
                                        "items": {
                                            **anchor_schema,
                                            "properties": {
                                                **anchor_schema["properties"],
                                                "role": {
                                                    "type": "string",
                                                    "enum": [
                                                        "cause",
                                                        "contract",
                                                        "trigger",
                                                        "impact",
                                                        "related",
                                                    ],
                                                },
                                                "description": {"type": "string"},
                                            },
                                        },
                                    },
                                    "observed_behavior": {"type": "string"},
                                    "causal_mechanism": {"type": "string"},
                                    "violated_invariant": {"type": "string"},
                                    "repair_intent": {
                                        "type": "object",
                                        "properties": {
                                            "action": {"type": "string"},
                                            "targets": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                            },
                                            "boundary": {"type": "string"},
                                        },
                                        "required": ["action", "targets", "boundary"],
                                    },
                                    "trigger": {"type": "string"},
                                    "impact": {"type": "string"},
                                    "cause_evidence": {
                                        "type": "array",
                                        "items": evidence_schema,
                                        "description": (
                                            "Causal evidence. Warning/critical findings require "
                                            "at least one entry on a real changed line."
                                        ),
                                    },
                                    "contract_evidence": {
                                        "type": "array",
                                        "items": evidence_schema,
                                    },
                                    "trigger_evidence": {
                                        "type": "array",
                                        "items": evidence_schema,
                                    },
                                    "impact_evidence": {
                                        "type": "array",
                                        "items": evidence_schema,
                                    },
                                    "context_manifest_id": {"type": "string"},
                                    "context_hash": {"type": "string"},
                                },
                                "required": [
                                    "severity",
                                    "location",
                                    "evidence",
                                    "suggestion",
                                    "schema_version",
                                    "finding_id",
                                    "primary_anchor",
                                    "related_locations",
                                    "observed_behavior",
                                    "causal_mechanism",
                                    "violated_invariant",
                                    "repair_intent",
                                    "trigger",
                                    "impact",
                                    "cause_evidence",
                                    "contract_evidence",
                                    "trigger_evidence",
                                    "impact_evidence",
                                ],
                            },
                        },
                    },
                    "required": ["summary", "issues"],
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
