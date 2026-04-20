"""Tests for context builder diff/project/file-content helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.analyzer.context_builder import ContextBuilder


def test_load_diff_uses_head_comparison(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="diff --git ...")

    monkeypatch.setattr("src.analyzer.context_builder.subprocess.run", _fake_run)

    text = ContextBuilder().load_diff(".")
    assert text.startswith("diff --git")
    assert captured["cmd"] == ["git", "-C", ".", "diff", "HEAD"]


def test_build_project_structure_respects_depth_and_entry_limit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "x.py").write_text("print(1)\n", encoding="utf-8")
    (root / "a" / "b" / "y.py").write_text("print(2)\n", encoding="utf-8")

    output = ContextBuilder().build_project_structure(
        str(root), max_depth=2, max_entries=4
    )
    assert "repo/" in output
    assert "- a/" in output
    assert "truncated" in output or "- a/x.py" in output


def test_load_diff_file_contents_prioritizes_diff_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "module.py").write_text("x = 1\n" * 100, encoding="utf-8")
    (root / "tests" / "test_module.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    diff = (
        "diff --git a/src/module.py b/src/module.py\n"
        "--- a/src/module.py\n+++ b/src/module.py\n"
        "@@ -1,1 +1,1 @@\n-x = 0\n+x = 1\n"
    )
    loaded = ContextBuilder().load_diff_file_contents(
        str(root),
        diff_text=diff,
        max_files=2,
        max_chars_per_file=20,
        max_chars_total=25,
    )
    assert "src/module.py" in loaded
    assert len(loaded["src/module.py"]) <= 20
