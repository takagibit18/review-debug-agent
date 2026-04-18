"""Tests for eval runner utilities."""

from __future__ import annotations

from pathlib import Path

from eval.runner import (
    _persist_event_log_to_outputs,
    _resolve_event_log_path,
    _sanitize_fixture_id_for_filename,
)


def test_resolve_event_log_path_returns_existing_file(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EVENT_LOG_DIR", ".cr-debug-agent/logs")
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    log_dir = repo_root / ".cr-debug-agent" / "logs"
    log_dir.mkdir(parents=True)
    log_path = log_dir / "run-1.jsonl"
    log_path.write_text('{"event_type":"model_call"}\n', encoding="utf-8")

    resolved = _resolve_event_log_path(repo_root, "run-1")
    assert resolved == str(log_path)


def test_resolve_event_log_path_returns_none_when_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EVENT_LOG_DIR", ".cr-debug-agent/logs")
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    resolved = _resolve_event_log_path(repo_root, "missing")
    assert resolved is None


def test_sanitize_fixture_id_for_filename() -> None:
    assert _sanitize_fixture_id_for_filename("a/b\\c") == "a_b_c"


def test_persist_event_log_to_outputs_copies_and_returns_absolute(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "run-1.jsonl"
    src.write_text('{"event_type":"model_call"}\n', encoding="utf-8")

    out = _persist_event_log_to_outputs(src, "golden_fixture", "run-1")
    assert out is not None
    dest = tmp_path / "eval" / "outputs" / "event_logs" / "golden_fixture_run-1.jsonl"
    assert Path(out) == dest.resolve()
    assert dest.read_text(encoding="utf-8") == '{"event_type":"model_call"}\n'


def test_persist_event_log_to_outputs_returns_none_when_missing_src() -> None:
    assert _persist_event_log_to_outputs(None, "f", "r") is None


def test_persist_event_log_to_outputs_returns_none_when_file_missing(
    tmp_path: Path,
) -> None:
    assert _persist_event_log_to_outputs(tmp_path / "nope.jsonl", "f", "r") is None
