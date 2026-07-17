"""Run-scoped tool evidence captured and compacted for finding verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.analyzer.diff_lines import parse_unified_diff_hunks
from src.analyzer.location import normalize_location
from src.analyzer.schemas import FindingCandidate, ReviewRequest

VERIFIER_CONTEXT_TOOL_NAMES = {
    "read_file",
    "get_changed_context",
    "changed_context",
    "find_symbol_context",
    "symbol_context",
}


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
) -> list[dict[str, Any]]:
    """Build bounded hunk/window/symbol evidence associated with each candidate."""
    if not candidates:
        return []
    hunks_by_file = parse_unified_diff_hunks(request.diff_text or "")
    budget = max(800, max_chars // len(candidates))
    contexts: list[dict[str, Any]] = []
    for candidate in candidates:
        location = normalize_location(candidate.issue.location)
        context: dict[str, Any] = {
            "candidate_id": candidate.candidate_id,
            "location": location.canonical,
            "diff_hunks": [],
            "file_windows": [],
            "enclosing_symbols": [],
            "symbol_contexts": [],
        }
        if not location.valid or not location.path:
            contexts.append(context)
            continue
        start = location.line
        end = location.end_line or start

        for hunk in hunks_by_file.get(location.path, []):
            if start is not None and not _lines_overlap(
                start,
                end or start,
                hunk.changed_new_lines,
            ):
                continue
            _append_bounded(
                context,
                "diff_hunks",
                {
                    "path": location.path,
                    "header": hunk.header,
                    "new_start": hunk.new_start,
                    "new_count": hunk.new_count,
                    "changed_new_lines": list(hunk.changed_new_lines),
                    "text": "\n".join([hunk.header, *hunk.lines]),
                    "source": "diff",
                },
                budget,
            )

        for entry in tool_evidence:
            _append_matching_tool_context(
                context,
                entry,
                candidate_path=location.path,
                candidate_start=start,
                candidate_end=end,
                budget=budget,
            )
        contexts.append(context)
    return contexts


def _append_matching_tool_context(
    context: dict[str, Any],
    entry: dict[str, Any],
    *,
    candidate_path: str,
    candidate_start: int | None,
    candidate_end: int | None,
    budget: int,
) -> None:
    tool_name = str(entry.get("tool_name", "")).strip()
    if tool_name not in VERIFIER_CONTEXT_TOOL_NAMES:
        return
    arguments = entry.get("arguments")
    data = entry.get("data")
    if not isinstance(arguments, dict) or not isinstance(data, dict):
        return
    data_path = _entry_path(data, arguments)

    if tool_name in {"get_changed_context", "changed_context"}:
        if data_path != candidate_path:
            return
        hunk = data.get("hunk")
        if isinstance(hunk, dict) and _payload_matches_range(
            hunk,
            candidate_start,
            candidate_end,
        ):
            _append_bounded(
                context,
                "diff_hunks",
                {**hunk, "path": data_path, "source": tool_name},
                budget,
            )
        window = data.get("file_window")
        if isinstance(window, dict) and _payload_matches_range(
            window,
            candidate_start,
            candidate_end,
        ):
            _append_bounded(
                context,
                "file_windows",
                {**window, "path": data_path, "source": tool_name},
                budget,
            )
        _append_symbols(
            context,
            data.get("enclosing_symbols"),
            candidate_path=candidate_path,
            candidate_start=candidate_start,
            candidate_end=candidate_end,
            source=tool_name,
            budget=budget,
        )
        return

    if tool_name == "read_file":
        if data_path != candidate_path:
            return
        start_line = _as_int(data.get("start_line"))
        line_count = _as_int(data.get("line_count")) or 0
        end_line = start_line + max(0, line_count - 1) if start_line else None
        if not _ranges_overlap(candidate_start, candidate_end, start_line, end_line):
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
    scoped_to_candidate = _normalized_path(arguments.get("path", "")) == candidate_path
    directly_relevant = any(
        _normalized_path(item.get("path", "")) == candidate_path for item in records
    )
    if not scoped_to_candidate and not directly_relevant:
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
        candidate_path=candidate_path,
        candidate_start=candidate_start,
        candidate_end=candidate_end,
        source=tool_name,
        budget=budget,
    )


def _append_symbols(
    context: dict[str, Any],
    raw_symbols: Any,
    *,
    candidate_path: str,
    candidate_start: int | None,
    candidate_end: int | None,
    source: str,
    budget: int,
) -> None:
    if not isinstance(raw_symbols, list):
        return
    for raw_symbol in raw_symbols:
        if not isinstance(raw_symbol, dict):
            continue
        path = _normalized_path(raw_symbol.get("path", candidate_path))
        if path != candidate_path:
            continue
        symbol_start = _as_int(raw_symbol.get("line"))
        symbol_end = _as_int(raw_symbol.get("end_line")) or symbol_start
        if candidate_start is not None and not _ranges_overlap(
            candidate_start,
            candidate_end,
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


def _location_overlaps_record(
    path: str,
    start: int,
    end: int,
    record: Any,
) -> bool:
    if not isinstance(record, dict):
        return False
    record_path = _normalized_path(record.get("path", ""))
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
