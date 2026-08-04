"""Static contracts for reverse-engineered formal Graph A/B fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from eval.runner import _changed_new_lines_by_file
from eval.schemas import Fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATHS = (
    ROOT / "eval/fixtures/golden_vybestack_llxprt-code_pr3012_reverse.json",
    ROOT / "eval/fixtures/golden_deepset-ai_haystack_pr12208_reverse.json",
)


@pytest.fixture(params=FIXTURE_PATHS)
def reverse_fixture(request: pytest.FixtureRequest) -> Fixture:
    return Fixture.model_validate_json(request.param.read_text(encoding="utf-8"))


def test_reverse_fixture_schema_and_review_state(reverse_fixture: Fixture) -> None:
    workspace = reverse_fixture.input.workspace

    assert reverse_fixture.metadata.suite == "golden"
    assert reverse_fixture.metadata.annotated_by == "agent_draft"
    assert reverse_fixture.metadata.reviewed is False
    assert not {"held-out", "held_out"}.intersection(reverse_fixture.metadata.tags)
    assert reverse_fixture.input.diff_text.strip()
    assert reverse_fixture.input.files == {}
    assert workspace is not None
    assert workspace.apply_fixture_diff is True
    assert workspace.checkout_sha == workspace.head_sha
    for sha in (
        workspace.base_sha,
        workspace.head_sha,
        workspace.checkout_sha,
        reverse_fixture.source.merge_commit_sha,
    ):
        assert re.fullmatch(r"[0-9a-f]{40}", sha)


def test_reverse_fixture_has_one_root_cause_on_changed_line(
    reverse_fixture: Fixture,
) -> None:
    changed_lines = _changed_new_lines_by_file(reverse_fixture.input.diff_text)

    assert len(reverse_fixture.expected.issues) == 1
    issue = reverse_fixture.expected.issues[0]
    assert issue.line is not None
    assert any(
        line in changed_lines[issue.path]
        for line in range(issue.line, (issue.end_line or issue.line) + 1)
    )
    assert issue.root_cause_id
    assert issue.repair_unit
    assert issue.mechanism_pattern
    assert issue.invariant_pattern
    assert issue.affected_paths
    assert set(issue.affected_paths) <= set(changed_lines)


def test_reverse_fixture_regression_tests_are_not_reversed() -> None:
    llxprt = Fixture.model_validate_json(FIXTURE_PATHS[0].read_text(encoding="utf-8"))
    haystack = Fixture.model_validate_json(FIXTURE_PATHS[1].read_text(encoding="utf-8"))

    assert "403, 429" in llxprt.input.diff_text
    assert "test.ts" not in llxprt.input.diff_text
    assert 'source.meta[\\"file_path\\"]' not in haystack.input.diff_text
    assert 'source.meta["file_path"]' in haystack.input.diff_text
    assert "test/components/converters/test_json.py" not in haystack.input.diff_text


def test_manifest_indexes_both_pending_reverse_fixtures_once() -> None:
    manifest = json.loads(
        (ROOT / "eval/fixtures/manifest.json").read_text(encoding="utf-8")
    )
    entries = {entry["fixture_id"]: entry for entry in manifest["entries"]}

    for path in FIXTURE_PATHS:
        fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
        assert entries[fixture.id]["path"] == path.relative_to(ROOT).as_posix()
        assert entries[fixture.id]["reviewed"] is False
