"""Durable append-only checkpoint journal for paired Graph A/B runs."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

CheckpointStatus = Literal["priming", "measured", "invalid", "workspace_failure"]


class StableRunKey(BaseModel):
    """Inputs that must match before a measured result can be reused."""

    experiment_id: str = Field(min_length=1)
    fixture_id: str = Field(min_length=1)
    sample_index: int = Field(ge=1)
    variant_id: str = Field(min_length=1)
    repository_snapshot: str = Field(min_length=1)
    experiment_contract_hash: str = Field(min_length=64, max_length=64)

    def identity(self) -> tuple[str, str, int, str, str, str]:
        return (
            self.experiment_id,
            self.fixture_id,
            self.sample_index,
            self.variant_id,
            self.repository_snapshot,
            self.experiment_contract_hash,
        )


class CheckpointRecord(StableRunKey):
    """One immutable checkpoint event."""

    schema_version: Literal[1] = 1
    status: CheckpointStatus
    attempt: int = Field(ge=1)
    valid: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    run_record: dict[str, Any] | None = None


class CheckpointJournal:
    """Strict reader and durable writer for checkpoint JSONL."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.records = self._load()

    def _load(self) -> list[CheckpointRecord]:
        if not self.path.exists():
            return []
        records: list[CheckpointRecord] = []
        exact: set[tuple[tuple[str, str, int, str, str, str], str, int]] = set()
        valid_measured: set[tuple[str, str, int, str, str, str]] = set()
        for line_number, raw_line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not raw_line.strip():
                raise RuntimeError(
                    f"Corrupt checkpoint {self.path}: blank line {line_number}"
                )
            try:
                record = CheckpointRecord.model_validate_json(raw_line)
            except (ValidationError, ValueError) as exc:
                raise RuntimeError(
                    f"Corrupt checkpoint {self.path} at line {line_number}: {exc}"
                ) from exc
            stable = record.identity()
            exact_key = (stable, record.status, record.attempt)
            if exact_key in exact:
                raise RuntimeError(
                    "Duplicate checkpoint record at line "
                    f"{line_number}: {stable}/{record.status}/attempt-{record.attempt}"
                )
            exact.add(exact_key)
            if record.status == "measured" and record.valid:
                if stable in valid_measured:
                    raise RuntimeError(
                        f"Duplicate valid measured checkpoint at line {line_number}: {stable}"
                    )
                valid_measured.add(stable)
            records.append(record)
        return records

    def reset(self) -> None:
        """Atomically start a new journal after an explicit no-resume request."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.rewrite.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        self.records = []

    def matching(self, key: StableRunKey) -> list[CheckpointRecord]:
        identity = key.identity()
        return [record for record in self.records if record.identity() == identity]

    def completed(self, key: StableRunKey) -> CheckpointRecord | None:
        return next(
            (
                record
                for record in reversed(self.matching(key))
                if record.status == "measured" and record.valid
            ),
            None,
        )

    def latest_failure(self, key: StableRunKey) -> CheckpointRecord | None:
        return next(
            (
                record
                for record in reversed(self.matching(key))
                if record.status in {"invalid", "workspace_failure"}
            ),
            None,
        )

    def next_attempt(self, key: StableRunKey, status: CheckpointStatus) -> int:
        attempts = [
            record.attempt for record in self.matching(key) if record.status == status
        ]
        return max(attempts, default=0) + 1

    def append(
        self,
        *,
        key: StableRunKey,
        status: CheckpointStatus,
        valid: bool,
        run_record: dict[str, Any] | None,
    ) -> CheckpointRecord:
        if status == "measured" and not valid:
            raise ValueError("measured checkpoint records must be valid")
        if status != "measured" and valid:
            raise ValueError(f"{status} checkpoint records cannot be valid")
        if status != "priming" and run_record is None:
            raise ValueError(f"{status} checkpoint record requires run_record")
        with self._lock:
            if status == "measured" and self.completed(key) is not None:
                raise RuntimeError(
                    f"Duplicate valid measured checkpoint: {key.identity()}"
                )
            record = CheckpointRecord(
                **key.model_dump(),
                status=status,
                attempt=self.next_attempt(key, status),
                valid=valid,
                run_record=run_record,
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = record.model_dump_json()
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(payload + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.records.append(record)
            return record
