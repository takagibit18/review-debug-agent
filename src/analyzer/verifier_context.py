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


@dataclass(frozen=True)
class _EvidenceLocationRequest:
    """One finding-cited location ordered by deterministic retention priority."""

    path: str
    start_line: int
    end_line: int
    role: str
    retrieval_source: str = ""
    context_manifest_id: str = ""


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
            "included_spans": [],
            "included_graph_paths": [],
            "excluded_low_confidence_paths": [],
            "context_mode": context_mode,
            "evidence_policy": evidence_policy_for_mode(
                "agent_search" if context_mode == "agent_search" else "graph_hybrid"
            ).model_dump(mode="json"),
        }
        start = location.line if location.valid else None
        end = (location.end_line or start) if location.valid else None

        manifest = None
        if location.valid and location.path:
            manifest = _select_context_manifest(
                candidate, context_manifests or [], location.path, start, end
            )
        if manifest is not None:
            context["context_manifest_id"] = str(manifest.get("candidate_id", ""))

        # Retain actual finding citations in semantic order. Once the bounded
        # envelope is full, later roles are omitted without displacing the bug
        # anchor or cause/contract evidence. Structured evidence requests only
        # its declared source representation; legacy/related locations retain
        # the compatible best-effort search across available source types.
        for requested in requested_locations:
            if requested.context_manifest_id:
                if manifest is not None:
                    _append_matching_manifest_context(
                        context,
                        manifest,
                        requested=requested,
                        budget=budget,
                    )
                continue
            if _is_diff_retrieval_source(requested.retrieval_source):
                _append_matching_diff_context(
                    context,
                    hunks_by_file,
                    requested=requested,
                    budget=budget,
                )
                continue
            if requested.retrieval_source:
                for entry in tool_evidence:
                    _append_matching_tool_context(
                        context,
                        entry,
                        requested=requested,
                        budget=budget,
                    )
                continue

            _append_matching_diff_context(
                context,
                hunks_by_file,
                requested=requested,
                budget=budget,
            )
            for entry in tool_evidence:
                _append_matching_tool_context(
                    context,
                    entry,
                    requested=requested,
                    budget=budget,
                )
            if manifest is not None:
                _append_matching_manifest_context(
                    context, manifest, requested=requested, budget=budget
                )

        # Only after each cited location has had its declared source retained,
        # add alternate trusted representations for semantic inspection. This
        # keeps a redundant primary read/symbol/manifest copy from consuming
        # the budget before a lower-priority trigger or impact location gets
        # its first evidence record. Deduplication makes this pass additive.
        for requested in requested_locations:
            _append_matching_diff_context(
                context,
                hunks_by_file,
                requested=requested,
                budget=budget,
            )
            for entry in tool_evidence:
                _append_matching_tool_context(
                    context,
                    entry,
                    requested=requested,
                    budget=budget,
                    respect_requested_source=False,
                )
            if manifest is not None:
                _append_matching_manifest_context(
                    context, manifest, requested=requested, budget=budget
                )

        # Preserve the prior manifest envelope when budget remains, but only
        # after every explicitly cited location has had a chance to be retained.
        if manifest is not None:
            for span in manifest.get("included_spans", []):
                if isinstance(span, dict):
                    _append_bounded(context, "included_spans", span, budget)
            for path in manifest.get("included_graph_paths", []):
                if isinstance(path, dict):
                    _append_bounded(context, "included_graph_paths", path, budget)
            for path in manifest.get("excluded_low_confidence_paths", []):
                if isinstance(path, dict):
                    _append_bounded(
                        context, "excluded_low_confidence_paths", path, budget
                    )
        contexts.append(context)
    return contexts


