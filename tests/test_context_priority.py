"""Tests for context priority tiers, diff splitting, truncation, and tie-break rules."""

from __future__ import annotations

import json

from src.analyzer.context_builder import ContextBuilder, ContextPart
from src.analyzer.context_priority import (
    TIER_DIFF,
    TIER_FILES,
    TIER_META,
    assemble_debug_payload,
    assemble_review_payload,
    build_debug_context_parts,
    build_review_context_parts,
    split_diff_hunks,
)
from src.analyzer.context_state import ContextState
from src.analyzer.prompts import build_debug_messages, build_review_messages
from src.analyzer.schemas import DebugRequest, ReviewRequest


def _user_payload(content: str) -> dict[str, object]:
    return json.loads(content.split("\n", 1)[1])


def test_split_diff_hunks_single_file_two_hunks() -> None:
    diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,2 +1,3 @@
 a
+b
 c
@@ -10,2 +11,3 @@
 d
+e
 f
"""
    parts = split_diff_hunks(diff)
    assert len(parts) == 2
    assert "@@ -1,2 +1,3 @@" in parts[0]
    assert "@@ -10,2 +11,3 @@" in parts[1]


def test_split_diff_hunks_single_file_preserves_header_for_later_hunks() -> None:
    diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,2 +1,3 @@
 a
+b
 c
@@ -10,2 +11,3 @@
 d
+e
 f
"""
    parts = split_diff_hunks(diff)
    assert len(parts) == 2
    assert "diff --git a/x.py b/x.py" in parts[1]
    assert "--- a/x.py" in parts[1]
    assert "+++ b/x.py" in parts[1]


def test_split_diff_hunks_multi_file() -> None:
    diff = """diff --git a/a.txt b/a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-olda
+newa
diff --git a/b.txt b/b.txt
--- a/b.txt
+++ b/b.txt
@@ -1 +1 @@
-oldb
+newb
"""
    parts = split_diff_hunks(diff)
    assert len(parts) == 2
    assert "a/a.txt" in parts[0] or "a.txt" in parts[0]
    assert "b/b.txt" in parts[1] or "b.txt" in parts[1]


def test_truncate_respects_tier_order_meta_before_files() -> None:
    cb = ContextBuilder()
    parts = [
        ContextPart(priority=TIER_FILES + 0, label="file:a", content="x" * 100),
        ContextPart(priority=TIER_META, label="meta", content="{}"),
    ]
    selected = cb.truncate_context(parts, budget=10_000)
    assert [p.label for p in selected] == ["meta", "file:a"]


def test_truncate_skips_oversized_middle_keeps_lower_priority_if_fits() -> None:
    """Greedy: large tier-30 block may be skipped; tier-40 may still fit after."""
    cb = ContextBuilder()
    huge = "y" * 5000
    small = "z" * 10
    parts = [
        ContextPart(priority=TIER_META, label="meta", content="{}"),
        ContextPart(priority=TIER_DIFF + 0, label="diff_hunk_0", content=huge),
        ContextPart(priority=TIER_FILES + 0, label="file:small", content=small),
    ]
    budget = cb.estimate_tokens("{}") + cb.estimate_tokens(small) + 50
    selected = cb.truncate_context(parts, budget=budget)
    labels = [p.label for p in selected]
    assert "meta" in labels
    assert "diff_hunk_0" not in labels
    assert "file:small" in labels


def test_review_file_tie_break_lexicographic() -> None:
    """Files use sorted(path) — b before z."""
    req = ReviewRequest(repo_path=".")
    ctx = ContextState()
    parts = build_review_context_parts(
        req,
        ctx,
        diff_loaded="",
        file_contents={"z.txt": "Z", "b.txt": "B"},
    )
    file_parts = [p for p in parts if p.label.startswith("file:")]
    assert [p.label for p in file_parts] == ["file:b.txt", "file:z.txt"]
    assert file_parts[0].priority < file_parts[1].priority


def test_assemble_review_truncated_flags() -> None:
    req = ReviewRequest(repo_path="/r")
    ctx = ContextState()
    all_parts = build_review_context_parts(
        req,
        ctx,
        diff_loaded="diff --git a/x b/x\n@@\n+a\n",
        file_contents={"/a": "A"},
    )
    selected = [p for p in all_parts if p.label == "meta"]
    payload = assemble_review_payload(req, ctx, all_parts, selected)
    assert payload["truncated"]["any"] is True
    assert payload["truncated"]["diff_hunks"] is True
    assert "/a" in payload["truncated"]["files"]


def test_debug_error_log_before_files_in_priority() -> None:
    req = DebugRequest(repo_path=".")
    ctx = ContextState()
    parts = build_debug_context_parts(
        req,
        ctx,
        error_log_loaded="ERR",
        file_contents={"a.py": "x"},
    )
    priorities = {p.label: p.priority for p in parts}
    assert priorities["error_log"] < priorities["file:a.py"]


def test_assemble_debug_payload_error_dropped() -> None:
    req = DebugRequest(repo_path=".")
    ctx = ContextState()
    all_parts = build_debug_context_parts(
        req,
        ctx,
        error_log_loaded="long error",
        file_contents={},
    )
    selected = [p for p in all_parts if p.label == "meta"]
    payload = assemble_debug_payload(req, ctx, all_parts, selected)
    assert payload["error_log_loaded"] == ""
    assert payload["truncated"]["error_log"] is True


def test_review_messages_do_not_embed_full_direct_diff_text_when_truncated() -> None:
    direct_diff = "diff --git a/x.py b/x.py\n" + ("x" * 20_000)
    req = ReviewRequest(repo_path=".", diff_text=direct_diff)
    ctx = ContextState()
    messages = build_review_messages(
        req,
        ctx,
        direct_diff,
        {},
        prompt_token_budget=50,
    )
    payload = _user_payload(messages[1].content)
    assert direct_diff not in messages[1].content
    assert payload["diff_text"] is None
    assert payload["truncated"]["any"] is True


def test_debug_messages_do_not_embed_full_direct_error_log_text_when_truncated() -> None:
    direct_error_log = "E" * 20_000
    req = DebugRequest(repo_path=".", error_log_text=direct_error_log)
    ctx = ContextState()
    messages = build_debug_messages(
        req,
        ctx,
        direct_error_log,
        {},
        prompt_token_budget=50,
    )
    payload = _user_payload(messages[1].content)
    assert direct_error_log not in messages[1].content
    assert payload["error_log_text"] is None
    assert payload["truncated"]["any"] is True


def test_review_payload_json_roundtrip() -> None:
    req = ReviewRequest(repo_path=".")
    ctx = ContextState(constraints=["c"])
    all_parts = build_review_context_parts(req, ctx, diff_loaded="", file_contents={})
    payload = assemble_review_payload(req, ctx, all_parts, all_parts)
    json.dumps(payload)
