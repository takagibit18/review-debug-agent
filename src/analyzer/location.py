"""Location contract helpers: parse and normalize path[:line[-end_line]]."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

_CANONICAL_PATTERN = re.compile(
    r"^(?P<path>[^\s:]+)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?$"
)
_PATH_LINE_PATTERN = re.compile(r"(?P<path>[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+):(?P<line>\d+)")


@dataclass(frozen=True)
class LocationParseResult:
    raw: str
    canonical: str
    path: str | None
    line: int | None
    end_line: int | None
    valid: bool
    warning: str = ""


def normalize_location(raw: str) -> LocationParseResult:
    value = (raw or "").strip()
    if not value:
        return LocationParseResult(
            raw=raw,
            canonical=value,
            path=None,
            line=None,
            end_line=None,
            valid=False,
            warning="empty_location",
        )

    parsed = _parse_canonical(value)
    if parsed is None:
        parsed = _parse_fuzzy(value)
    if parsed is None:
        return LocationParseResult(
            raw=raw,
            canonical=value,
            path=None,
            line=None,
            end_line=None,
            valid=False,
            warning="unparseable_location",
        )

    path, line, end_line = parsed
    if not _is_valid_path(path):
        return LocationParseResult(
            raw=raw,
            canonical=value,
            path=None,
            line=None,
            end_line=None,
            valid=False,
            warning="invalid_path",
        )
    if line is not None and line <= 0:
        return LocationParseResult(
            raw=raw,
            canonical=value,
            path=None,
            line=None,
            end_line=None,
            valid=False,
            warning="invalid_line_range",
        )
    if line is not None and end_line is not None and end_line < line:
        return LocationParseResult(
            raw=raw,
            canonical=value,
            path=None,
            line=None,
            end_line=None,
            valid=False,
            warning="invalid_line_range",
        )

    canonical = path
    if line is not None:
        canonical = f"{path}:{line}" if end_line is None else f"{path}:{line}-{end_line}"
    warning = "" if canonical == value else "normalized_location"
    return LocationParseResult(
        raw=raw,
        canonical=canonical,
        path=path,
        line=line,
        end_line=end_line,
        valid=True,
        warning=warning,
    )


def _parse_canonical(value: str) -> tuple[str, int | None, int | None] | None:
    match = _CANONICAL_PATTERN.match(value)
    if match is None:
        return None
    path = _normalize_path(match.group("path"))
    start = match.group("start")
    end = match.group("end")
    line = int(start) if start else None
    end_line = int(end) if end else None
    return path, line, end_line


def _parse_fuzzy(value: str) -> tuple[str, int | None, int | None] | None:
    match = _PATH_LINE_PATTERN.search(value)
    if match is None:
        return None
    path = _normalize_path(match.group("path"))
    return path, int(match.group("line")), None


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    normalized = re.sub(r"/+", "/", normalized)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return str(PurePosixPath(normalized))


def _is_valid_path(path: str) -> bool:
    if not path or path.startswith("/") or path.startswith("../"):
        return False
    parts = PurePosixPath(path).parts
    return all(part not in {"..", ""} for part in parts)
