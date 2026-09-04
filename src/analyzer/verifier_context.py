"""Run-scoped tool evidence captured and compacted for finding verification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.analyzer.diff_lines import ParsedDiffHunk, parse_unified_diff_hunks
from src.analyzer.evidence_policy import evidence_policy_for_mode
from src.analyzer.location import normalize_location
from src.analyzer.schemas import FindingCandidate, ReviewRequest

VERIFIER_CONTEXT_TOOL_NAMES = {
    "read_file",
    "get_changed_context",
    "changed_context",
    "find_symbol_context",
    "symbol_context",
}
_MAX_CONTEXT_TEXT_CHARS = 3_500


@dataclass(frozen=True)
class _EvidenceLocationRequest:
    """One finding-cited location ordered by deterministic retention priority."""

    path: str
    start_line: int
    end_line: int
    role: str
    candidate_id: str = ""
    retrieval_source: str = ""
    context_manifest_id: str = ""
    context_hash: str = ""
    edge_kind: str = ""
    edge_confidence: float | None = None
    resolver: str = ""
    evidence_eligibility: str = ""
    request_kind: str = ""


@dataclass(frozen=True)
class _AppendOutcome:
    """One bounded append attempt used for exact retention attribution."""

    status: str
    clipped: bool = False

    def __bool__(self) -> bool:
        return self.status in {"added", "already_retained"}


def capture_verifier_tool_evidence(
    entries: list[dict[str, Any]],
    workspace_root: Path | None,
) -> list[dict[str, Any]]:
    """Capture successful context-tool results independently of the feedback window."""
    captured: list[dict[str, Any]] = []
    for entry in entries:
        tool_call = entry.get("tool_call")
        function = tool_call.get("function") if isinstance(tool_call, dict) else None
        if not isinstance(function, dict):
            continue
        tool_name = str(function.get("name", "")).strip()
        if tool_name not in VERIFIER_CONTEXT_TOOL_NAMES:
            continue
        result = entry.get("result")
        if result is not None and hasattr(result, "model_dump"):
            result_payload = result.model_dump(mode="python")
        elif isinstance(result, dict):
            result_payload = result
        else:
            continue
        if not bool(result_payload.get("ok")):
            continue
        data = result_payload.get("data")
        if not isinstance(data, dict):
            continue
        arguments = _parse_arguments(function.get("arguments", {}))
        captured.append(
            {
                "tool_name": tool_name,
                "arguments": _normalize_paths(arguments, workspace_root),
                "data": _normalize_paths(data, workspace_root),
            }
        )
    return captured


def build_candidate_verifier_context(
    candidates: list[FindingCandidate],
    request: ReviewRequest,
    tool_evidence: list[dict[str, Any]],
    *,
    max_chars: int = 12_000,
    context_manifests: list[dict[str, Any]] | None = None,
    context_mode: str = "graph_hybrid",
) -> list[dict[str, Any]]:
    """Build bounded hunk/window/symbol evidence associated with each candidate."""
    if not candidates:
        return []
    hunks_by_file = parse_unified_diff_hunks(request.diff_text or "")
    budget = max(800, max_chars // len(candidates))
    contexts: list[dict[str, Any]] = []
    for candidate in candidates:
        location = normalize_location(candidate.issue.location)
        requested_locations = _candidate_evidence_locations(candidate)
        context: dict[str, Any] = {
            "candidate_id": candidate.candidate_id,
            "location": location.canonical,
            "diff_hunks": [],
            "file_windows": [],
            "enclosing_symbols": [],
            "symbol_contexts": [],
            "context_manifest_id": "",
            "context_manifest_ids": [],
            "manifest_envelopes": [],
            "included_spans": [],
            "included_graph_paths": [],
            "excluded_low_confidence_paths": [],
            "verifier_context_budget_exhausted": False,
            "budget_exhausted_locations": [],
            "evidence_retention": [],
            "context_mode": context_mode,
            "evidence_policy": evidence_policy_for_mode(
                "agent_search" if context_mode == "agent_search" else "graph_hybrid"
            ).model_dump(mode="json"),
        }
        start = location.line if location.valid else None
        end = (location.end_line or start) if location.valid else None

        manifests: list[dict[str, Any]] = []
        if location.valid and location.path:
            manifests = _select_context_manifests(
                candidate, context_manifests or [], location.path, start, end
            )
        manifest_ids = [
            str(manifest.get("candidate_id", "")).strip()
            for manifest in manifests
            if str(manifest.get("candidate_id", "")).strip()
        ]
        context["context_manifest_ids"] = manifest_ids
        if manifest_ids:
            # Keep the scalar field as a compatibility alias for older callers.
            context["context_manifest_id"] = manifest_ids[0]
            context["manifest_envelopes"] = [
                _manifest_envelope(manifest) for manifest in manifests
            ]
        manifests_by_id = {
            str(manifest.get("candidate_id", "")).strip(): manifest
            for manifest in manifests
            if str(manifest.get("candidate_id", "")).strip()
        }

        # Retain actual finding citations in semantic order. Once the bounded
        # envelope is full, later roles are omitted without displacing the bug
        # anchor or cause/contract evidence. Structured evidence requests only
        # its declared source representation; legacy/related locations retain
        # the compatible best-effort search across available source types.
        for requested in requested_locations:
            append_outcomes: list[_AppendOutcome] = []
            if requested.context_manifest_id:
                manifest = manifests_by_id.get(requested.context_manifest_id)
                if manifest is not None:
                    append_outcomes.extend(
                        _append_matching_manifest_context(
                            context,
                            manifest,
                            requested=requested,
                            budget=budget,
                        )
                    )
                _mark_budget_exhaustion_if_needed(
                    context,
                    requested,
                    available=_requested_location_available(
                        requested,
                        hunks_by_file=hunks_by_file,
                        tool_evidence=tool_evidence,
                        manifests=manifests,
                    ),
                    retained=_requested_location_retained(context, requested),
                    append_outcomes=append_outcomes,
                )
                continue
            if _is_diff_retrieval_source(requested.retrieval_source):
                append_outcomes.extend(
                    _append_matching_diff_context(
                        context,
                        hunks_by_file,
                        requested=requested,
                        budget=budget,
                    )
                )
                _mark_budget_exhaustion_if_needed(
                    context,
                    requested,
                    available=_requested_location_available(
                        requested,
                        hunks_by_file=hunks_by_file,
                        tool_evidence=tool_evidence,
                        manifests=manifests,
                    ),
                    retained=_requested_location_retained(context, requested),
                    append_outcomes=append_outcomes,
                )
                continue
            if requested.retrieval_source:
                for entry in tool_evidence:
                    append_outcomes.extend(
                        _append_matching_tool_context(
                            context,
                            entry,
                            requested=requested,
                            budget=budget,
                        )
                    )
                _mark_budget_exhaustion_if_needed(
                    context,
                    requested,
                    available=_requested_location_available(
                        requested,
                        hunks_by_file=hunks_by_file,
                        tool_evidence=tool_evidence,
                        manifests=manifests,
                    ),
                    retained=_requested_location_retained(context, requested),
                    append_outcomes=append_outcomes,
                )
                continue

            append_outcomes.extend(
                _append_matching_diff_context(
                    context,
                    hunks_by_file,
                    requested=requested,
                    budget=budget,
                )
            )
            for entry in tool_evidence:
                append_outcomes.extend(
                    _append_matching_tool_context(
                        context,
                        entry,
                        requested=requested,
                        budget=budget,
                    )
                )
            for manifest in manifests:
                append_outcomes.extend(
                    _append_matching_manifest_context(
                        context, manifest, requested=requested, budget=budget
                    )
                )
            _mark_budget_exhaustion_if_needed(
                context,
                requested,
                available=_requested_location_available(
                    requested,
                    hunks_by_file=hunks_by_file,
                    tool_evidence=tool_evidence,
                    manifests=manifests,
                ),
                retained=_requested_location_retained(context, requested),
                append_outcomes=append_outcomes,
            )

        # Preserve the prior manifest envelope when budget remains, but only
        # after every explicitly cited location has had a chance to be retained.
        for manifest in manifests:
            for span in manifest.get("included_spans", []):
                if isinstance(span, dict):
                    _append_bounded(
                        context,
                        "included_spans",
                        _with_manifest_id(manifest, span),
                        budget,
                    )
            for path in manifest.get("included_graph_paths", []):
                if isinstance(path, dict):
                    _append_bounded(
                        context,
                        "included_graph_paths",
                        _with_manifest_id(manifest, path),
                        budget,
                    )
            for path in manifest.get("excluded_low_confidence_paths", []):
                if isinstance(path, dict):
                    _append_bounded(
                        context,
                        "excluded_low_confidence_paths",
                        _with_manifest_id(manifest, path),
                        budget,
                    )
        contexts.append(context)
    return contexts


def _candidate_evidence_locations(
    candidate: FindingCandidate,
) -> list[_EvidenceLocationRequest]:
    """Return deduplicated finding locations in fail-closed retention order."""

    issue = candidate.issue
    requested: list[_EvidenceLocationRequest] = []
    seen: set[tuple[str, int, int, str, str, str, str, str, float | None, str, str]] = set()

    def add(
        path: str,
        start: int | None,
        end: int | None,
        role: str,
        *,
        retrieval_source: str = "",
        context_manifest_id: str = "",
        context_hash: str = "",
        edge_kind: str = "",
        edge_confidence: float | None = None,
        resolver: str = "",
        evidence_eligibility: str = "",
        request_kind: str = "location",
    ) -> None:
        normalized_path = _normalized_path(path)
        if not normalized_path or start is None:
            return
        normalized_end = end or start
        normalized_source = str(retrieval_source or "").strip().lower()
        normalized_manifest = str(context_manifest_id or "").strip()
        normalized_hash = str(context_hash or "").strip()
        normalized_edge_kind = str(edge_kind or "").strip()
        normalized_resolver = str(resolver or "").strip()
        key = (
            normalized_path,
            start,
            normalized_end,
            role,
            normalized_source,
            normalized_manifest,
            normalized_hash,
            normalized_edge_kind,
            edge_confidence,
            normalized_resolver,
            str(evidence_eligibility or "").strip(),
        )
        if key in seen:
            return
        seen.add(key)
        requested.append(
            _EvidenceLocationRequest(
                path=normalized_path,
                start_line=start,
                end_line=normalized_end,
                role=role,
                candidate_id=candidate.candidate_id,
                retrieval_source=normalized_source,
                context_manifest_id=normalized_manifest,
                context_hash=normalized_hash,
                edge_kind=normalized_edge_kind,
                edge_confidence=edge_confidence,
                resolver=normalized_resolver,
                evidence_eligibility=str(evidence_eligibility or "").strip(),
                request_kind=request_kind,
            )
        )

    location = normalize_location(issue.location)
    if location.valid:
        add(
            location.path or "",
            location.line,
            location.end_line,
            "primary",
        )
    if issue.primary_anchor is not None:
        add(
            issue.primary_anchor.file,
            issue.primary_anchor.line,
            issue.primary_anchor.end_line,
            "primary",
        )
    for role, evidence_items in (
        ("cause", issue.cause_evidence),
        ("contract", issue.contract_evidence),
        ("trigger", issue.trigger_evidence),
        ("impact", issue.impact_evidence),
    ):
        for evidence in evidence_items:
            add(
                evidence.file,
                evidence.line,
                evidence.end_line,
                role,
                retrieval_source=evidence.retrieval_source,
                context_manifest_id=evidence.context_manifest_id,
                context_hash=evidence.context_hash,
                edge_kind=evidence.edge_kind,
                edge_confidence=evidence.edge_confidence,
                resolver=evidence.resolver,
                evidence_eligibility=evidence.evidence_eligibility,
                request_kind="evidence",
            )
    for related in issue.related_locations:
        add(related.file, related.line, related.end_line, "related")
    return requested


def _is_diff_retrieval_source(value: str) -> bool:
    return value in {"git_diff", "diff", "review_diff", "changed_hunk"}


def _append_matching_manifest_context(
    context: dict[str, Any],
    manifest: dict[str, Any],
    *,
    requested: _EvidenceLocationRequest,
    budget: int,
) -> list[_AppendOutcome]:
    """Retain manifest spans and graph paths that cover one cited location."""

    outcomes: list[_AppendOutcome] = []
    for span in manifest.get("included_spans", []):
        if isinstance(span, dict) and _manifest_span_matches_request(
            manifest, span, requested
        ):
            outcomes.append(
                _append_bounded(
                    context,
                    "included_spans",
                    _with_manifest_id(manifest, span),
                    budget,
                )
            )
    for path in manifest.get("included_graph_paths", []):
        if not isinstance(path, dict) or not _graph_path_matches_request(
            manifest, path, requested
        ):
            continue
        outcomes.append(
            _append_bounded(
                context,
                "included_graph_paths",
                _with_manifest_id(manifest, path),
                budget,
            )
        )
    return outcomes


def _append_matching_diff_context(
    context: dict[str, Any],
    hunks_by_file: dict[str, list[ParsedDiffHunk]],
    *,
    requested: _EvidenceLocationRequest,
    budget: int,
) -> list[_AppendOutcome]:
    """Retain every diff hunk explicitly cited by the finding."""

    outcomes: list[_AppendOutcome] = []
    for hunk in hunks_by_file.get(requested.path, []):
        hunk_end = hunk.new_start + max(0, hunk.new_count - 1)
        if not _ranges_overlap(
            requested.start_line,
            requested.end_line,
            hunk.new_start,
            hunk_end,
        ):
            continue
        outcomes.append(
            _append_bounded(
                context,
                "diff_hunks",
                {
                    "path": requested.path,
                    "header": hunk.header,
                    "new_start": hunk.new_start,
                    "new_count": hunk.new_count,
                    "changed_new_lines": list(hunk.changed_new_lines),
                    "text": "\n".join([hunk.header, *hunk.lines]),
                    "source": "diff",
                },
                budget,
            )
        )
    return outcomes


def _append_matching_tool_context(
    context: dict[str, Any],
    entry: dict[str, Any],
    *,
    requested: _EvidenceLocationRequest,
    budget: int,
    respect_requested_source: bool = True,
) -> list[_AppendOutcome]:
    outcomes: list[_AppendOutcome] = []
    tool_name = str(entry.get("tool_name", "")).strip()
    if tool_name not in VERIFIER_CONTEXT_TOOL_NAMES:
        return outcomes
    if respect_requested_source and not _tool_source_matches(
        requested.retrieval_source, tool_name
    ):
        return outcomes
    arguments = entry.get("arguments")
    data = entry.get("data")
    if not isinstance(arguments, dict) or not isinstance(data, dict):
        return outcomes
    data_path = _entry_path(data, arguments)

    if tool_name in {"get_changed_context", "changed_context"}:
        if data_path != requested.path:
            return outcomes
        hunk = data.get("hunk")
        hunk_relevant = _payload_matches_range(
            hunk if isinstance(hunk, dict) else {},
            requested.start_line,
            requested.end_line,
        )
        if isinstance(hunk, dict) and hunk_relevant:
            outcomes.append(
                _append_bounded(
                    context,
                    "diff_hunks",
                    {**hunk, "path": data_path, "source": tool_name},
                    budget,
                )
            )
        window = data.get("file_window")
        window_relevant = _payload_matches_range(
            window if isinstance(window, dict) else {},
            requested.start_line,
            requested.end_line,
        )
        if isinstance(window, dict) and window_relevant:
            outcomes.append(
                _append_bounded(
                    context,
                    "file_windows",
                    {**window, "path": data_path, "source": tool_name},
                    budget,
                )
            )
        outcomes.extend(
            _append_symbols(
                context,
                data.get("enclosing_symbols"),
                requested=requested,
                source=tool_name,
                budget=budget,
            )
        )
        return outcomes

    if tool_name == "read_file":
        if data_path != requested.path:
            return outcomes
        start_line = _as_int(data.get("start_line"))
        line_count = _as_int(data.get("line_count")) or 0
        end_line = start_line + max(0, line_count - 1) if start_line else None
        content = data.get("content")
        if not _ranges_overlap(
            requested.start_line, requested.end_line, start_line, end_line
        ) or not isinstance(content, str):
            return outcomes
        retained_window = _retained_read_window(
            data,
            path=data_path,
            requested_start=requested.start_line,
            requested_end=requested.end_line,
            source=tool_name,
        )
        if retained_window is None:
            return outcomes
        if len(content) <= _MAX_CONTEXT_TEXT_CHARS and _read_window_fully_returned(
            data
        ):
            full_outcome = _append_bounded(
                context,
                "file_windows",
                {
                    "path": data_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "content": content,
                    "truncated": bool(data.get("truncated", False)),
                    "source": tool_name,
                },
                budget,
            )
            outcomes.append(full_outcome)
            if full_outcome:
                return outcomes
        outcomes.append(
            _append_bounded(
                context,
                "file_windows",
                retained_window,
                budget,
            )
        )
        return outcomes

    records: list[dict[str, Any]] = []
    for key in ("definitions", "references", "enclosing_symbols"):
        raw_records = data.get(key)
        if not isinstance(raw_records, list):
            continue
        records.extend(item for item in raw_records if isinstance(item, dict))
    matching_records = [
        item
        for item in records
        if _location_overlaps_record(
            requested.path,
            requested.start_line,
            requested.end_line,
            item,
        )
    ]
    scoped_to_requested = _normalized_path(arguments.get("path", "")) == requested.path
    if not matching_records and not (
        scoped_to_requested
        and any(
            _normalized_path(item.get("path", "")) == requested.path for item in records
        )
    ):
        return outcomes
    directly_relevant = any(
        _normalized_path(item.get("path", "")) == requested.path
        for item in matching_records
    )
    if not scoped_to_requested and not directly_relevant:
        return outcomes
    outcomes.append(
        _append_bounded(
            context,
            "symbol_contexts",
            {
                "source": tool_name,
                "symbol": data.get("symbol", arguments.get("symbol", "")),
                "backend": data.get("backend", ""),
                "language": data.get("language", ""),
                "definitions": _clip_records(
                    data.get("definitions"), requested=requested
                ),
                "references": _clip_records(
                    data.get("references"), requested=requested
                ),
                "enclosing_symbols": _clip_records(
                    data.get("enclosing_symbols"), requested=requested
                ),
                "truncated": bool(data.get("truncated", False)),
                "warnings": data.get("warnings", []),
            },
            budget,
        )
    )
    outcomes.extend(
        _append_symbols(
            context,
            data.get("enclosing_symbols"),
            requested=requested,
            source=tool_name,
            budget=budget,
        )
    )
    return outcomes


def _tool_source_matches(requested_source: str, tool_name: str) -> bool:
    """Match aliases without upgrading one successful tool source to another."""

    if not requested_source:
        return True
    alias_groups = (
        {"get_changed_context", "changed_context"},
        {"find_symbol_context", "symbol_context"},
    )
    if requested_source == tool_name:
        return True
    return any(
        requested_source in aliases and tool_name in aliases for aliases in alias_groups
    )


def _retained_read_window(
    data: dict[str, Any],
    *,
    path: str,
    requested_start: int,
    requested_end: int,
    source: str,
) -> dict[str, Any] | None:
    """Retain only cited lines that are present in a successful read result.

    Large read windows must not consume the whole candidate-context budget, but
    their metadata also must not authorize lines whose content was clipped or
    never returned. Slicing to the cited range keeps provenance fail-closed and
    leaves room for later trigger/impact evidence in the bounded envelope.
    """

    source_start = _as_int(data.get("start_line"))
    declared_count = _as_int(data.get("line_count")) or 0
    content = data.get("content")
    if source_start is None or declared_count <= 0 or not isinstance(content, str):
        return None
    content_lines = content.splitlines()
    if not content_lines:
        return None
    declared_end = source_start + declared_count - 1
    retained_start = max(requested_start, source_start)
    retained_end = min(requested_end, declared_end)
    if retained_start > retained_end:
        return None
    numbered_lines = _numbered_read_lines(content_lines, source_start, declared_end)
    retained_lines = [
        numbered_lines[line_number]
        for line_number in range(retained_start, retained_end + 1)
        if line_number in numbered_lines
    ]
    if len(retained_lines) != retained_end - retained_start + 1:
        return None
    retained_content = "\n".join(retained_lines)
    return {
        "path": path,
        "start_line": retained_start,
        "end_line": retained_start + len(retained_lines) - 1,
        "content": retained_content,
        "truncated": bool(data.get("truncated", False))
        or retained_start != source_start
        or retained_end != declared_end,
        "source": source,
    }


def _read_window_fully_returned(data: dict[str, Any]) -> bool:
    """Return whether read content covers every line claimed by its metadata."""

    source_start = _as_int(data.get("start_line"))
    declared_count = _as_int(data.get("line_count")) or 0
    content = data.get("content")
    if source_start is None or declared_count <= 0 or not isinstance(content, str):
        return False
    declared_end = source_start + declared_count - 1
    numbered_lines = _numbered_read_lines(
        content.splitlines(), source_start, declared_end
    )
    return all(
        line_number in numbered_lines
        for line_number in range(source_start, declared_end + 1)
    )


def _numbered_read_lines(
    content_lines: list[str], source_start: int, declared_end: int
) -> dict[int, str]:
    numbered_lines: dict[int, str] = {}
    for content_line in content_lines:
        prefix, separator, _remainder = content_line.partition(":")
        if separator and prefix.strip().isdigit():
            line_number = int(prefix.strip())
            if source_start <= line_number <= declared_end:
                numbered_lines[line_number] = content_line
    return numbered_lines


def _append_symbols(
    context: dict[str, Any],
    raw_symbols: Any,
    *,
    requested: _EvidenceLocationRequest,
    source: str,
    budget: int,
) -> list[_AppendOutcome]:
    outcomes: list[_AppendOutcome] = []
    if not isinstance(raw_symbols, list):
        return outcomes
    for raw_symbol in raw_symbols:
        if not isinstance(raw_symbol, dict):
            continue
        path = _normalized_path(raw_symbol.get("path", requested.path))
        if path != requested.path:
            continue
        symbol_start = _as_int(raw_symbol.get("line"))
        symbol_end = _as_int(raw_symbol.get("end_line")) or symbol_start
        if not _ranges_overlap(
            requested.start_line,
            requested.end_line,
            symbol_start,
            symbol_end,
        ):
            continue
        outcomes.append(
            _append_bounded(
                context,
                "enclosing_symbols",
                {**raw_symbol, "path": path, "source": source},
                budget,
            )
        )
    return outcomes


def _append_bounded(
    context: dict[str, Any],
    key: str,
    value: dict[str, Any],
    budget: int,
) -> _AppendOutcome:
    clipped = _clip_payload(value)
    payload_clipped = clipped != value
    text_clipped = _evidence_text_was_clipped(value, clipped)
    if payload_clipped:
        clipped = {
            **clipped,
            "_verifier_payload_clipped": True,
            "_verifier_text_clipped": text_clipped,
        }
    bucket = context[key]
    if not isinstance(bucket, list):
        return _AppendOutcome("invalid_bucket", clipped=payload_clipped)
    fingerprint = json.dumps(clipped, ensure_ascii=True, sort_keys=True, default=str)
    if any(
        json.dumps(item, ensure_ascii=True, sort_keys=True, default=str) == fingerprint
        for item in bucket
    ):
        return _AppendOutcome("already_retained", clipped=payload_clipped)
    bucket.append(clipped)
    serialized = json.dumps(
        {
            item_key: item_value
            for item_key, item_value in context.items()
            if item_key
            not in {
                "verifier_context_budget_exhausted",
                "budget_exhausted_locations",
                "evidence_retention",
            }
        },
        ensure_ascii=True,
        default=str,
    )
    if len(serialized) > budget:
        bucket.pop()
        return _AppendOutcome("budget_rejected", clipped=payload_clipped)
    return _AppendOutcome("added", clipped=payload_clipped)


def context_budget_exhausted_for_location(
    context: dict[str, Any] | None,
    location: Any,
) -> bool:
    """Return whether a cited location was observed but omitted by the budget."""

    if not context or not context.get("verifier_context_budget_exhausted"):
        return False
    path = _normalized_path(getattr(location, "path", ""))
    line = _as_int(getattr(location, "line", None))
    if not path or line is None:
        return False
    end_line = _as_int(getattr(location, "end_line", None)) or line
    records = context.get("budget_exhausted_locations")
    return isinstance(records, list) and any(
        _location_overlaps_record(path, line, end_line, record)
        and str(record.get("request_kind", "location")) != "evidence"
        for record in records
        if isinstance(record, dict)
    )


def context_budget_exhausted_for_evidence(
    context: dict[str, Any] | None,
    evidence: Any,
    *,
    role: str = "",
) -> bool:
    """Return whether one exact evidence provenance was omitted by the budget."""

    if not context or not context.get("verifier_context_budget_exhausted"):
        return False
    requested = _request_from_evidence(evidence, role=role)
    if requested is None:
        return False
    records = context.get("budget_exhausted_locations")
    if not isinstance(records, list):
        return False
    exact_records = [
        record
        for record in records
        if isinstance(record, dict)
        and str(record.get("request_kind", "")) == "evidence"
    ]
    if exact_records:
        return any(
            _budget_record_matches_request(record, requested, role=role)
            for record in exact_records
        )
    # Compatibility for contexts produced before exact retention records existed.
    location = normalize_location(
        f"{requested.path}:{requested.start_line}-{requested.end_line}"
    )
    return context_budget_exhausted_for_location(context, location)


def _mark_budget_exhaustion_if_needed(
    context: dict[str, Any],
    requested: _EvidenceLocationRequest,
    *,
    available: bool,
    retained: bool | None = None,
    append_outcomes: list[_AppendOutcome] | tuple[_AppendOutcome, ...] = (),
) -> None:
    exact_retained = (
        _requested_location_retained(context, requested)
        if retained is None
        else retained
    )
    budget_limited = any(
        outcome.status == "budget_rejected" or outcome.clipped
        for outcome in append_outcomes
    )
    request_kind = _request_kind(requested)
    retention_record = {
        "candidate_id": requested.candidate_id
        or str(context.get("candidate_id", "")),
        "request_kind": request_kind,
        "path": requested.path,
        "start_line": requested.start_line,
        "end_line": requested.end_line,
        "role": requested.role,
        "retrieval_source": requested.retrieval_source,
        "context_manifest_id": requested.context_manifest_id,
        "context_hash": requested.context_hash,
        "edge_kind": requested.edge_kind,
        "edge_confidence": requested.edge_confidence,
        "resolver": requested.resolver,
        "evidence_eligibility": requested.evidence_eligibility,
        "available": available,
        "retained": exact_retained,
        "omission_reason": (
            ""
            if exact_retained
            else "not_available"
            if not available
            else "context_budget"
            if budget_limited
            else "not_retained"
        ),
        "append_outcomes": [
            {"status": outcome.status, "clipped": outcome.clipped}
            for outcome in append_outcomes
        ],
    }
    retention_records = context.setdefault("evidence_retention", [])
    if isinstance(retention_records, list) and retention_record not in retention_records:
        retention_records.append(retention_record)
    if not available or exact_retained or not budget_limited:
        return
    context["verifier_context_budget_exhausted"] = True
    records = context.setdefault("budget_exhausted_locations", [])
    if not isinstance(records, list):
        records = []
        context["budget_exhausted_locations"] = records
    record = {
        "candidate_id": requested.candidate_id
        or str(context.get("candidate_id", "")),
        "request_kind": request_kind,
        "path": requested.path,
        "start_line": requested.start_line,
        "end_line": requested.end_line,
        "role": requested.role,
        "retrieval_source": requested.retrieval_source,
        "context_manifest_id": requested.context_manifest_id,
        "context_hash": requested.context_hash,
        "edge_kind": requested.edge_kind,
        "edge_confidence": requested.edge_confidence,
        "resolver": requested.resolver,
        "evidence_eligibility": requested.evidence_eligibility,
        "omission_reason": "context_budget",
        "code": "verifier_context_budget_exhausted",
    }
    if record not in records:
        records.append(record)


def _request_kind(requested: _EvidenceLocationRequest) -> str:
    if requested.request_kind:
        return requested.request_kind
    if any(
        (
            requested.retrieval_source,
            requested.context_manifest_id,
            requested.context_hash,
            requested.edge_kind,
        )
    ):
        return "evidence"
    return "location"


def _request_from_evidence(
    evidence: Any, *, role: str = ""
) -> _EvidenceLocationRequest | None:
    path = _normalized_path(getattr(evidence, "file", ""))
    start_line = _as_int(getattr(evidence, "line", None))
    if not path or start_line is None:
        return None
    return _EvidenceLocationRequest(
        path=path,
        start_line=start_line,
        end_line=_as_int(getattr(evidence, "end_line", None)) or start_line,
        role=role,
        candidate_id=str(getattr(evidence, "candidate_id", "") or "").strip(),
        retrieval_source=str(
            getattr(evidence, "retrieval_source", "") or ""
        ).strip().lower(),
        context_manifest_id=str(
            getattr(evidence, "context_manifest_id", "") or ""
        ).strip(),
        context_hash=str(getattr(evidence, "context_hash", "") or "").strip(),
        edge_kind=str(getattr(evidence, "edge_kind", "") or "").strip(),
        edge_confidence=getattr(evidence, "edge_confidence", None),
        resolver=str(getattr(evidence, "resolver", "") or "").strip(),
        evidence_eligibility=str(
            getattr(evidence, "evidence_eligibility", "") or ""
        ).strip(),
        request_kind="evidence",
    )


def _budget_record_matches_request(
    record: dict[str, Any],
    requested: _EvidenceLocationRequest,
    *,
    role: str = "",
) -> bool:
    if requested.candidate_id and str(record.get("candidate_id", "")).strip() != (
        requested.candidate_id
    ):
        return False
    if _normalized_path(record.get("path", "")) != requested.path:
        return False
    record_start = _as_int(record.get("start_line"))
    record_end = _as_int(record.get("end_line")) or record_start
    if record_start != requested.start_line or record_end != requested.end_line:
        return False
    if role and str(record.get("role", "")) not in {"", role}:
        return False
    record_source = str(record.get("retrieval_source", "")).strip().lower()
    if requested.retrieval_source:
        if _is_diff_retrieval_source(requested.retrieval_source):
            if not _is_diff_retrieval_source(record_source):
                return False
        elif not _tool_source_matches(requested.retrieval_source, record_source):
            return False
    for key, expected in (
        ("context_manifest_id", requested.context_manifest_id),
        ("context_hash", requested.context_hash),
        ("edge_kind", requested.edge_kind),
        ("resolver", requested.resolver),
        ("evidence_eligibility", requested.evidence_eligibility),
    ):
        if expected and str(record.get(key, "")).strip() != expected:
            return False
    if requested.edge_confidence is not None:
        record_edge_confidence = record.get("edge_confidence")
        if record_edge_confidence is None:
            return False
        try:
            if float(record_edge_confidence) != float(requested.edge_confidence):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _requested_location_available(
    requested: _EvidenceLocationRequest,
    *,
    hunks_by_file: dict[str, list[ParsedDiffHunk]],
    tool_evidence: list[dict[str, Any]],
    manifests: list[dict[str, Any]],
) -> bool:
    """Check source availability independently from bounded retention."""

    if requested.context_manifest_id:
        return any(
            str(manifest.get("candidate_id", "")).strip()
            == requested.context_manifest_id
            and _manifest_covers_location(manifest, requested)
            for manifest in manifests
        )
    if _is_diff_retrieval_source(requested.retrieval_source):
        return _diff_covers_location(hunks_by_file, requested)
    if requested.retrieval_source:
        return any(
            _tool_covers_location(entry, requested) for entry in tool_evidence
        )
    return _diff_covers_location(hunks_by_file, requested) or any(
        _tool_covers_location(entry, requested) for entry in tool_evidence
    ) or any(_manifest_covers_location(manifest, requested) for manifest in manifests)


def _requested_location_retained(
    context: dict[str, Any], requested: _EvidenceLocationRequest
) -> bool:
    """Check exact retained provenance rather than borrowing a colocated record."""

    if requested.context_manifest_id:
        spans = context.get("included_spans", [])
        source_retained = isinstance(spans, list) and any(
            _manifest_span_matches_request(
                {"candidate_id": span.get("context_manifest_id", "")},
                span,
                requested,
                require_retained=True,
            )
            for span in spans
            if isinstance(span, dict)
        )
        if not source_retained:
            return False
        if not requested.edge_kind:
            return True
        paths = context.get("included_graph_paths", [])
        return isinstance(paths, list) and any(
            _graph_path_matches_request(
                {"candidate_id": path.get("context_manifest_id", "")},
                path,
                requested,
            )
            for path in paths
            if isinstance(path, dict)
        )
    if _is_diff_retrieval_source(requested.retrieval_source):
        records = context.get("diff_hunks", [])
        return isinstance(records, list) and any(
            _record_retains_location(
                requested.path,
                requested.start_line,
                requested.end_line,
                record,
            )
            for record in records
            if isinstance(record, dict)
            and _is_diff_retrieval_source(
                str(record.get("source", "diff")).strip().lower()
            )
        )
    if requested.retrieval_source:
        return _location_in_tool_context(
            context,
            requested.path,
            requested.start_line,
            requested.end_line,
            retrieval_source=requested.retrieval_source,
        )
    location = normalize_location(
        f"{requested.path}:{requested.start_line}-{requested.end_line}"
    )
    return location_in_candidate_context(context, location)


def _diff_covers_location(
    hunks_by_file: dict[str, list[ParsedDiffHunk]],
    requested: _EvidenceLocationRequest,
) -> bool:
    return any(
        _ranges_overlap(
            requested.start_line,
            requested.end_line,
            hunk.new_start,
            hunk.new_start + max(0, hunk.new_count - 1),
        )
        for hunk in hunks_by_file.get(requested.path, [])
    )


def _manifest_covers_location(
    manifest: dict[str, Any],
    requested: _EvidenceLocationRequest,
) -> bool:
    spans = manifest.get("included_spans", [])
    source_available = isinstance(spans, list) and any(
        _manifest_span_matches_request(manifest, span, requested)
        for span in spans
        if isinstance(span, dict)
    )
    if not source_available:
        return False
    if not requested.edge_kind:
        return True
    paths = manifest.get("included_graph_paths", [])
    return isinstance(paths, list) and any(
        _graph_path_matches_request(manifest, path, requested)
        for path in paths
        if isinstance(path, dict)
    )


def _manifest_span_matches_request(
    manifest: dict[str, Any],
    span: dict[str, Any],
    requested: _EvidenceLocationRequest,
    *,
    require_retained: bool = False,
) -> bool:
    manifest_id = str(manifest.get("candidate_id", "")).strip()
    span_manifest_id = str(span.get("context_manifest_id", "")).strip()
    if requested.context_manifest_id and manifest_id != requested.context_manifest_id:
        return False
    if (
        requested.context_manifest_id
        and span_manifest_id
        and span_manifest_id != requested.context_manifest_id
    ):
        return False
    if not _record_covers_location(
        requested.path,
        requested.start_line,
        requested.end_line,
        span,
    ):
        return False
    if requested.context_hash and str(span.get("context_hash", "")).strip() != (
        requested.context_hash
    ):
        return False
    span_source = str(span.get("retrieval_source", "")).strip().lower()
    if requested.retrieval_source and span_source and span_source != (
        requested.retrieval_source
    ):
        return False
    return not require_retained or _record_retains_location(
        requested.path,
        requested.start_line,
        requested.end_line,
        span,
    )


def _graph_path_matches_request(
    manifest: dict[str, Any],
    path: dict[str, Any],
    requested: _EvidenceLocationRequest,
) -> bool:
    manifest_id = str(manifest.get("candidate_id", "")).strip()
    path_manifest_id = str(path.get("context_manifest_id", "")).strip()
    if requested.context_manifest_id and manifest_id != requested.context_manifest_id:
        return False
    if (
        requested.context_manifest_id
        and path_manifest_id
        and path_manifest_id != requested.context_manifest_id
    ):
        return False
    if requested.edge_kind:
        if (
            path.get("evidence_eligibility") != "strong"
            or requested.evidence_eligibility != "strong"
            or requested.edge_confidence is None
            or requested.edge_confidence < 0.65
            or not requested.resolver
        ):
            return False
    edges = path.get("edges", [])
    if not isinstance(edges, list):
        return False
    return any(
        _graph_edge_matches_request(edge, requested)
        for edge in edges
        if isinstance(edge, dict)
    )


def _graph_edge_matches_request(
    edge: dict[str, Any], requested: _EvidenceLocationRequest
) -> bool:
    if not _record_covers_location(
        requested.path,
        requested.start_line,
        requested.end_line,
        edge,
    ):
        return False
    if not requested.edge_kind:
        return True
    if str(edge.get("kind", "")) != requested.edge_kind:
        return False
    if requested.resolver and str(edge.get("resolver", "")) != requested.resolver:
        return False
    if edge.get("evidence_eligibility") != "strong":
        return False
    try:
        return float(edge.get("confidence", 0.0) or 0.0) >= 0.65
    except (TypeError, ValueError):
        return False


def _tool_covers_location(
    entry: dict[str, Any],
    requested: _EvidenceLocationRequest,
) -> bool:
    tool_name = str(entry.get("tool_name", "")).strip()
    if tool_name not in VERIFIER_CONTEXT_TOOL_NAMES:
        return False
    if requested.retrieval_source and not _tool_source_matches(
        requested.retrieval_source, tool_name
    ):
        return False
    arguments = entry.get("arguments")
    data = entry.get("data")
    if not isinstance(arguments, dict) or not isinstance(data, dict):
        return False
    if _entry_path(data, arguments) != requested.path:
        return False
    if tool_name in {"get_changed_context", "changed_context"}:
        return any(
            isinstance(data.get(key), dict)
            and _payload_matches_range(
                data[key], requested.start_line, requested.end_line
            )
            for key in ("hunk", "file_window")
        )
    if tool_name == "read_file":
        return (
            _retained_read_window(
                data,
                path=requested.path,
                requested_start=requested.start_line,
                requested_end=requested.end_line,
                source=tool_name,
            )
            is not None
        )
    records = [
        item
        for key in ("definitions", "references", "enclosing_symbols")
        for item in data.get(key, [])
        if isinstance(data.get(key), list) and isinstance(item, dict)
    ]
    return any(
        _location_overlaps_record(
            requested.path,
            requested.start_line,
            requested.end_line,
            record,
        )
        for record in records
    )


def location_in_candidate_context(
    context: dict[str, Any] | None,
    location: Any,
) -> bool:
    """Return whether a parsed location is covered by retained candidate context."""
    if not context or not location.valid or not location.path or location.line is None:
        return False
    location_end = location.end_line or location.line
    spans = context.get("included_spans")
    if isinstance(spans, list):
        for span in spans:
            if _record_retains_location(
                location.path, location.line, location_end, span
            ):
                return True
    for key in ("diff_hunks", "file_windows", "enclosing_symbols"):
        records = context.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            if _record_retains_location(
                location.path, location.line, location_end, record
            ):
                return True
    symbol_contexts = context.get("symbol_contexts")
    if not isinstance(symbol_contexts, list):
        return False
    for symbol_context in symbol_contexts:
        if not isinstance(symbol_context, dict):
            continue
        for key in ("definitions", "references", "enclosing_symbols"):
            records = symbol_context.get(key)
            if not isinstance(records, list):
                continue
            for record in records:
                if _record_retains_location(
                    location.path, location.line, location_end, record
                ):
                    return True
    return False


def provenance_in_candidate_context(
    context: dict[str, Any] | None,
    evidence: Any,
    *,
    min_edge_confidence: float = 0.65,
) -> bool:
    """Validate diff/tool/manifest provenance under the selected source policy."""

    if context is None:
        return False
    policy_raw = context.get("evidence_policy", {})
    mode = str(context.get("context_mode", "graph_hybrid"))
    policy = evidence_policy_for_mode(
        "agent_search" if mode == "agent_search" else "graph_hybrid"
    )
    if isinstance(policy_raw, dict) and policy_raw:
        try:
            policy = policy.model_validate(policy_raw)
        except Exception:  # noqa: BLE001
            policy = evidence_policy_for_mode(
                "agent_search" if mode == "agent_search" else "graph_hybrid"
            )
    manifest_id = str(context.get("context_manifest_id", "")).strip()
    raw_manifest_ids = context.get("context_manifest_ids")
    manifest_ids = (
        {
            str(item).strip()
            for item in raw_manifest_ids
            if str(item).strip()
        }
        if isinstance(raw_manifest_ids, list)
        else set()
    )
    if not manifest_ids and manifest_id:
        manifest_ids.add(manifest_id)
    evidence_manifest = str(getattr(evidence, "context_manifest_id", ""))
    file = _normalized_path(getattr(evidence, "file", ""))
    line = _as_int(getattr(evidence, "line", None))
    end_line = _as_int(getattr(evidence, "end_line", None)) or line
    digest = str(getattr(evidence, "context_hash", ""))
    retrieval_source = str(getattr(evidence, "retrieval_source", "")).strip().lower()
    if not file or line is None or not retrieval_source:
        return False

    if evidence_manifest:
        if not policy.allow_manifest_evidence:
            return False
        if not manifest_ids or evidence_manifest not in manifest_ids:
            return False
        if policy.require_context_hash_for_manifest and not digest:
            return False
        return _manifest_provenance_valid(
            context,
            evidence,
            file=file,
            line=line,
            end_line=end_line or line,
            digest=digest,
            min_edge_confidence=min_edge_confidence,
        )

    if digest:
        # A hash without its manifest identity is not an auditable claim.
        return False
    if policy.require_manifest:
        return False
    if getattr(evidence, "edge_kind", ""):
        return False
    if retrieval_source in {"git_diff", "diff", "review_diff", "changed_hunk"}:
        return policy.allow_diff_evidence and _location_in_records(
            context.get("diff_hunks"), file, line, end_line or line
        )
    if not policy.allow_tool_evidence:
        return False
    return _location_in_tool_context(
        context,
        file,
        line,
        end_line or line,
        retrieval_source=retrieval_source,
    )


def _manifest_provenance_valid(
    context: dict[str, Any],
    evidence: Any,
    *,
    file: str,
    line: int,
    end_line: int,
    digest: str,
    min_edge_confidence: float,
) -> bool:
    """Validate exact manifest span/hash and strong graph-edge eligibility."""

    spans = context.get("included_spans")
    matching_span = False
    if isinstance(spans, list):
        for span in spans:
            if not isinstance(span, dict):
                continue
            span_manifest = str(span.get("context_manifest_id", "")).strip()
            if span_manifest and span_manifest != str(
                getattr(evidence, "context_manifest_id", "")
            ).strip():
                continue
            if str(span.get("context_hash", "")) != digest:
                continue
            span_source = str(span.get("retrieval_source", "")).strip().lower()
            evidence_source = str(
                getattr(evidence, "retrieval_source", "")
            ).strip().lower()
            if span_source and evidence_source and span_source != evidence_source:
                continue
            if _record_retains_location(file, line, end_line, span):
                matching_span = True
                break
    if not matching_span:
        return False
    edge_kind = str(getattr(evidence, "edge_kind", ""))
    if not edge_kind:
        return True
    edge_confidence = getattr(evidence, "edge_confidence", None)
    eligibility = str(getattr(evidence, "evidence_eligibility", ""))
    resolver = str(getattr(evidence, "resolver", ""))
    if (
        edge_confidence is None
        or float(edge_confidence) < min_edge_confidence
        or eligibility != "strong"
        or not resolver
    ):
        return False
    graph_paths = context.get("included_graph_paths")
    if not isinstance(graph_paths, list):
        return False
    for path in graph_paths:
        if not isinstance(path, dict) or path.get("evidence_eligibility") != "strong":
            continue
        path_manifest = str(path.get("context_manifest_id", "")).strip()
        if path_manifest and path_manifest != str(
            getattr(evidence, "context_manifest_id", "")
        ).strip():
            continue
        for edge in path.get("edges", []):
            if not isinstance(edge, dict):
                continue
            if (
                _record_covers_location(file, line, end_line, edge)
                and str(edge.get("kind", "")) == edge_kind
                and str(edge.get("resolver", "")) == resolver
                and float(edge.get("confidence", 0.0) or 0.0) >= min_edge_confidence
                and edge.get("evidence_eligibility") == "strong"
            ):
                return True
    return False


def _location_in_records(records: Any, file: str, line: int, end_line: int) -> bool:
    return isinstance(records, list) and any(
        _record_retains_location(file, line, end_line, record) for record in records
    )


def _location_in_tool_context(
    context: dict[str, Any],
    file: str,
    line: int,
    end_line: int,
    *,
    retrieval_source: str,
) -> bool:
    for key in ("diff_hunks", "file_windows", "enclosing_symbols"):
        records = context.get(key)
        if isinstance(records, list) and any(
            isinstance(record, dict)
            and _tool_source_matches(retrieval_source, str(record.get("source", "")))
            and _record_retains_location(file, line, end_line, record)
            for record in records
        ):
            return True
    symbol_contexts = context.get("symbol_contexts")
    if not isinstance(symbol_contexts, list):
        return False
    for symbol_context in symbol_contexts:
        if not isinstance(symbol_context, dict):
            continue
        if not _tool_source_matches(
            retrieval_source, str(symbol_context.get("source", ""))
        ):
            continue
        for key in ("definitions", "references", "enclosing_symbols"):
            if _location_in_records(symbol_context.get(key), file, line, end_line):
                return True
    return False


def _select_context_manifests(
    candidate: FindingCandidate,
    manifests: list[dict[str, Any]],
    path: str,
    start: int | None,
    end: int | None,
) -> list[dict[str, Any]]:
    """Select every explicitly declared manifest, with one legacy fallback."""

    requested_ids = {str(candidate.issue.context_manifest_id or "").strip()}
    requested_ids.update(
        str(evidence.context_manifest_id or "").strip()
        for evidence in candidate.issue.all_evidence()
        if str(evidence.context_manifest_id or "").strip()
    )
    if requested_ids:
        return [
            manifest
            for manifest in manifests
            if str(manifest.get("candidate_id", "")).strip() in requested_ids
        ]

    # Legacy evidence has no manifest id. Retain the prior deterministic anchor
    # fallback, but never broaden an explicitly identified manifest to another.
    for manifest in manifests:
        anchor = manifest.get("changed_anchor")
        if (
            not isinstance(anchor, dict)
            or _normalized_path(anchor.get("file", "")) != path
        ):
            continue
        anchor_start = _as_int(anchor.get("line"))
        anchor_end = _as_int(anchor.get("end_line")) or anchor_start
        if _ranges_overlap(start, end, anchor_start, anchor_end):
            return [manifest]
    return []


def _select_context_manifest(
    candidate: FindingCandidate,
    manifests: list[dict[str, Any]],
    path: str,
    start: int | None,
    end: int | None,
) -> dict[str, Any] | None:
    """Compatibility wrapper for callers that still expect one manifest."""

    return next(
        iter(_select_context_manifests(candidate, manifests, path, start, end)),
        None,
    )


def _with_manifest_id(
    manifest: dict[str, Any], value: dict[str, Any]
) -> dict[str, Any]:
    """Tag flattened retained records so provenance can distinguish manifests."""

    manifest_id = str(manifest.get("candidate_id", "")).strip()
    if not manifest_id:
        return dict(value)
    return {**value, "context_manifest_id": manifest_id}


def _manifest_envelope(manifest: dict[str, Any]) -> dict[str, Any]:
    """Build a compact prompt-visible manifest identity and hash envelope."""

    spans = manifest.get("included_spans", [])
    span_envelopes = [
        {
            "span_id": span.get("span_id", ""),
            "file": span.get("file", span.get("path", "")),
            "start_line": span.get("start_line", span.get("line")),
            "end_line": span.get("end_line", span.get("line")),
            "context_hash": span.get("context_hash", ""),
        }
        for span in spans
        if isinstance(span, dict)
    ]
    paths = manifest.get("included_graph_paths", [])
    return {
        "context_manifest_id": str(manifest.get("candidate_id", "")).strip(),
        "changed_anchor": manifest.get("changed_anchor", {}),
        "included_spans": span_envelopes,
        "included_graph_path_ids": [
            path.get("path_id", "")
            for path in paths
            if isinstance(path, dict) and path.get("path_id")
        ],
    }


def _location_overlaps_record(
    path: str,
    start: int,
    end: int,
    record: Any,
) -> bool:
    if not isinstance(record, dict):
        return False
    record_path = _normalized_path(record.get("path") or record.get("file", ""))
    if record_path != path:
        return False
    record_start = _as_int(record.get("start_line"))
    if record_start is None:
        record_start = _as_int(record.get("line"))
    if record_start is None:
        record_start = _as_int(record.get("new_start"))
    if record_start is None:
        return False
    record_end = _as_int(record.get("end_line"))
    if record_end is None and "new_count" in record:
        count = _as_int(record.get("new_count")) or 1
        record_end = record_start + max(0, count - 1)
    record_end = record_end or record_start
    return start <= record_end and record_start <= end


def _record_covers_location(
    path: str,
    start: int,
    end: int,
    record: Any,
) -> bool:
    if not isinstance(record, dict):
        return False
    record_path = _normalized_path(record.get("path") or record.get("file", ""))
    if record_path != path:
        return False
    record_start = _as_int(record.get("start_line"))
    if record_start is None:
        record_start = _as_int(record.get("line"))
    if record_start is None:
        record_start = _as_int(record.get("new_start"))
    if record_start is None:
        return False
    record_end = _as_int(record.get("end_line"))
    if record_end is None and "new_count" in record:
        count = _as_int(record.get("new_count")) or 1
        record_end = record_start + max(0, count - 1)
    record_end = record_end or record_start
    return record_start <= start and end <= record_end


def _record_retains_location(
    path: str,
    start: int,
    end: int,
    record: Any,
) -> bool:
    if not _record_covers_location(path, start, end, record):
        return False
    if not isinstance(record, dict) or not record.get("_verifier_text_clipped"):
        return True
    content = record.get("content")
    if not isinstance(content, str):
        content = record.get("text")
    if not isinstance(content, str):
        return False
    record_start = (
        _as_int(record.get("start_line"))
        or _as_int(record.get("line"))
        or _as_int(record.get("new_start"))
    )
    if record_start is None:
        return False
    record_end = _as_int(record.get("end_line"))
    if record_end is None and "new_count" in record:
        count = _as_int(record.get("new_count")) or 1
        record_end = record_start + max(0, count - 1)
    record_end = record_end or record_start
    numbered = _numbered_read_lines(content.splitlines(), record_start, record_end)
    retained_lines = [numbered.get(line_number, "") for line_number in range(start, end + 1)]
    return all(
        line and "...(truncated " not in line for line in retained_lines
    )


def _evidence_text_was_clipped(original: Any, clipped: Any) -> bool:
    if isinstance(original, dict) and isinstance(clipped, dict):
        for key, value in original.items():
            clipped_value = clipped.get(key)
            if key in {"content", "text"} and value != clipped_value:
                return True
            if _evidence_text_was_clipped(value, clipped_value):
                return True
        return False
    if isinstance(original, list) and isinstance(clipped, list):
        if any(
            _evidence_text_was_clipped(value, clipped[index])
            for index, value in enumerate(original[: len(clipped)])
        ):
            return True
        return any(_contains_evidence_text(value) for value in original[len(clipped) :])
    return False


def _contains_evidence_text(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in {"content", "text"} or _contains_evidence_text(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_evidence_text(item) for item in value)
    return False


def _clip_payload(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        clipped = {
            str(item_key): _clip_payload(item, key=str(item_key))
            for item_key, item in value.items()
        }
        if clipped != value:
            return {
                **clipped,
                "_verifier_payload_clipped": True,
                "_verifier_text_clipped": _evidence_text_was_clipped(
                    value, clipped
                ),
            }
        return clipped
    if isinstance(value, list):
        return [_clip_payload(item, key=key) for item in value[:20]]
    if isinstance(value, str):
        limit = _MAX_CONTEXT_TEXT_CHARS if key in {"content", "text"} else 1_500
        if len(value) <= limit:
            return value
        return value[:limit] + f"...(truncated {len(value) - limit} chars)"
    return value


def _clip_records(
    value: Any,
    *,
    requested: _EvidenceLocationRequest | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    records = [item for item in value if isinstance(item, dict)]
    if requested is not None:
        matching = [
            item
            for item in records
            if _record_covers_location(
                requested.path,
                requested.start_line,
                requested.end_line,
                item,
            )
        ]
        records = [*matching, *[item for item in records if item not in matching]]
    return records[:10]


def _payload_matches_range(
    payload: dict[str, Any],
    candidate_start: int | None,
    candidate_end: int | None,
) -> bool:
    changed_lines = payload.get("changed_new_lines")
    if isinstance(changed_lines, list) and candidate_start is not None:
        numeric_lines = [line for line in changed_lines if isinstance(line, int)]
        if numeric_lines:
            return _lines_overlap(
                candidate_start,
                candidate_end or candidate_start,
                numeric_lines,
            )
    start = _as_int(payload.get("start_line")) or _as_int(payload.get("new_start"))
    end = _as_int(payload.get("end_line"))
    if end is None and start is not None:
        count = _as_int(payload.get("new_count")) or 1
        end = start + max(0, count - 1)
    return _ranges_overlap(candidate_start, candidate_end, start, end)


def _lines_overlap(start: int, end: int, lines: list[int]) -> bool:
    return any(start <= line <= end for line in lines)


def _ranges_overlap(
    left_start: int | None,
    left_end: int | None,
    right_start: int | None,
    right_end: int | None,
) -> bool:
    if left_start is None or right_start is None:
        return True
    left_end = left_end or left_start
    right_end = right_end or right_start
    return left_start <= right_end and right_start <= left_end


def _entry_path(data: dict[str, Any], arguments: dict[str, Any]) -> str:
    return _normalized_path(
        data.get("file_path")
        or data.get("path")
        or arguments.get("file_path")
        or arguments.get("path")
        or ""
    )


def _parse_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_paths(value: Any, workspace_root: Path | None, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(item_key): _normalize_paths(item, workspace_root, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_paths(item, workspace_root, key=key) for item in value]
    if isinstance(value, str) and key in {"file_path", "path"}:
        return _relative_path(value, workspace_root)
    return value


def _relative_path(value: str, workspace_root: Path | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if workspace_root is not None:
        try:
            path = Path(raw)
            resolved = (
                path.resolve()
                if path.is_absolute()
                else (workspace_root / path).resolve()
            )
            return resolved.relative_to(workspace_root.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return _normalized_path(raw)


def _normalized_path(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
