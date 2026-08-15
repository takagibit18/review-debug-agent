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
TIER_MANIFEST = 35_000
TIER_MANIFEST_PATH = 36_000
TIER_FILES = 40_000
TIER_STRUCTURE = 50_000

SUMMARY_LABEL_PREFIX = "[summarized]"
MANIFEST_LABEL_PREFIX = "manifest:"
MANIFEST_PATH_LABEL_PREFIX = "manifest_path:"
_AUDIT_ONLY_MANIFEST_FIELDS = {"excluded_low_confidence_paths", "discarded_paths"}


def _is_diff_duplicate_span(span: Any) -> bool:
    """Return whether a manifest span repeats a changed hunk already in the diff."""

    return (
        isinstance(span, dict)
        and str(span.get("retrieval_source", "")).strip() == "git_diff"
        and str(span.get("role", "")).strip() == "changed_hunk"
    )


def _review_prompt_manifest(
    manifest: dict[str, Any], *, has_selected_graph_paths: bool
) -> dict[str, Any] | None:
    """Build the reviewer-facing manifest without duplicating visible diff hunks."""

    prompt_manifest = {
        key: value
        for key, value in manifest.items()
        if key not in _AUDIT_ONLY_MANIFEST_FIELDS and key != "included_graph_paths"
    }
    spans = prompt_manifest.get("included_spans", [])
    if isinstance(spans, list):
        prompt_manifest["included_spans"] = [
            span for span in spans if not _is_diff_duplicate_span(span)
        ]
    retained_spans = prompt_manifest.get("included_spans", [])
    if not retained_spans and not has_selected_graph_paths:
        return None
    prompt_manifest["included_graph_paths"] = []
    return prompt_manifest


def _split_section_at_hunks(section: str) -> list[str]:
    """Split one file's diff section at ``@@`` boundaries (after the first hunk)."""
    lines = section.splitlines(keepends=True)
    if not any(line.startswith("@@") for line in lines):
        return [section] if section.strip() else []
    chunks: list[str] = []
    current: list[str] = []
    header: list[str] = []
    seen_hunk = False
    for line in lines:
        if line.startswith("@@") and seen_hunk:
            chunks.append("".join(current))
            current = [*header, line]
        else:
            if line.startswith("@@"):
                seen_hunk = True
                header = current.copy()
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


def _meta_dict_review(request: ReviewRequest, context: ContextState) -> dict[str, Any]:
    return {
        "repo_path": request.repo_path,
        "diff_mode": request.diff_mode,
        "has_diff_text": bool(request.diff_text),
        "constraints": list(context.constraints),
        "candidate_context_manifest_count": len(context.candidate_context_manifests),
        "relation_graph_summary": context.relation_graph_summary,
    }


