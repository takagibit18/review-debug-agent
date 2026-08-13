"""Minimal in-memory working state for durable draft findings."""

from __future__ import annotations

from uuid import uuid4

from src.models.schemas import DraftFinding, DraftFindingInput


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
