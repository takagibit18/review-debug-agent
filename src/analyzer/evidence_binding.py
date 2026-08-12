"""Bind model-authored evidence metadata to provenance observed by the system."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.analyzer.diff_lines import ParsedDiffHunk, parse_unified_diff_hunks
from src.analyzer.finding_schema import EvidenceProvenance, normalize_repo_path
from src.analyzer.output_formatter import ReviewIssue
from src.analyzer.schemas import FindingCandidate, ReviewRequest

_DIFF_SOURCES = {"git_diff", "diff", "review_diff", "changed_hunk"}
_CANONICAL_TOOL_SOURCES = {
    "changed_context": "get_changed_context",
    "get_changed_context": "get_changed_context",
    "symbol_context": "find_symbol_context",
    "find_symbol_context": "find_symbol_context",
}
_TRUSTED_TOOL_SOURCES = {
    "read_file",
    "get_changed_context",
    "changed_context",
    "find_symbol_context",
    "symbol_context",
}
_TOOL_SOURCE_PRIORITY = {
    "read_file": 1,
    "get_changed_context": 2,
    "find_symbol_context": 3,
}


@dataclass(frozen=True)
class TrustedEvidenceBinding:
    """One provenance representation the runtime can prove it retained."""

    kind: Literal["diff", "tool", "manifest"]
    retrieval_source: str
    context_manifest_id: str = ""
    context_hash: str = ""
    symbol_id: str = ""


def bind_candidate_evidence(
    candidates: list[FindingCandidate],
    request: ReviewRequest,
    tool_evidence: list[dict[str, Any]],
    *,
    context_manifests: list[dict[str, Any]] | None = None,
) -> list[FindingCandidate]:
    """Return candidates whose evidence is bound only to trusted run context.

    Candidate identity and non-manifest source selection are system-owned. When a
    location has several trusted representations, a stable runtime priority chooses
    diff, then read, changed-context, and symbol-context evidence. Explicit manifest
    claims still require an exact id/hash match; ambiguous manifest-only provenance
    remains unchanged so deterministic validation can fail closed.
    """

    hunks_by_file = parse_unified_diff_hunks(request.diff_text or "")
    manifests = context_manifests or []
    bound: list[FindingCandidate] = []
    for candidate in candidates:
        issue = candidate.issue.model_copy(deep=True)
        issue.candidate_id = candidate.candidate_id
        bound_manifest_ids: set[str] = set()
        for evidence in issue.all_evidence():
            evidence.candidate_id = candidate.candidate_id
            matches = _trusted_bindings_for_evidence(
                evidence,
                hunks_by_file,
                tool_evidence,
                manifests,
            )
            selected = _select_binding(evidence, matches)
            if selected is not None:
                _apply_binding(evidence, selected)
                if selected.context_manifest_id:
                    bound_manifest_ids.add(selected.context_manifest_id)
        issue.context_manifest_id = _single_value(bound_manifest_ids)
        issue.context_hash = ""
        bound.append(candidate.model_copy(update={"issue": issue}))
    return bound


def bind_issue_candidate_id(issue: ReviewIssue, candidate_id: str) -> ReviewIssue:
    """Deep-copy an issue and overwrite every model-authored candidate id."""

    bound = issue.model_copy(deep=True)
    bound.candidate_id = candidate_id
    for evidence in bound.all_evidence():
        evidence.candidate_id = candidate_id
    return bound


def bind_issue_evidence_from_context(
    issue: ReviewIssue,
    candidate_id: str,
    context: dict[str, Any] | None,
) -> ReviewIssue:
    """Rebind a verifier-revised issue to context the verifier actually received."""

    bound = bind_issue_candidate_id(issue, candidate_id)
    bound.context_manifest_id = ""
    bound.context_hash = ""
    if context is None:
        return bound
    bound_manifest_ids: set[str] = set()
    for evidence in bound.all_evidence():
        selected = _select_binding(
            evidence, _trusted_bindings_in_candidate_context(evidence, context)
        )
        if selected is not None:
            _apply_binding(evidence, selected)
            if selected.context_manifest_id:
                bound_manifest_ids.add(selected.context_manifest_id)
    bound.context_manifest_id = _single_value(bound_manifest_ids)
    return bound


def _trusted_bindings_for_evidence(
    evidence: EvidenceProvenance,
    hunks_by_file: dict[str, list[ParsedDiffHunk]],
    tool_evidence: list[dict[str, Any]],
    manifests: list[dict[str, Any]],
) -> list[TrustedEvidenceBinding]:
    file = normalize_repo_path(evidence.file)
    line = evidence.line
    if not file or line is None:
        return []
    end_line = evidence.end_line or line
    matches: set[TrustedEvidenceBinding] = set()

    for hunk in hunks_by_file.get(file, []):
        hunk_end = hunk.new_start + max(0, hunk.new_count - 1)
        if _ranges_overlap(line, end_line, hunk.new_start, hunk_end):
            matches.add(
                TrustedEvidenceBinding(kind="diff", retrieval_source="git_diff")
            )

    for entry in tool_evidence:
        tool_name = str(entry.get("tool_name", "")).strip().lower()
        if tool_name not in _TRUSTED_TOOL_SOURCES:
            continue
        arguments = entry.get("arguments")
        data = entry.get("data")
        if not isinstance(arguments, dict) or not isinstance(data, dict):
            continue
        if _tool_entry_covers(tool_name, arguments, data, file, line, end_line):
            matches.add(
                TrustedEvidenceBinding(
                    kind="tool",
                    retrieval_source=_canonical_tool_source(tool_name),
                )
            )

    for manifest in manifests:
        manifest_id = str(manifest.get("candidate_id", "")).strip()
        if not manifest_id:
            continue
        spans = manifest.get("included_spans")
        if not isinstance(spans, list):
            continue
        for span in spans:
            if not isinstance(span, dict) or not _record_covers(
                span, file, line, end_line
            ):
                continue
            matches.add(
                TrustedEvidenceBinding(
                    kind="manifest",
                    retrieval_source=str(span.get("retrieval_source", "relation_graph"))
                    .strip()
                    .lower(),
                    context_manifest_id=manifest_id,
                    context_hash=str(span.get("context_hash", "")).strip(),
                    symbol_id=str(span.get("symbol_id", "")).strip(),
                )
            )
    return sorted(
        matches,
        key=lambda item: (
            item.kind,
            item.retrieval_source,
            item.context_manifest_id,
            item.context_hash,
        ),
    )


def _trusted_bindings_in_candidate_context(
    evidence: EvidenceProvenance,
    context: dict[str, Any],
) -> list[TrustedEvidenceBinding]:
    """Enumerate exact source representations retained in a verifier envelope."""

    file = normalize_repo_path(evidence.file)
    line = evidence.line
    if not file or line is None:
        return []
    end_line = evidence.end_line or line
    matches: set[TrustedEvidenceBinding] = set()

    diff_hunks = context.get("diff_hunks")
    if isinstance(diff_hunks, list):
        for record in diff_hunks:
            if not isinstance(record, dict) or not _record_covers(
                record, file, line, end_line
            ):
                continue
            source = str(record.get("source", "diff")).strip().lower()
            if source in _DIFF_SOURCES:
                matches.add(
                    TrustedEvidenceBinding(kind="diff", retrieval_source="git_diff")
                )
            elif source in _TRUSTED_TOOL_SOURCES:
                matches.add(
                    TrustedEvidenceBinding(
                        kind="tool",
                        retrieval_source=_canonical_tool_source(source),
                    )
                )

    for key in ("file_windows", "enclosing_symbols"):
        records = context.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or not _record_covers(
                record, file, line, end_line
            ):
                continue
            source = str(record.get("source", "")).strip().lower()
            if source in _TRUSTED_TOOL_SOURCES:
                matches.add(
                    TrustedEvidenceBinding(
                        kind="tool",
                        retrieval_source=_canonical_tool_source(source),
                    )
                )

    symbol_contexts = context.get("symbol_contexts")
    if isinstance(symbol_contexts, list):
        for symbol_context in symbol_contexts:
            if not isinstance(symbol_context, dict):
                continue
            source = str(symbol_context.get("source", "")).strip().lower()
            if source not in _TRUSTED_TOOL_SOURCES:
                continue
            if any(
                _records_cover(symbol_context.get(key), file, line, end_line)
                for key in ("definitions", "references", "enclosing_symbols")
            ):
                matches.add(
                    TrustedEvidenceBinding(
                        kind="tool",
                        retrieval_source=_canonical_tool_source(source),
                    )
                )

    manifest_id = str(context.get("context_manifest_id", "")).strip()
    spans = context.get("included_spans")
    if manifest_id and isinstance(spans, list):
        for span in spans:
            if not isinstance(span, dict) or not _record_covers(
                span, file, line, end_line
            ):
                continue
            matches.add(
                TrustedEvidenceBinding(
                    kind="manifest",
                    retrieval_source=str(span.get("retrieval_source", "relation_graph"))
                    .strip()
                    .lower(),
                    context_manifest_id=manifest_id,
                    context_hash=str(span.get("context_hash", "")).strip(),
                    symbol_id=str(span.get("symbol_id", "")).strip(),
                )
            )
    return sorted(
        matches,
        key=lambda item: (
            item.kind,
            item.retrieval_source,
            item.context_manifest_id,
            item.context_hash,
        ),
    )


def _select_binding(
    evidence: EvidenceProvenance,
    matches: list[TrustedEvidenceBinding],
) -> TrustedEvidenceBinding | None:
    """Choose provenance from trusted runtime state, never a model source label."""

    declared_manifest = evidence.context_manifest_id.strip()
    declared_hash = evidence.context_hash.strip()
    if declared_manifest:
        manifest_matches = [
            item
            for item in matches
            if item.kind == "manifest" and item.context_manifest_id == declared_manifest
        ]
        if declared_hash:
            manifest_matches = [
                item for item in manifest_matches if item.context_hash == declared_hash
            ]
        return manifest_matches[0] if len(manifest_matches) == 1 else None

    non_manifest_matches = [item for item in matches if item.kind != "manifest"]
    if non_manifest_matches:
        return min(non_manifest_matches, key=_binding_priority)

    manifest_matches = [item for item in matches if item.kind == "manifest"]
    return manifest_matches[0] if len(manifest_matches) == 1 else None


def _apply_binding(
    evidence: EvidenceProvenance,
    binding: TrustedEvidenceBinding,
) -> None:
    evidence.retrieval_source = binding.retrieval_source
    evidence.context_manifest_id = binding.context_manifest_id
    evidence.context_hash = binding.context_hash
    if binding.symbol_id:
        evidence.symbol_id = binding.symbol_id
    if binding.kind != "manifest":
        evidence.edge_kind = ""
        evidence.edge_confidence = None
        evidence.resolver = ""
        evidence.evidence_eligibility = "strong"


def _binding_priority(binding: TrustedEvidenceBinding) -> tuple[int, str]:
    if binding.kind == "diff":
        return (0, binding.retrieval_source)
    return (
        _TOOL_SOURCE_PRIORITY.get(binding.retrieval_source, 100),
        binding.retrieval_source,
    )


def _canonical_tool_source(value: str) -> str:
    source = value.strip().lower()
    return _CANONICAL_TOOL_SOURCES.get(source, source)


def _single_value(values: set[str]) -> str:
    return next(iter(values)) if len(values) == 1 else ""


def _tool_entry_covers(
    tool_name: str,
    arguments: dict[str, Any],
    data: dict[str, Any],
    file: str,
    line: int,
    end_line: int,
) -> bool:
    data_path = normalize_repo_path(
        str(
            data.get("file_path")
            or data.get("path")
            or arguments.get("file_path")
            or arguments.get("path")
            or ""
        )
    )
    if tool_name == "read_file":
        if data_path != file:
            return False
        start = _as_int(data.get("start_line"))
        count = _as_int(data.get("line_count")) or 0
        finish = start + max(0, count - 1) if start is not None else None
        return _ranges_overlap(line, end_line, start, finish)
    if tool_name in {"get_changed_context", "changed_context"}:
        if data_path != file:
            return False
        return any(
            isinstance(data.get(key), dict)
            and _record_covers(data[key], file, line, end_line, default_path=data_path)
            for key in ("hunk", "file_window")
        ) or _records_cover(
            data.get("enclosing_symbols"), file, line, end_line, default_path=data_path
        )
    return any(
        _records_cover(data.get(key), file, line, end_line, default_path=data_path)
        for key in ("definitions", "references", "enclosing_symbols")
    )


def _records_cover(
    records: Any,
    file: str,
    line: int,
    end_line: int,
    *,
    default_path: str = "",
) -> bool:
    return isinstance(records, list) and any(
        isinstance(record, dict)
        and _record_covers(record, file, line, end_line, default_path=default_path)
        for record in records
    )


def _record_covers(
    record: dict[str, Any],
    file: str,
    line: int,
    end_line: int,
    *,
    default_path: str = "",
) -> bool:
    path = normalize_repo_path(
        str(record.get("path") or record.get("file") or default_path)
    )
    if path != file:
        return False
    start = _as_int(record.get("start_line"))
    if start is None:
        start = _as_int(record.get("line"))
    if start is None:
        start = _as_int(record.get("new_start"))
    finish = _as_int(record.get("end_line"))
    if finish is None and start is not None:
        count = _as_int(record.get("new_count"))
        if count is None:
            count = _as_int(record.get("line_count"))
        finish = start + max(0, (count or 1) - 1)
    return _ranges_overlap(line, end_line, start, finish)


def _ranges_overlap(
    left_start: int | None,
    left_end: int | None,
    right_start: int | None,
    right_end: int | None,
) -> bool:
    if left_start is None or right_start is None:
        return False
    return left_start <= (right_end or right_start) and right_start <= (
        left_end or left_start
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