def _candidate_evidence_locations(
    candidate: FindingCandidate,
) -> list[_EvidenceLocationRequest]:
    """Return deduplicated finding locations in fail-closed retention order."""

    issue = candidate.issue
    requested: list[_EvidenceLocationRequest] = []
    seen: set[tuple[str, int, int, str, str]] = set()

    def add(
        path: str,
        start: int | None,
        end: int | None,
        role: str,
        *,
        retrieval_source: str = "",
        context_manifest_id: str = "",
    ) -> None:
        normalized_path = _normalized_path(path)
        if not normalized_path or start is None:
            return
        normalized_end = end or start
        normalized_source = str(retrieval_source or "").strip().lower()
        normalized_manifest = str(context_manifest_id or "").strip()
        key = (
            normalized_path,
            start,
            normalized_end,
            normalized_source,
            normalized_manifest,
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
                retrieval_source=normalized_source,
                context_manifest_id=normalized_manifest,
            )
        )

    location = normalize_location(issue.location)
    if location.valid:
        add(
            location.path or "",
            location.line,
            location.end_line,
            "primary",
            retrieval_source="diff" if issue.is_structured_hypothesis else "",
        )
    if issue.primary_anchor is not None:
        add(
            issue.primary_anchor.file,
            issue.primary_anchor.line,
            issue.primary_anchor.end_line,
            "primary",
            retrieval_source="diff",
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
) -> None:
    """Retain manifest spans and graph paths that cover one cited location."""

    for span in manifest.get("included_spans", []):
        if isinstance(span, dict) and _location_overlaps_record(
            requested.path,
            requested.start_line,
            requested.end_line,
            span,
        ):
            _append_bounded(context, "included_spans", span, budget)
    for path in manifest.get("included_graph_paths", []):
        if not isinstance(path, dict):
            continue
        edges = path.get("edges", [])
        if any(
            isinstance(edge, dict)
            and _location_overlaps_record(
                requested.path,
                requested.start_line,
                requested.end_line,
                edge,
            )
            for edge in edges
        ):
            _append_bounded(context, "included_graph_paths", path, budget)


def _append_matching_diff_context(
    context: dict[str, Any],
    hunks_by_file: dict[str, list[ParsedDiffHunk]],
    *,
    requested: _EvidenceLocationRequest,
    budget: int,
) -> None:
    """Retain every diff hunk explicitly cited by the finding."""

    for hunk in hunks_by_file.get(requested.path, []):
        hunk_end = hunk.new_start + max(0, hunk.new_count - 1)
        if not _ranges_overlap(
            requested.start_line,
            requested.end_line,
            hunk.new_start,
            hunk_end,
        ):
            continue
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


def _append_matching_tool_context(
    context: dict[str, Any],
    entry: dict[str, Any],
    *,
    requested: _EvidenceLocationRequest,
    budget: int,
    respect_requested_source: bool = True,
) -> None:
    tool_name = str(entry.get("tool_name", "")).strip()
    if tool_name not in VERIFIER_CONTEXT_TOOL_NAMES:
        return
    if respect_requested_source and not _tool_source_matches(
        requested.retrieval_source, tool_name
    ):
        return
    arguments = entry.get("arguments")
    data = entry.get("data")
    if not isinstance(arguments, dict) or not isinstance(data, dict):
        return
    data_path = _entry_path(data, arguments)

    if tool_name in {"get_changed_context", "changed_context"}:
        if data_path != requested.path:
            return
        hunk = data.get("hunk")
        hunk_relevant = _payload_matches_range(
            hunk if isinstance(hunk, dict) else {},
            requested.start_line,
            requested.end_line,
        )
        if isinstance(hunk, dict) and hunk_relevant:
            _append_bounded(
                context,
                "diff_hunks",
                {**hunk, "path": data_path, "source": tool_name},
                budget,
            )
        window = data.get("file_window")
        window_relevant = _payload_matches_range(
            window if isinstance(window, dict) else {},
            requested.start_line,
            requested.end_line,
        )
        if isinstance(window, dict) and window_relevant:
            _append_bounded(
                context,
                "file_windows",
                {**window, "path": data_path, "source": tool_name},
                budget,
            )
        _append_symbols(
            context,
            data.get("enclosing_symbols"),
            requested=requested,
            source=tool_name,
            budget=budget,
        )
        return

    if tool_name == "read_file":
        if data_path != requested.path:
            return
        start_line = _as_int(data.get("start_line"))
        line_count = _as_int(data.get("line_count")) or 0
        end_line = start_line + max(0, line_count - 1) if start_line else None
        if not _ranges_overlap(
            requested.start_line, requested.end_line, start_line, end_line
        ):
            return
        _append_bounded(
            context,
            "file_windows",
            {
                "path": data_path,
                "start_line": start_line,
                "end_line": end_line,
                "content": data.get("content", ""),
                "truncated": bool(data.get("truncated", False)),
                "source": tool_name,
            },
            budget,
        )
        return

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
        return
    directly_relevant = any(
        _normalized_path(item.get("path", "")) == requested.path
        for item in matching_records
    )
    if not scoped_to_requested and not directly_relevant:
        return
    _append_bounded(
        context,
        "symbol_contexts",
        {
            "source": tool_name,
            "symbol": data.get("symbol", arguments.get("symbol", "")),
            "backend": data.get("backend", ""),
            "language": data.get("language", ""),
            "definitions": _clip_records(data.get("definitions")),
            "references": _clip_records(data.get("references")),
            "enclosing_symbols": _clip_records(data.get("enclosing_symbols")),
            "truncated": bool(data.get("truncated", False)),
            "warnings": data.get("warnings", []),
        },
        budget,
    )
    _append_symbols(
        context,
        data.get("enclosing_symbols"),
        requested=requested,
        source=tool_name,
        budget=budget,
    )


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


