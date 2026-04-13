"""Session event logging with memory cache plus JSONL persistence."""

from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """Canonical run event types used by orchestrator and analyzers."""

    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    DECISION = "decision"
    ERROR = "error"
    PHASE_START = "phase_start"
    PHASE_END = "phase_end"


class EventEntry(BaseModel):
    """One event record in a run timeline."""

    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    run_id: str
    event_type: EventType
    phase: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class EventLog:
    """Write events to in-memory cache and on-disk JSONL."""

    def __init__(self, run_id: str, log_dir: Path, cache_size: int = 50) -> None:
        self._run_id = run_id
        self._cache: deque[EventEntry] = deque(maxlen=max(1, cache_size))
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._log_dir / f"{run_id}.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def record(self, event: EventEntry) -> None:
        self._cache.append(event)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")

    def recent(self, n: int) -> list[EventEntry]:
        if n <= 0:
            return []
        return list(self._cache)[-n:]

    def replay(self) -> list[EventEntry]:
        if not self._path.exists():
            return []
        events: list[EventEntry] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                raw = line.strip()
                if not raw:
                    continue
                events.append(EventEntry.model_validate(json.loads(raw)))
        return events

    def close(self) -> None:
        # EventLog writes eagerly per record, nothing to flush.
        return None
