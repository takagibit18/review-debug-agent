"""Priority tiers and context part factories for MVP truncation.

Smaller ``priority`` values are packed first by ``ContextBuilder.truncate_context``.
Tiers use spaced bases (10_000, 20_000, …) so sub-indices never collide across tiers.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.analyzer.context_builder import ContextPart
from src.analyzer.context_state import ContextState
from src.analyzer.schemas import DebugRequest, ReviewRequest

# Tier bases (conceptual bands from analyzer_dev_plan §2.3).
TIER_META = 10_000
TIER_ERROR_LOG = 20_000
TIER_DIFF = 30_000
TIER_FILES = 40_000
TIER_STRUCTURE = 50_000


def _split_section_at_hunks(section: str) -> list[str]:
    """Split one file's diff section at ``@@`` boundaries (after the first hunk)."""
    lines = section.splitlines(keepends=True)
    if not any(line.startswith("@@") for line in lines):
        return [section] if section.strip() else []
    chunks: list[str] = []
    current: list[str] = []
    seen_hunk = False
    for line in lines:
        if line.startswith("@@") and seen_hunk:
            chunks.append("".join(current))
            current = [line]
        else:
            if line.startswith("@@"):
                seen_hunk = True
            current.append(line)
    if current:
        chunks.append("".join(current))
    return [c for c in chunks if c.strip()]


def split_diff_hunks(diff_text: str) -> list[str]:
    """Split a unified diff into hunk-level chunks, preserving order.

    Multi-file diffs are first split on ``diff --git``; each file section is then split
    on ``@@`` so each chunk retains its file header / context.
    """
    if not diff_text or not diff_text.strip():
        return []
    if re.search(r"^diff --git ", diff_text, re.MULTILINE):
        sections = re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE)
        sections = [s for s in sections if s.strip()]
        out: list[str] = []
        for section in sections:
            out.extend(_split_section_at_hunks(section))
        return out if out else [diff_text]
    return _split_section_at_hunks(diff_text) or [diff_text]


def _meta_dict_review(
    request: ReviewRequest, context: ContextState
) -> dict[str, Any]:
    return {
        "repo_path": request.repo_path,
        "diff_mode": request.diff_mode,
        "diff_text": request.diff_text,
        "constraints": list(context.constraints),
    }


def _meta_dict_debug(
    request: DebugRequest, context: ContextState
) -> dict[str, Any]:
    return {
        "repo_path": request.repo_path,
        "error_log_path": request.error_log_path,
        "error_log_text": request.error_log_text,
        "constraints": list(context.constraints),
    }


def build_review_context_parts(
    request: ReviewRequest,
    context: ContextState,
    diff_loaded: str,
    file_contents: dict[str, str],
    project_structure: str | None = None,
) -> list[ContextPart]:
    """Build ordered truncatable parts for review mode."""
    meta = _meta_dict_review(request, context)
    parts: list[ContextPart] = [
        ContextPart(
            priority=TIER_META,
            label="meta",
            content=json.dumps(meta, ensure_ascii=True),
        )
    ]
    for i, hunk in enumerate(split_diff_hunks(diff_loaded)):
        parts.append(
            ContextPart(
                priority=TIER_DIFF + i,
                label=f"diff_hunk_{i}",
                content=hunk,
            )
        )
    for j, path in enumerate(sorted(file_contents.keys())):
        parts.append(
            ContextPart(
                priority=TIER_FILES + j,
                label=f"file:{path}",
                content=file_contents[path],
            )
        )
    if project_structure and project_structure.strip():
        parts.append(
            ContextPart(
                priority=TIER_STRUCTURE,
                label="structure",
                content=project_structure.strip(),
            )
        )
    return parts


