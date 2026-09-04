"""Offline tests for lightweight reviewer skill loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analyzer.context_state import ContextState
from src.analyzer.prompts import build_review_messages, review_system_prompt
from src.analyzer.review_skills import (
    ReviewSkill,
    ReviewSkillLoader,
    SkillQuery,
    build_skill_query,
)
from src.analyzer.schemas import ReviewRequest


def _write_skill_files(tmp_path: Path) -> None:
    tmp_path.joinpath("core.md").write_text(
        "Core principle: anchor risks in the current change.", encoding="utf-8"
    )
    records = [
        {
            "id": "skill-active-1",
            "status": "active",
            "category": "concurrency",
            "principle": "Check whether the path can really execute concurrently.",
            "why": "Shared state alone does not prove a race.",
            "source_feedback_ids": ["fb-1"],
        },
        {
            "id": "skill-candidate-1",
            "status": "candidate",
            "category": "noise",
            "principle": "This candidate must not enter the reviewer yet.",
            "why": "Human activation is required.",
            "source_feedback_ids": ["fb-2"],
        },
        {
            "id": "skill-deprecated-1",
            "status": "deprecated",
            "category": "old",
            "principle": "This deprecated principle must not be loaded.",
            "why": "It remains only for history.",
            "source_feedback_ids": ["fb-3"],
        },
        {
            "id": "skill-active-2",
            "status": "active",
            "category": "contracts",
            "principle": "Compare changed behavior with the retained contract.",
            "why": "Unchanged callers can expose a new inconsistency.",
            "source_feedback_ids": ["fb-4"],
        },
    ]
    tmp_path.joinpath("learned.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def test_core_is_always_loaded_and_only_active_skills_are_selected(tmp_path: Path) -> None:
    _write_skill_files(tmp_path)
    loader = ReviewSkillLoader(tmp_path)

    assert [skill.id for skill in loader.load_active_skills()] == [
        "skill-active-1",
        "skill-active-2",
    ]
    context = loader.render()
    assert context.startswith("<review_skills>\n")
    assert context.endswith("\n</review_skills>")
    assert "Core principle" in context
    assert "Check whether the path" in context
    assert "Compare changed behavior" in context
    assert "candidate must not enter" not in context
    assert "deprecated principle" not in context


def test_skill_budget_truncation_is_deterministic_and_core_first(tmp_path: Path) -> None:
    _write_skill_files(tmp_path)
    loader = ReviewSkillLoader(tmp_path, max_chars=230)

    first = loader.render()
    second = loader.render()

    assert first == second
    assert len(first) <= 230
    assert "Core principle" in first
    assert "Check whether the path" in first
    assert "Compare changed behavior" not in first


def test_reviewer_system_prompt_contains_skills_for_both_context_modes(
    tmp_path: Path,
) -> None:
    _write_skill_files(tmp_path)
    loader = ReviewSkillLoader(tmp_path)
    request = ReviewRequest(repo_path=".")

    for mode, policy_phrase in (
        ("agent_search", "Context policy: agent_search"),
        ("graph_hybrid", "Context policy: graph_hybrid"),
    ):
        prompt = review_system_prompt(mode, skill_loader=loader)
        assert "<review_skills>" in prompt
        assert "Core principle" in prompt
        assert "Check whether the path" in prompt
        assert policy_phrase in prompt

        messages = build_review_messages(
            request,
            ContextState(context_mode=mode),
            "",
            {},
            skill_loader=loader,
        )
        assert messages[0].content == prompt
        assert messages[1].role == "user"


def _write_records(tmp_path: Path, records: list[dict[str, object]]) -> None:
    tmp_path.joinpath("core.md").write_text("Core invariant.", encoding="utf-8")
    tmp_path.joinpath("learned.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _record(skill_id: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": skill_id,
        "status": "active",
        "category": "contracts",
        "principle": f"Principle {skill_id}",
        "why": "Because contracts matter.",
        "source_feedback_ids": [],
    }
    value.update(overrides)
    return value


def test_optional_metadata_is_normalized_and_malformed_values_fall_back() -> None:
    old = ReviewSkill.from_record(_record("old"))
    malformed = ReviewSkill.from_record(
        _record(
            "bad",
            languages="python",
            path_globs=[1],
            triggers={"asyncio": True},
        )
    )
    assert old is not None and old.description == old.principle
    assert malformed is not None
    assert malformed.languages == malformed.path_globs == malformed.triggers == ()


def test_retrieval_filters_status_and_explicit_scope_then_ranks_signals(
    tmp_path: Path,
) -> None:
    _write_records(
        tmp_path,
        [
            _record("skill-trigger", languages=["PYTHON"], triggers=["ASYNCIO"]),
            _record("skill-path", languages=["python"], path_globs=["src/**/*.py"]),
            _record("skill-other", languages=["rust"]),
            _record("skill-candidate", status="candidate", languages=["python"]),
            _record("skill-deprecated", status="deprecated", languages=["python"]),
        ],
    )
    query = build_skill_query(
        "diff --git a/src/pkg/app.py b/src/pkg/app.py\n"
        "--- a/src/pkg/app.py\n+++ b/src/pkg/app.py\n"
        "@@ -1 +1 @@\n-import time\n+import asyncio\n"
    )
    result = ReviewSkillLoader(tmp_path).retrieve(query)
    assert [item.skill.id for item in result.selected] == [
        "skill-trigger",
        "skill-path",
    ]
    assert dict(result.skipped)["skill-other"] == "language_mismatch"
    assert dict(result.skipped)["skill-candidate"] == "status"
    assert dict(result.skipped)["skill-deprecated"] == "status"
    assert result.total_chars <= 4000


def test_top_k_tie_break_and_bank_digest_are_file_order_independent(
    tmp_path: Path,
) -> None:
    records = [
        _record("skill-b", languages=["python"]),
        _record("skill-a", languages=["python"]),
    ]
    _write_records(tmp_path, records)
    loader = ReviewSkillLoader(tmp_path)
    query = SkillQuery(("app.py",), ("python",), "app.py")
    first = loader.retrieve(query, top_k=1)
    _write_records(tmp_path, list(reversed(records)))
    second = loader.retrieve(query, top_k=1)
    assert [item.skill.id for item in first.selected] == ["skill-a"]
    assert first.selected == second.selected
    assert first.bank_digest == second.bank_digest
    records[0]["why"] = "Changed bank content."
    _write_records(tmp_path, records)
    assert loader.retrieve(query).bank_digest != first.bank_digest


def test_budget_skips_oversized_item_and_keeps_full_core(tmp_path: Path) -> None:
    _write_records(
        tmp_path,
        [
            _record("skill-large", triggers=["asyncio"], principle="X" * 500),
            _record("skill-small", languages=["python"], principle="Short."),
        ],
    )
    result = ReviewSkillLoader(tmp_path, max_chars=140).retrieve(
        SkillQuery(("app.py",), ("python",), "asyncio")
    )
    assert [item.skill.id for item in result.selected] == ["skill-small"]
    assert ("skill-large", "budget") in result.skipped
    assert "Core invariant." in result.context
    assert len(result.context) <= 140


def test_retrieval_rejects_core_that_cannot_fit(tmp_path: Path) -> None:
    _write_records(tmp_path, [])
    tmp_path.joinpath("core.md").write_text("X" * 100, encoding="utf-8")
    with pytest.raises(ValueError, match="Core exceeds"):
        ReviewSkillLoader(tmp_path, max_chars=60).retrieve(
            SkillQuery((), (), "")
        )


def test_query_extracts_diff_and_optional_graph_signals() -> None:
    diff = (
        "diff --git a/web/view.tsx b/web/view.tsx\n"
        "--- a/web/view.tsx\n+++ b/web/view.tsx\n"
        "@@ -1 +1 @@ render\n-old\n+await load()\n"
    )
    base = build_skill_query(diff)
    enriched = build_skill_query(
        diff,
        [{
            "changed_anchor": {"symbol_id": "View.render", "change_kind": "api_handler"},
            "included_spans": [{"symbol_id": "load"}],
            "included_graph_paths": [{
                "edge_kinds": ["calls"], "node_ids": ["View.render", "load"]
            }],
        }],
    )
    assert base.changed_files == ("web/view.tsx",)
    assert base.languages == ("typescript",)
    assert "await load()" in base.lexical_corpus
    assert base.changed_symbols == ()
    assert enriched.changed_symbols == ("load", "view.render")
    assert enriched.change_kinds == ("api_handler",)
    assert enriched.graph_edge_kinds == ("calls",)
