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
    ROOT / "eval/fixtures/golden_deepset-ai_haystack_pr12162_reverse.json",
    ROOT / "eval/fixtures/golden_deepset-ai_haystack_pr12257_reverse.json",
)
PREFLIGHT_FIXTURE_PATHS = (
    ROOT / "eval/fixtures/golden_pydantic_pydantic_pr12568.json",
    ROOT / "eval/fixtures/golden_pydantic_pydantic_pr12590.json",
    ROOT / "eval/fixtures/golden_pytest-dev_pytest_pr13969.json",
)
ANNOTATED_FIXTURE_PATHS = (
    ROOT / "eval/fixtures/golden_pydantic_pydantic_pr12117.json",
    *FIXTURE_PATHS,
)
EXPECTED_MERGE_COMMIT_SHAS = {
    "golden_vybestack_llxprt-code_pr3012_reverse": "",
    "golden_deepset-ai_haystack_pr12208_reverse": (
        "14535047214b8d0e1345bdcd800c9522ae445501"
    ),
    "golden_deepset-ai_haystack_pr12162_reverse": (
        "66fb9d0dedeec1848aca56ef7651ec9afcc090a4"
    ),
    "golden_deepset-ai_haystack_pr12257_reverse": (
        "49f8d2d54999cd4b59c0e98694fbb4fd9f9ac1f0"
    ),
}


@pytest.fixture(params=FIXTURE_PATHS)
def reverse_fixture(request: pytest.FixtureRequest) -> Fixture:
    return Fixture.model_validate_json(request.param.read_text(encoding="utf-8"))


def test_reverse_fixture_schema_and_review_state(reverse_fixture: Fixture) -> None:
    workspace = reverse_fixture.input.workspace

    assert reverse_fixture.metadata.suite == "golden"
    assert reverse_fixture.metadata.annotated_by == "manual"
    assert reverse_fixture.metadata.reviewed is True
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
    ):
        assert re.fullmatch(r"[0-9a-f]{40}", sha)
    assert (
        reverse_fixture.source.merge_commit_sha
        == EXPECTED_MERGE_COMMIT_SHAS[reverse_fixture.id]
    )


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


@pytest.mark.parametrize("path", PREFLIGHT_FIXTURE_PATHS + ANNOTATED_FIXTURE_PATHS)
def test_formal_fixture_has_complete_expected_annotations(
    path: Path,
) -> None:
    fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
    workspace = fixture.input.workspace

    assert fixture.metadata.suite == "golden"
    assert fixture.metadata.reviewed is True
    assert workspace is not None
    if workspace.review_scope == "legacy":
        assert workspace.apply_fixture_diff is True
    else:
        assert workspace.checkout_sha == workspace.head_sha
        assert workspace.diff_base_sha == workspace.base_sha
        assert workspace.apply_fixture_diff is False
    assert fixture.input.diff_text.strip()
    if not fixture.expected.issues:
        assert fixture.expected.is_empty_annotation is True
        assert fixture.expected.min_issues == 0
        assert fixture.expected.max_issues == 0
        return

    for issue in fixture.expected.issues:
        assert issue.severity is not None
        assert issue.path
        assert issue.line is not None
        assert issue.category
        assert issue.description
        assert issue.root_cause_id
        assert issue.repair_unit
        assert issue.mechanism_pattern
        assert issue.invariant_pattern
        assert issue.affected_paths
        assert issue.structural_scope is not None
        assert issue.graph_observable is not None


def test_reverse_fixture_regression_tests_are_not_reversed() -> None:
    llxprt = Fixture.model_validate_json(FIXTURE_PATHS[0].read_text(encoding="utf-8"))
    haystack = Fixture.model_validate_json(FIXTURE_PATHS[1].read_text(encoding="utf-8"))

    assert "403, 429" in llxprt.input.diff_text
    assert "test.ts" not in llxprt.input.diff_text
    assert "shouldBypassRetry" not in llxprt.input.diff_text
    assert "canAttemptFailover" not in llxprt.input.diff_text
    assert 'source.meta[\\"file_path\\"]' not in haystack.input.diff_text
    assert 'source.meta["file_path"]' in haystack.input.diff_text
    assert "test/components/converters/test_json.py" not in haystack.input.diff_text


def test_reverse_fixture_production_only_diffs() -> None:
    haystack_12257 = Fixture.model_validate_json(
        FIXTURE_PATHS[3].read_text(encoding="utf-8")
    )
    assert "test/" not in haystack_12257.input.diff_text
    assert "releasenotes/" not in haystack_12257.input.diff_text
    assert "diff --git a/haystack/utils/filters.py" in haystack_12257.input.diff_text
    assert (
        "diff --git a/haystack/components/routers/metadata_router.py"
        in haystack_12257.input.diff_text
    )
    assert (
        "diff --git a/haystack/document_stores/in_memory/document_store.py"
        in haystack_12257.input.diff_text
    )

    haystack_12162 = Fixture.model_validate_json(
        FIXTURE_PATHS[2].read_text(encoding="utf-8")
    )
    assert "test/" not in haystack_12162.input.diff_text
    assert "releasenotes/" not in haystack_12162.input.diff_text
    # The independent _NoOutputProduced sentinel regression must not be folded
    # into the snapshot/resume gold: none of its production files may be reversed.
    assert (
        "diff --git a/haystack/core/pipeline/component_checks.py"
        not in haystack_12162.input.diff_text
    )
    assert (
        "diff --git a/haystack/core/component/types.py"
        not in haystack_12162.input.diff_text
    )
    assert (
        "diff --git a/haystack/core/pipeline/base.py"
        not in haystack_12162.input.diff_text
    )


def test_manifest_indexes_all_reverse_fixtures_once() -> None:
    manifest = json.loads(
        (ROOT / "eval/fixtures/manifest.json").read_text(encoding="utf-8")
    )

    for path in FIXTURE_PATHS:
        fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
        matching_entries = [
            entry for entry in manifest["entries"] if entry["fixture_id"] == fixture.id
        ]
        assert len(matching_entries) == 1
        assert matching_entries[0]["path"] == path.relative_to(ROOT).as_posix()
        assert matching_entries[0]["suite"] == "golden"
        assert matching_entries[0]["fixture_type"] == "review"
        assert matching_entries[0]["repo_full_name"] == fixture.source.repo_full_name
