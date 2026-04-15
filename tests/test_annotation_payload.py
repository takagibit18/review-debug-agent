"""Tests for eval crawler annotation payload size limits."""

from eval.crawler.annotator import build_annotation_user_json


def test_build_annotation_user_json_fits_large_diff() -> None:
    huge_diff = "x\n" * 200_000
    s = build_annotation_user_json(
        repo_full_name="o/r",
        pr_number=1,
        pr_title="t",
        pr_body="",
        diff_text=huge_diff,
        instructions="go",
        max_json_chars=12_000,
    )
    assert len(s) <= 12_000
    assert "truncated" in s.lower() or "exceeded" in s.lower()


def test_build_annotation_user_json_preserves_small_payload() -> None:
    s = build_annotation_user_json(
        repo_full_name="a/b",
        pr_number=2,
        pr_title="small",
        pr_body="body",
        diff_text="+1\n-1\n",
        instructions="x",
        max_json_chars=90_000,
    )
    assert "small" in s
    assert "+1" in s
