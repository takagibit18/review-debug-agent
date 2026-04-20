"""Tests for location normalization and contract validation."""

from __future__ import annotations

from src.analyzer.location import normalize_location


def test_normalize_location_accepts_canonical() -> None:
    parsed = normalize_location("src/app.py:10-12")
    assert parsed.valid is True
    assert parsed.canonical == "src/app.py:10-12"
    assert parsed.warning == ""


def test_normalize_location_converts_backslashes_and_text_wrapper() -> None:
    parsed = normalize_location("see file src\\core\\main.py:9 please")
    assert parsed.valid is True
    assert parsed.canonical == "src/core/main.py:9"
    assert parsed.warning in {"normalized_location"}


def test_normalize_location_rejects_invalid_ranges() -> None:
    parsed = normalize_location("src/app.py:12-2")
    assert parsed.valid is False
    assert parsed.warning == "invalid_line_range"
