"""Append-only durable facts for one agent run.

The run journal is intentionally separate from the analyzer event log. Event logs
serve observability, while this module preserves the minimum structured facts that
later recovery logic needs.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.models.schemas import DraftFinding, TokenUsage

RUN_JOURNAL_SCHEMA_VERSION: Literal["1.0"] = "1.0"
RunJournalEntryType = Literal["model_response", "tool_result", "draft_finding"]

_SENSITIVE_KEYWORDS = (
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "cookie",
    "session",
    "credential",
)


class RunJournalError(RuntimeError):
    """Base error for journal persistence or replay failures."""


class RunJournalCorruptionError(RunJournalError):
    """Raised when corruption is found before the crash-tolerant final line."""


class ModelResponseJournalPayload(BaseModel):
    """Visible provider response persisted before parsing or validation."""

    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(..., ge=0, description="Agent loop iteration")
    model: str = Field(default="", description="Provider model identifier")
    finish_reason: str = Field(default="", description="Provider finish reason")
    content: str = Field(default="", description="Visible assistant content")
    tool_calls: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Provider tool calls exactly as returned",
    )
    usage: TokenUsage = Field(
        default_factory=TokenUsage,
        description="Provider token accounting",
    )


class ToolResultJournalPayload(BaseModel):
    """Structured result of one tool call bound to its model response."""

    model_config = ConfigDict(extra="forbid")

    source_response_id: str = Field(
        ...,
        min_length=1,
        description="Journal id of the model response that requested this tool",
    )
    tool_call_id: str = Field(
        ...,
        min_length=1,
        description="Provider or runtime-bound tool call identifier",
    )
    tool: str = Field(..., min_length=1, description="Tool name")
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured and key-redacted tool arguments",
    )
    result: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured ToolResult envelope",
    )


class PendingRunJournalEntry(BaseModel):
    """Validated fact supplied to :meth:`RunJournal.append`."""

    model_config = ConfigDict(extra="forbid")

    type: RunJournalEntryType
    payload: dict[str, Any] = Field(default_factory=dict)


class RunJournalEntry(BaseModel):
    """Versioned, sequenced record persisted as one JSONL line."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = RUN_JOURNAL_SCHEMA_VERSION
    id: str = Field(default_factory=lambda: f"rje_{uuid4().hex}")
    seq: int = Field(..., ge=1)
    type: RunJournalEntryType
    run_id: str = Field(..., min_length=1)
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload: dict[str, Any] = Field(default_factory=dict)


class RunJournal:
    """Append-only JSONL journal with crash-tolerant replay.

    A malformed final non-empty line is treated as an interrupted append and ignored.
    Malformed data anywhere earlier is corruption and raises. Every append writes,
    flushes, and optionally makes an ``fsync`` call before returning.
    """

    def __init__(self, run_id: str, path: Path, *, fsync: bool = True) -> None:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        self._run_id = run_id
        self._path = path
        self._fsync = fsync
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._discard_interrupted_tail()
        entries = self.replay()
        self._seq = entries[-1].seq if entries else 0

    @property
    def path(self) -> Path:
        """Return the journal JSONL path."""

        return self._path

    def append(self, entry: PendingRunJournalEntry) -> RunJournalEntry:
        """Validate and durably append one run fact."""

        payload = self._validated_payload(entry.type, entry.payload)
        with self._lock:
            persisted = RunJournalEntry(
                seq=self._seq + 1,
                type=entry.type,
                run_id=self._run_id,
                payload=payload,
            )
            try:
                with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(persisted.model_dump_json() + "\n")
                    handle.flush()
                    if self._fsync:
                        os.fsync(handle.fileno())
            except OSError as exc:
                raise RunJournalError(
                    f"Failed to append run journal {self._path}: {exc}"
                ) from exc
            self._seq = persisted.seq
            return persisted

    def replay(self) -> list[RunJournalEntry]:
        """Replay valid entries, ignoring only a malformed final non-empty line."""

        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise RunJournalError(
                f"Failed to read run journal {self._path}: {exc}"
            ) from exc

        last_non_empty = max(
            (index for index, line in enumerate(lines) if line.strip()),
            default=-1,
        )
        entries: list[RunJournalEntry] = []
        expected_seq = 1
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                entry = RunJournalEntry.model_validate(raw)
                self._validated_payload(entry.type, entry.payload)
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                if index == last_non_empty:
                    break
                raise RunJournalCorruptionError(
                    f"Invalid journal entry on line {index + 1}: {exc}"
                ) from exc
            if entry.run_id != self._run_id:
                raise RunJournalCorruptionError(
                    f"Journal line {index + 1} has run_id {entry.run_id!r}; "
                    f"expected {self._run_id!r}"
                )
            if entry.seq != expected_seq:
                raise RunJournalCorruptionError(
                    f"Journal line {index + 1} has seq {entry.seq}; "
                    f"expected {expected_seq}"
                )
            entries.append(entry)
            expected_seq += 1
        return entries

    def last_entry(self) -> RunJournalEntry | None:
        """Return the last replayable fact, if any."""

        entries = self.replay()
        return entries[-1] if entries else None

    def _discard_interrupted_tail(self) -> None:
        """Remove an invalid final line before a recovered journal appends again."""

        if not self._path.exists():
            return
        try:
            raw_lines = self._path.read_bytes().splitlines(keepends=True)
        except OSError as exc:
            raise RunJournalError(
                f"Failed to inspect run journal {self._path}: {exc}"
            ) from exc
        last_non_empty = max(
            (index for index, line in enumerate(raw_lines) if line.strip()),
            default=-1,
        )
        if last_non_empty < 0:
            return
        try:
            raw = json.loads(raw_lines[last_non_empty].decode("utf-8"))
            entry = RunJournalEntry.model_validate(raw)
            self._validated_payload(entry.type, entry.payload)
        except (UnicodeError, json.JSONDecodeError, ValidationError, ValueError):
            offset = sum(len(line) for line in raw_lines[:last_non_empty])
            try:
                with self._path.open("r+b") as handle:
                    handle.truncate(offset)
                    handle.flush()
                    if self._fsync:
                        os.fsync(handle.fileno())
            except OSError as exc:
                raise RunJournalError(
                    f"Failed to discard interrupted journal tail {self._path}: {exc}"
                ) from exc

    @staticmethod
    def _validated_payload(
        entry_type: RunJournalEntryType,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        model: ModelResponseJournalPayload | ToolResultJournalPayload | DraftFinding
        if entry_type == "model_response":
            model = ModelResponseJournalPayload.model_validate(payload)
        elif entry_type == "tool_result":
            model = ToolResultJournalPayload.model_validate(payload)
        else:
            model = DraftFinding.model_validate(payload)
        return model.model_dump(mode="json")


def redact_sensitive_values(value: Any, *, key: str = "") -> Any:
    """Recursively redact values whose keys match the existing trace policy."""

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for item_key, item_value in value.items():
            normalized_key = str(item_key).lower()
            if any(keyword in normalized_key for keyword in _SENSITIVE_KEYWORDS):
                redacted[str(item_key)] = "[REDACTED]"
            else:
                redacted[str(item_key)] = redact_sensitive_values(
                    item_value, key=normalized_key
                )
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_values(item, key=key) for item in value]
    if isinstance(value, str) and any(
        keyword in key for keyword in _SENSITIVE_KEYWORDS
    ):
        return "[REDACTED]"
    return value
