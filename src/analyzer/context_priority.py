"""Priority tiers and context part factories for MVP truncation.

Smaller ``priority`` values are packed first by ``ContextBuilder.truncate_context``.
Tiers use spaced bases (10_000, 20_000, …) so sub-indices never collide across tiers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.analyzer.context_builder import ContextBuilder, ContextPart
from src.analyzer.context_state import ContextState
from src.analyzer.reviewer_projection import (
    project_manifest_header_for_reviewer,
    project_manifest_for_reviewer,
)
from src.analyzer.schemas import DebugRequest, ReviewRequest
from src.models.token_telemetry import estimate_tokens, serialize_json

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


def _review_prompt_manifest(
    manifest: dict[str, Any], *, has_selected_graph_paths: bool
) -> dict[str, Any] | None:
    """Build the reviewer-facing manifest without duplicating visible diff hunks."""

    prompt_manifest = project_manifest_for_reviewer(manifest)
    prompt_manifest.pop("included_graph_paths", None)
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
    projection_telemetry: dict[str, Any] | None = None,
) -> list[ContextPart]:
    """Build ordered truncatable parts for review mode."""
    meta = _meta_dict_review(request, context)
    parts: list[ContextPart] = [
        ContextPart(
            priority=TIER_META,
            label="meta",
            content=serialize_json(meta),
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
        projected = project_manifest_for_reviewer(
            manifest,
            telemetry_sink=projection_telemetry,
        )
        prompt_manifest = dict(projected)
        graph_paths = prompt_manifest.pop("included_graph_paths", [])
        prompt_manifest["included_graph_paths"] = []
        parts.append(
            ContextPart(
                priority=TIER_MANIFEST + j,
                label=f"{MANIFEST_LABEL_PREFIX}{candidate_id}",
                content=serialize_json(prompt_manifest),
                token_count=estimate_tokens(serialize_json(prompt_manifest)),
            )
        )
        if not isinstance(graph_paths, list):
            continue
        for path in graph_paths:
            if not isinstance(path, dict):
                continue
            path_id = str(path.get("path_id") or path_index)
            path_content = serialize_json(
                {"candidate_id": candidate_id, "path": path},
            )
            parts.append(
                ContextPart(
                    priority=TIER_MANIFEST_PATH + path_index,
                    label=(f"{MANIFEST_PATH_LABEL_PREFIX}{candidate_id}:{path_id}"),
                    content=path_content,
                    token_count=estimate_tokens(path_content),
                )
            )
            path_index += 1
    for j, path in enumerate(sorted(file_contents.keys())):
        parts.append(
            ContextPart(
                priority=TIER_FILES + j,
                label=f"file:{path}",
                content=file_contents[path],
                source_complete=_is_complete_source_file(
                    request.repo_path, path, file_contents[path]
                ),
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
            content=serialize_json(meta),
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
                source_complete=True,
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
    all_parts: list[ContextPart],
    selected: list[ContextPart],
    *,
    telemetry_sink: dict[str, Any] | None = None,
) -> tuple[list[ContextPart], list[ContextPart]]:
    """Project manifests and remove only spans proven visible in selected context."""

    selected_diff_ranges = _selected_diff_ranges(selected)
    selected_files = {
        part.label[5:]: part
        for part in selected
        if part.label.startswith("file:")
        and not part.label.startswith(SUMMARY_LABEL_PREFIX)
    }
    selected_path_candidates = {
        part.label[len(MANIFEST_PATH_LABEL_PREFIX) :].split(":", 1)[0]
        for part in selected
        if part.label.startswith(MANIFEST_PATH_LABEL_PREFIX)
    }
    available_path_candidates = {
        part.label[len(MANIFEST_PATH_LABEL_PREFIX) :].split(":", 1)[0]
        for part in all_parts
        if part.label.startswith(MANIFEST_PATH_LABEL_PREFIX)
    }
    replacements: dict[str, ContextPart | None] = {}
    for part in all_parts:
        if not part.label.startswith(MANIFEST_LABEL_PREFIX):
            continue
        try:
            decoded = json.loads(part.content)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue
        candidate_id = part.label[len(MANIFEST_LABEL_PREFIX) :]
        prompt_manifest = _review_prompt_manifest(
            decoded,
            has_selected_graph_paths=(
                candidate_id in selected_path_candidates
                or candidate_id in available_path_candidates
            ),
        )
        if prompt_manifest is not None:
            spans = prompt_manifest.get("included_spans", [])
            if isinstance(spans, list):
                prompt_manifest["included_spans"] = [
                    span
                    for span in spans
                    if not _span_covered_by_selected_context(
                        span,
                        selected_diff_ranges,
                        selected_files,
                    )
                ]
            remaining_spans = prompt_manifest.get("included_spans", [])
            if (
                not remaining_spans
                and candidate_id not in selected_path_candidates
                and candidate_id not in available_path_candidates
            ):
                prompt_manifest = None
        replacements[part.label] = (
            None
            if prompt_manifest is None
            else ContextPart(
                priority=part.priority,
                label=part.label,
                content=serialize_json(prompt_manifest),
                token_count=estimate_tokens(serialize_json(prompt_manifest)),
                source_complete=part.source_complete,
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


def select_graph_prompt_parts(
    all_parts: list[ContextPart],
    selected: list[ContextPart],
    *,
    token_budget: int | None,
    telemetry_sink: dict[str, Any] | None = None,
) -> list[ContextPart]:
    """Apply an independent budget to serialized reviewer graph components.

    Manifests and paths are kept as candidate groups so a path is never sent
    without its stable manifest id.  This only filters the reviewer projection;
    ``all_parts`` and the internal planner manifest remain unchanged.
    """

    graph_all = [part for part in all_parts if _is_graph_part(part.label)]
    graph_selected = [part for part in selected if _is_graph_part(part.label)]
    selected_labels = {part.label for part in selected}
    selected_manifest_modes: dict[str, int] = {"full": 0, "header": 0}
    if token_budget is None:
        kept = selected
        drop_reason_counts: dict[str, int] = {}
    else:
        budget = max(0, int(token_budget))
        manifests_by_candidate: dict[str, ContextPart] = {}
        paths_by_candidate: dict[str, list[ContextPart]] = {}
        for part in graph_selected:
            candidate_id = _graph_candidate_id(part.label)
            if part.label.startswith(MANIFEST_LABEL_PREFIX):
                manifests_by_candidate[candidate_id] = part
            else:
                paths_by_candidate.setdefault(candidate_id, []).append(part)

        candidate_order = sorted(
            manifests_by_candidate,
            key=lambda candidate: (
                manifests_by_candidate[candidate].priority,
                candidate,
            ),
        )
        kept_by_label: dict[str, ContextPart] = {}
        remaining = budget

        def add_manifest_header(candidate_id: str) -> bool:
            nonlocal remaining
            full_part = manifests_by_candidate.get(candidate_id)
            if full_part is None:
                return False
            header = _header_manifest_part(full_part)
            cost = _part_token_cost(header)
            if cost > remaining:
                return False
            kept_by_label[header.label] = header
            remaining -= cost
            selected_manifest_modes["header"] += 1
            return True

        def add_path(path_part: ContextPart) -> bool:
            nonlocal remaining
            cost = _part_token_cost(path_part)
            if cost > remaining:
                return False
            kept_by_label[path_part.label] = path_part
            remaining -= cost
            return True

        # First reserve one path for every candidate that can fit.  Production
        # execution/state paths are preferred over test navigation paths.
        for candidate_id in candidate_order:
            paths = paths_by_candidate.get(candidate_id, [])
            if not paths:
                continue
            preferred = sorted(paths, key=_graph_path_priority)[0]
            header = _header_manifest_part(manifests_by_candidate[candidate_id])
            if _part_token_cost(header) + _part_token_cost(preferred) > remaining:
                continue
            if add_manifest_header(candidate_id):
                add_path(preferred)

        # Spend the remaining budget on role-diverse paths before considering
        # any full manifest.  This prevents large local manifests from
        # starving production paths belonging to later candidates.
        selected_roles: set[str] = set()
        for label, part in kept_by_label.items():
            if label.startswith(MANIFEST_PATH_LABEL_PREFIX):
                role = _graph_path_role(part)
                if role:
                    selected_roles.add(role)
        pending_paths = [
            path
            for paths in paths_by_candidate.values()
            for path in paths
            if path.label not in kept_by_label
            and _graph_candidate_id(path.label)
            in {
                _graph_candidate_id(label)
                for label in kept_by_label
                if label.startswith(MANIFEST_LABEL_PREFIX)
            }
        ]
        while pending_paths:
            pending_paths.sort(
                key=lambda part: (
                    _graph_path_role(part) in selected_roles,
                    *_graph_path_priority(part),
                )
            )
            path_part = pending_paths.pop(0)
            if add_path(path_part):
                role = _graph_path_role(path_part)
                if role:
                    selected_roles.add(role)

        # Candidates with no path still get a full manifest when there is
        # genuine room.  Path-bearing candidates deliberately keep headers so
        # the path-to-candidate binding remains cheap and explicit.
        for candidate_id in candidate_order:
            full_part = manifests_by_candidate[candidate_id]
            if full_part.label in kept_by_label:
                continue
            if not paths_by_candidate.get(candidate_id):
                cost = _part_token_cost(full_part)
                if cost <= remaining:
                    kept_by_label[full_part.label] = full_part
                    remaining -= cost
                    selected_manifest_modes["full"] += 1

        kept = [
            kept_by_label.get(part.label, part)
            for part in selected
            if not _is_graph_part(part.label) or part.label in kept_by_label
        ]
        drop_reason_counts = {}
        kept_labels = set(kept_by_label)
        for part in graph_all:
            if part.label in kept_labels:
                continue
            reason = (
                "global_prompt_budget"
                if part.label not in selected_labels
                else "graph_reviewer_token_budget"
            )
            if part.label.startswith(MANIFEST_PATH_LABEL_PREFIX):
                drop_reason_counts[reason] = drop_reason_counts.get(reason, 0) + 1

    kept_path_parts = [
        part for part in kept if part.label.startswith(MANIFEST_PATH_LABEL_PREFIX)
    ]
    available_paths = [
        part for part in graph_all if part.label.startswith(MANIFEST_PATH_LABEL_PREFIX)
    ]
    telemetry = {
        "budget": None if token_budget is None else max(0, int(token_budget)),
        "available_token_count": sum(_part_token_cost(part) for part in graph_all),
        "selected_token_count": sum(
            _part_token_cost(part) for part in kept if _is_graph_part(part.label)
        ),
        "available_path_count": len(available_paths),
        "selected_path_count": len(kept_path_parts),
        "dropped_path_count": max(0, len(available_paths) - len(kept_path_parts)),
        "drop_reason_counts": drop_reason_counts,
        "selected_role_coverage": sorted(
            {
                role
                for role in (_graph_path_role(part) for part in kept_path_parts)
                if role
            }
        ),
        "selected_manifest_mode_counts": selected_manifest_modes,
        "path_first": True,
    }
    if telemetry_sink is not None:
        telemetry_sink["graph_reviewer_prompt_projection"] = telemetry
    return kept


def _is_graph_part(label: str) -> bool:
    return label.startswith((MANIFEST_LABEL_PREFIX, MANIFEST_PATH_LABEL_PREFIX))


def _graph_candidate_id(label: str) -> str:
    if label.startswith(MANIFEST_LABEL_PREFIX):
        return label[len(MANIFEST_LABEL_PREFIX) :]
    return label[len(MANIFEST_PATH_LABEL_PREFIX) :].split(":", 1)[0]


def _part_token_cost(part: ContextPart) -> int:
    return int(part.token_count or estimate_tokens(part.content))


def _header_manifest_part(part: ContextPart) -> ContextPart:
    try:
        decoded = json.loads(part.content)
    except (TypeError, json.JSONDecodeError):
        return part
    if not isinstance(decoded, dict):
        return part
    content = serialize_json(project_manifest_header_for_reviewer(decoded))
    return ContextPart(
        priority=part.priority,
        label=part.label,
        content=content,
        token_count=estimate_tokens(content),
        source_complete=part.source_complete,
    )


def _graph_path_priority(part: ContextPart) -> tuple[int, int, int, str]:
    """Rank production causal paths ahead of test-only navigation paths."""

    role = _graph_path_role(part)
    role_rank = {
        "execution_flow": 0,
        "state_flow": 0,
        "field_flow": 0,
        "related_test": 2,
    }.get(role, 1)
    try:
        decoded = json.loads(part.content)
    except (TypeError, json.JSONDecodeError):
        decoded = {}
    path = decoded.get("path") if isinstance(decoded, dict) else {}
    edges = path.get("edges", []) if isinstance(path, dict) else []
    edge_count = len(edges) if isinstance(edges, list) else 0
    text = part.content.replace("\\", "/").lower()
    test_penalty = int("/tests/" in text or "test_" in text)
    return role_rank, test_penalty, -edge_count, part.label


def _graph_path_role(part: ContextPart) -> str:
    try:
        decoded = json.loads(part.content)
    except (TypeError, json.JSONDecodeError):
        return ""
    path = decoded.get("path") if isinstance(decoded, dict) else None
    return str(path.get("semantic_role", "") or "") if isinstance(path, dict) else ""


def _is_complete_source_file(repo_path: str, path: str, content: str) -> bool:
    """Prove that a file context part is the complete current source file."""

    if not content or "...[truncated]" in content.lower():
        return False
    root = Path(repo_path).resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
        return target.is_file() and target.read_text(encoding="utf-8") == content
    except (OSError, UnicodeError, ValueError):
        return False


def _selected_diff_ranges(
    selected: list[ContextPart],
) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    for part in selected:
        if not part.label.startswith("diff_hunk_"):
            continue
        for path, start, end in _diff_hunk_ranges(part.content):
            ranges.setdefault(path, []).append((start, end))
    return ranges


_DIFF_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")


def _diff_hunk_ranges(diff_hunk: str) -> list[tuple[str, int, int]]:
    path = ""
    for line in diff_hunk.splitlines():
        if line.startswith("+++ "):
            path = ContextBuilder._normalize_diff_path(
                line[4:].split("\t", 1)[0].strip()
            )
            break
        if line.startswith("diff --git "):
            pieces = line.split()
            if len(pieces) >= 4:
                path = ContextBuilder._normalize_diff_path(pieces[3])
    if not path:
        return []
    output: list[tuple[str, int, int]] = []
    for line in diff_hunk.splitlines():
        match = _DIFF_HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        end = start + max(0, count) - 1
        output.append((path, start, max(start, end)))
    return output


def _span_covered_by_selected_context(
    span: Any,
    diff_ranges: dict[str, list[tuple[int, int]]],
    selected_files: dict[str, ContextPart],
) -> bool:
    if not isinstance(span, dict):
        return False
    path = str(span.get("file", "")).replace("\\", "/").lstrip("./")
    try:
        start = int(span.get("start_line", 0) or 0)
        end = int(span.get("end_line", start) or start)
    except (TypeError, ValueError):
        return False
    if not path or start < 1 or end < start:
        return False
    if any(start >= low and end <= high for low, high in diff_ranges.get(path, [])):
        return True
    file_part = selected_files.get(path)
    if file_part is None or not file_part.source_complete:
        return False
    return len(file_part.content.splitlines()) >= end


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