def _meta_dict_debug(request: DebugRequest, context: ContextState) -> dict[str, Any]:
    return {
        "repo_path": request.repo_path,
        "error_log_path": request.error_log_path,
        "has_error_log_text": bool(request.error_log_text),
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
    path_index = 0
    for j, manifest in enumerate(context.candidate_context_manifests):
        candidate_id = str(manifest.get("candidate_id") or f"candidate-{j}")
        prompt_manifest = {
            key: value
            for key, value in manifest.items()
            if key not in _AUDIT_ONLY_MANIFEST_FIELDS and key != "included_graph_paths"
        }
        prompt_manifest["included_graph_paths"] = []
        parts.append(
            ContextPart(
                priority=TIER_MANIFEST + j,
                label=f"{MANIFEST_LABEL_PREFIX}{candidate_id}",
                content=json.dumps(prompt_manifest, ensure_ascii=True),
            )
        )
        graph_paths = manifest.get("included_graph_paths", [])
        if not isinstance(graph_paths, list):
            continue
        for path in graph_paths:
            if not isinstance(path, dict):
                continue
            path_id = str(path.get("path_id") or path_index)
            parts.append(
                ContextPart(
                    priority=TIER_MANIFEST_PATH + path_index,
                    label=(f"{MANIFEST_PATH_LABEL_PREFIX}{candidate_id}:{path_id}"),
                    content=json.dumps(
                        {"candidate_id": candidate_id, "path": path},
                        ensure_ascii=True,
                    ),
                )
            )
            path_index += 1
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


def compact_review_prompt_parts(
    all_parts: list[ContextPart], selected: list[ContextPart]
) -> tuple[list[ContextPart], list[ContextPart]]:
    """Elide duplicate diff spans only when the exact diff is already selected."""

    diff_labels = [
        part.label for part in all_parts if part.label.startswith("diff_hunk_")
    ]
    selected_labels = _selected_labels(selected)
    if not diff_labels or any(label not in selected_labels for label in diff_labels):
        return all_parts, selected
    selected_path_candidates = {
        part.label[len(MANIFEST_PATH_LABEL_PREFIX) :].split(":", 1)[0]
        for part in selected
        if part.label.startswith(MANIFEST_PATH_LABEL_PREFIX)
    }
    replacements: dict[str, ContextPart | None] = {}
    for part in all_parts:
        if not part.label.startswith(MANIFEST_LABEL_PREFIX):
            continue
        decoded = json.loads(part.content)
        if not isinstance(decoded, dict):
            continue
        candidate_id = part.label[len(MANIFEST_LABEL_PREFIX) :]
        prompt_manifest = _review_prompt_manifest(
            decoded,
            has_selected_graph_paths=candidate_id in selected_path_candidates,
        )
        replacements[part.label] = (
            None
            if prompt_manifest is None
            else ContextPart(
                priority=part.priority,
                label=part.label,
                content=json.dumps(prompt_manifest, ensure_ascii=True),
            )
        )

    def apply(parts: list[ContextPart]) -> list[ContextPart]:
        compacted: list[ContextPart] = []
        for part in parts:
            replacement = replacements.get(part.label, part)
            if replacement is not None:
                compacted.append(replacement)
        return compacted

    return apply(all_parts), apply(selected)


def _base_label(label: str) -> str:
    if label.startswith(SUMMARY_LABEL_PREFIX):
        return label[len(SUMMARY_LABEL_PREFIX) :]
    return label


def _is_summarized_label(label: str) -> bool:
    return label.startswith(SUMMARY_LABEL_PREFIX)


def _contains_label_or_summary(labels: set[str], label: str) -> bool:
    return label in labels or f"{SUMMARY_LABEL_PREFIX}{label}" in labels


def _selected_part_for_label(
    selected: list[ContextPart], target_label: str
) -> ContextPart | None:
    for part in selected:
        if part.label == target_label:
            return part
    summarized_label = f"{SUMMARY_LABEL_PREFIX}{target_label}"
    for part in selected:
        if part.label == summarized_label:
            return part
    return None


def assemble_review_payload(
    request: ReviewRequest,
    context: ContextState,
    all_parts: list[ContextPart],
    selected: list[ContextPart],
) -> dict[str, Any]:
    """Merge selected parts into the user JSON payload and set ``truncated`` flags."""
    sel = _selected_labels(selected)
    all_l = _selected_labels(all_parts)
    summarized_bases = sorted(
        _base_label(p.label) for p in selected if _is_summarized_label(p.label)
    )

    diff_hunk_labels = sorted(
        (p.label for p in all_parts if p.label.startswith("diff_hunk_")),
        key=lambda s: int(s.split("_")[-1]),
    )
    diff_selected: list[str] = []
    for label in diff_hunk_labels:
        part = _selected_part_for_label(selected, label)
        if part is not None:
            diff_selected.append(part.content)
    diff_loaded_out = "".join(diff_selected)

    files_out: dict[str, str] = {}
    for p in all_parts:
        if p.label.startswith("file:"):
            path = p.label[5:]
            selected_part = _selected_part_for_label(selected, p.label)
            if selected_part is not None:
                files_out[path] = selected_part.content
    structure_out = ""
    structure_part = _selected_part_for_label(selected, "structure")
    if structure_part is not None:
        structure_out = structure_part.content
    manifests_out: list[dict[str, Any]] = []
    manifests_by_id: dict[str, dict[str, Any]] = {}
    for part in selected:
        if not part.label.startswith(MANIFEST_LABEL_PREFIX):
            continue
        decoded = json.loads(part.content)
        if isinstance(decoded, dict):
            manifests_out.append(decoded)
            manifests_by_id[str(decoded.get("candidate_id") or "")] = decoded
    for part in selected:
        if not part.label.startswith(MANIFEST_PATH_LABEL_PREFIX):
            continue
        decoded = json.loads(part.content)
        if not isinstance(decoded, dict):
            continue
        manifest = manifests_by_id.get(str(decoded.get("candidate_id") or ""))
        manifest_path = decoded.get("path")
        if manifest is not None and isinstance(manifest_path, dict):
            manifest["included_graph_paths"].append(manifest_path)

    truncated: dict[str, Any] = {
        "any": any(not _contains_label_or_summary(sel, label) for label in all_l),
        "diff_hunks": any(
            h in all_l and not _contains_label_or_summary(sel, h)
            for h in diff_hunk_labels
        ),
        "files": [
            p.label[5:]
            for p in all_parts
            if p.label.startswith("file:")
            and not _contains_label_or_summary(sel, p.label)
        ],
        "structure": any(p.label == "structure" for p in all_parts)
        and not _contains_label_or_summary(sel, "structure"),
        "summarized": summarized_bases,
        "candidate_context_manifests": [
            part.label[len(MANIFEST_LABEL_PREFIX) :]
            for part in all_parts
            if part.label.startswith(MANIFEST_LABEL_PREFIX)
            and not _contains_label_or_summary(sel, part.label)
        ],
        "candidate_context_graph_paths": sum(
            part.label.startswith(MANIFEST_PATH_LABEL_PREFIX)
            and not _contains_label_or_summary(sel, part.label)
            for part in all_parts
        ),
    }
    raw_diff_text = request.diff_text
    if raw_diff_text and (truncated["diff_hunks"] or diff_loaded_out != raw_diff_text):
        raw_diff_text = None

    return {
        "repo_path": request.repo_path,
        "diff_mode": request.diff_mode,
        "diff_text": raw_diff_text,
        "diff_loaded": diff_loaded_out,
        "files": files_out,
        "project_structure": structure_out,
        "constraints": context.constraints,
        "candidate_context_manifests": manifests_out,
        "relation_graph_summary": context.relation_graph_summary,
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
    summarized_bases = sorted(
        _base_label(p.label) for p in selected if _is_summarized_label(p.label)
    )

    error_out = ""
    for p in all_parts:
        if p.label == "error_log":
            selected_part = _selected_part_for_label(selected, "error_log")
            if selected_part is not None:
                error_out = selected_part.content
            break

    files_out: dict[str, str] = {}
    for p in all_parts:
        if p.label.startswith("file:"):
            path = p.label[5:]
            selected_part = _selected_part_for_label(selected, p.label)
            if selected_part is not None:
                files_out[path] = selected_part.content
    structure_out = ""
    structure_part = _selected_part_for_label(selected, "structure")
    if structure_part is not None:
        structure_out = structure_part.content

    truncated = {
        "any": any(not _contains_label_or_summary(sel, label) for label in all_l),
        "error_log": (
            "error_log" in all_l and not _contains_label_or_summary(sel, "error_log")
        ),
        "files": [
            p.label[5:]
            for p in all_parts
            if p.label.startswith("file:")
            and not _contains_label_or_summary(sel, p.label)
        ],
        "structure": any(p.label == "structure" for p in all_parts)
        and not _contains_label_or_summary(sel, "structure"),
        "summarized": summarized_bases,
    }
    raw_error_log_text = request.error_log_text
    if raw_error_log_text and (
        truncated["error_log"] or error_out != raw_error_log_text
    ):
        raw_error_log_text = None

    return {
        "repo_path": request.repo_path,
        "error_log_path": request.error_log_path,
        "error_log_text": raw_error_log_text,
        "error_log_loaded": error_out,
        "files": files_out,
        "project_structure": structure_out,
        "constraints": context.constraints,
        "truncated": truncated,
    }