def _append_symbols(
    context: dict[str, Any],
    raw_symbols: Any,
    *,
    requested: _EvidenceLocationRequest,
    source: str,
    budget: int,
) -> None:
    if not isinstance(raw_symbols, list):
        return
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
        _append_bounded(
            context,
            "enclosing_symbols",
            {**raw_symbol, "path": path, "source": source},
            budget,
        )


def _append_bounded(
    context: dict[str, Any],
    key: str,
    value: dict[str, Any],
    budget: int,
) -> None:
    clipped = _clip_payload(value)
    bucket = context[key]
    if not isinstance(bucket, list):
        return
    fingerprint = json.dumps(clipped, ensure_ascii=True, sort_keys=True, default=str)
    if any(
        json.dumps(item, ensure_ascii=True, sort_keys=True, default=str) == fingerprint
        for item in bucket
    ):
        return
    bucket.append(clipped)
    serialized = json.dumps(context, ensure_ascii=True, default=str)
    if len(serialized) > budget:
        bucket.pop()


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
            if _location_overlaps_record(
                location.path, location.line, location_end, span
            ):
                return True
    for key in ("diff_hunks", "file_windows", "enclosing_symbols"):
        records = context.get(key)
        if not isinstance(records, list):
            continue
        for record in records:
            if _location_overlaps_record(
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
                if _location_overlaps_record(
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
    manifest_id = str(context.get("context_manifest_id", ""))
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
        if not manifest_id or evidence_manifest != manifest_id:
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
    return _location_in_tool_context(context, file, line, end_line or line)


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
            if str(span.get("context_hash", "")) != digest:
                continue
            if _location_overlaps_record(file, line, end_line, span):
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
        for edge in path.get("edges", []):
            if not isinstance(edge, dict):
                continue
            if (
                str(edge.get("kind", "")) == edge_kind
                and str(edge.get("resolver", "")) == resolver
                and float(edge.get("confidence", 0.0) or 0.0) >= min_edge_confidence
                and edge.get("evidence_eligibility") == "strong"
            ):
                return True
    return False


def _location_in_records(records: Any, file: str, line: int, end_line: int) -> bool:
    return isinstance(records, list) and any(
        _location_overlaps_record(file, line, end_line, record) for record in records
    )


def _location_in_tool_context(
    context: dict[str, Any], file: str, line: int, end_line: int
) -> bool:
    for key in ("file_windows", "enclosing_symbols"):
        records = context.get(key)
        if isinstance(records, list) and any(
            isinstance(record, dict)
            and str(record.get("source", "")) in VERIFIER_CONTEXT_TOOL_NAMES
            and _location_overlaps_record(file, line, end_line, record)
            for record in records
        ):
            return True
    symbol_contexts = context.get("symbol_contexts")
    if not isinstance(symbol_contexts, list):
        return False
    for symbol_context in symbol_contexts:
        if not isinstance(symbol_context, dict):
            continue
        if str(symbol_context.get("source", "")) not in VERIFIER_CONTEXT_TOOL_NAMES:
            continue
        for key in ("definitions", "references", "enclosing_symbols"):
            if _location_in_records(symbol_context.get(key), file, line, end_line):
                return True
    return False


def _select_context_manifest(
    candidate: FindingCandidate,
    manifests: list[dict[str, Any]],
    path: str,
    start: int | None,
    end: int | None,
) -> dict[str, Any] | None:
    requested = candidate.issue.context_manifest_id
    if requested:
        for manifest in manifests:
            if str(manifest.get("candidate_id", "")) == requested:
                return manifest
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
            return manifest
    return None


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


def _clip_payload(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(item_key): _clip_payload(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_clip_payload(item, key=key) for item in value[:20]]
    if isinstance(value, str):
        limit = 3_500 if key in {"content", "text"} else 1_500
        if len(value) <= limit:
            return value
        return value[:limit] + f"...(truncated {len(value) - limit} chars)"
    return value


def _clip_records(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_clip_payload(item) for item in value[:10] if isinstance(item, dict)]


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
