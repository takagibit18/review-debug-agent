"""Minimal in-memory working state for durable draft findings."""

from __future__ import annotations

import re
from uuid import uuid4

from src.models.schemas import DraftFinding, DraftFindingInput

_VISIBLE_DRAFT_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:[-*]\s*)?(?:finding\s*:\s*)?"
    r"`?(?P<file>(?:[a-z0-9_.-]+/)+[a-z0-9_.-]+\.[a-z0-9]+)"
    r"(?::(?P<line>\d+))?`?\s*(?:[-:\u2013\u2014]\s+)"
    r"(?P<claim>[^\r\n]{20,600})$"
)
_VISIBLE_CLAIM_SIGNAL = re.compile(
    r"\b(?:bug|regression|incorrect|wrong|fail(?:s|ure)?|break(?:s|ing)?|"
    r"may|can|does\s+not|instead|data\s+loss|exception|truncat(?:e|es|ed|ion)|"
    r"compar(?:e|es|ed|ison))\b",
    re.IGNORECASE,
)
_VISIBLE_REPOSITORY_PATH = re.compile(
    r"`?(?P<file>(?:[a-z0-9_.-]+/)*[a-z0-9_.-]+\.(?:py|pyi|js|jsx|ts|tsx|"
    r"java|go|rs|rb|php|cs|cpp|cc|c|h|hpp|swift|kt|kts|scala|sh|ps1|sql))"
    r"(?::(?P<line>\d+))?`?",
    re.IGNORECASE,
)
_VISIBLE_SYMBOL_CLAIM = re.compile(
    r"(?is)(?P<symbol>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+)\s+"
    r"(?P<claim>(?:compares?|routes?|drops?|writes?|returns?|raises?|fails?|"
    r"breaks?|changes?|may\b|can\b|does\s+not\b)[^.\r\n]{20,600}[.])"
)


class DraftFindingStore:
    """Keep runtime-bound draft hypotheses for the current process and run."""

    def __init__(self) -> None:
        self._items: dict[str, DraftFinding] = {}

    @staticmethod
    def bind(
        draft_input: DraftFindingInput,
        *,
        source_response_id: str,
    ) -> DraftFinding:
        """Bind trusted provenance without mutating in-memory state."""

        return DraftFinding(
            id=f"df_{uuid4().hex[:16]}",
            source_response_id=source_response_id,
            file=draft_input.file,
            line=draft_input.line,
            symbol=draft_input.symbol,
            claim=draft_input.claim,
        )

    def add(self, draft: DraftFinding) -> None:
        """Add a previously runtime-bound draft, replacing only the same id."""

        self._items[draft.id] = draft

    def all(self) -> list[DraftFinding]:
        """Return drafts in creation order."""

        return list(self._items.values())

    def get(self, draft_id: str) -> DraftFinding | None:
        """Return one draft by id."""

        return self._items.get(draft_id)

    def __len__(self) -> int:
        return len(self._items)


def extract_visible_draft_finding(content: str) -> DraftFindingInput | None:
    """Conservatively extract one minimal draft from visible truncated content."""

    if not content.strip():
        return None
    for match in _VISIBLE_DRAFT_PATTERN.finditer(content):
        claim = " ".join(match.group("claim").split()).strip()
        if not _VISIBLE_CLAIM_SIGNAL.search(claim):
            continue
        raw_line = match.group("line")
        return DraftFindingInput(
            file=match.group("file"),
            line=int(raw_line) if raw_line else None,
            claim=claim,
        )
    for match in _VISIBLE_SYMBOL_CLAIM.finditer(content):
        preceding_paths = list(
            _VISIBLE_REPOSITORY_PATH.finditer(content, 0, match.start())
        )
        if not preceding_paths:
            continue
        path_match = preceding_paths[-1]
        if match.start() - path_match.end() > 800:
            continue
        claim = " ".join(match.group("claim").split()).strip()
        if not _VISIBLE_CLAIM_SIGNAL.search(claim):
            continue
        raw_line = path_match.group("line")
        return DraftFindingInput(
            file=path_match.group("file"),
            line=int(raw_line) if raw_line else None,
            symbol=match.group("symbol"),
            claim=claim,
        )
    return None