def build_debug_context_parts(
    request: DebugRequest,
    context: ContextState,
    error_log_loaded: str,
    file_contents: dict[str, str],
    project_structure: str | None = None,
) -> list[ContextPart]:
    """Build ordered truncatable parts for debug mode (error log before files)."""
    meta = _meta_dict_debug(request, context)
    parts: list[ContextPart] = [
        ContextPart(
            priority=TIER_META,
            label="meta",
            content=json.dumps(meta, ensure_ascii=True),
        )
    ]
    if error_log_loaded.strip():
        parts.append(
            ContextPart(
                priority=TIER_ERROR_LOG,
                label="error_log",
                content=error_log_loaded,
            )
        )
    for j, path in enumerate(sorted(file_contents.keys())):
        parts.append(
            ContextPart(
                priority=TIER_FILES + j,
                label=f"file:{path}",
                content=file_contents[path],
            )
        )
    if project_structure and project_structure.strip():
        parts.append(
            ContextPart(
                priority=TIER_STRUCTURE,
                label="structure",
                content=project_structure.strip(),
            )
        )
    return parts


def _selected_labels(selected: list[ContextPart]) -> set[str]:
    return {p.label for p in selected}


def assemble_review_payload(
    request: ReviewRequest,
    context: ContextState,
    all_parts: list[ContextPart],
    selected: list[ContextPart],
) -> dict[str, Any]:
    """Merge selected parts into the user JSON payload and set ``truncated`` flags."""
    sel = _selected_labels(selected)
    all_l = _selected_labels(all_parts)

    diff_hunk_labels = sorted(
        (p.label for p in all_parts if p.label.startswith("diff_hunk_")),
        key=lambda s: int(s.split("_")[-1]),
    )
    diff_selected = [
        p.content
        for p in sorted(
            (x for x in selected if x.label.startswith("diff_hunk_")),
            key=lambda x: int(x.label.split("_")[-1]),
        )
    ]
    diff_loaded_out = "".join(diff_selected)

    files_out: dict[str, str] = {}
    for p in all_parts:
        if p.label.startswith("file:"):
            path = p.label[5:]
            if p.label in sel:
                files_out[path] = p.content

    truncated: dict[str, Any] = {
        "any": sel != all_l,
        "diff_hunks": any(l in all_l and l not in sel for l in diff_hunk_labels),
        "files": [
            p.label[5:]
            for p in all_parts
            if p.label.startswith("file:") and p.label not in sel
        ],
        "structure": any(p.label == "structure" for p in all_parts)
        and "structure" not in sel,
    }

    return {
        "repo_path": request.repo_path,
        "diff_mode": request.diff_mode,
        "diff_text": request.diff_text,
        "diff_loaded": diff_loaded_out,
        "files": files_out,
        "constraints": context.constraints,
        "truncated": truncated,
    }


def assemble_debug_payload(
    request: DebugRequest,
    context: ContextState,
    all_parts: list[ContextPart],
    selected: list[ContextPart],
) -> dict[str, Any]:
    """Merge selected parts into the debug user JSON payload."""
    sel = _selected_labels(selected)
    all_l = _selected_labels(all_parts)

    error_out = ""
    for p in all_parts:
        if p.label == "error_log":
            if "error_log" in sel:
                error_out = p.content
            break

    files_out: dict[str, str] = {}
    for p in all_parts:
        if p.label.startswith("file:"):
            path = p.label[5:]
            if p.label in sel:
                files_out[path] = p.content

    truncated = {
        "any": sel != all_l,
        "error_log": ("error_log" in all_l and "error_log" not in sel),
        "files": [
            p.label[5:]
            for p in all_parts
            if p.label.startswith("file:") and p.label not in sel
        ],
        "structure": any(p.label == "structure" for p in all_parts)
        and "structure" not in sel,
    }

    return {
        "repo_path": request.repo_path,
        "error_log_path": request.error_log_path,
        "error_log_text": request.error_log_text,
        "error_log_loaded": error_out,
        "files": files_out,
        "constraints": context.constraints,
        "truncated": truncated,
    }
