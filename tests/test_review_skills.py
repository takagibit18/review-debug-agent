"""Offline tests for lightweight reviewer skill loading."""

from __future__ import annotations

import json
from pathlib import Path

from src.analyzer.context_state import ContextState
from src.analyzer.prompts import build_review_messages, review_system_prompt
from src.analyzer.review_skills import ReviewSkillLoader
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
