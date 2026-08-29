"""Offline tests for feedback recording and skill lifecycle transitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scripts.review_experience import cli
from src.analyzer.review_lifecycle import (
    FeedbackRecord,
    FeedbackStore,
    PromptImprover,
    SkillStore,
    StaticImprover,
    build_contrastive_prompt,
    propose_skill,
)
from src.analyzer.review_skills import ReviewSkillLoader


def _feedback(feedback_id: str = "fb-001") -> dict[str, str]:
    return {
        "id": feedback_id,
        "finding_id": "F-01",
        "human_verdict": "invalid",
        "human_reason": (
            "The callbacks run serially on one event loop, so shared state is not "
            "evidence of a race."
        ),
        "finding_summary": "Shared state may race",
        "finding_reasoning": "The agent saw shared mutable state and inferred concurrency.",
    }


def test_feedback_append_and_malformed_feedback_are_handled(tmp_path: Path) -> None:
    store = FeedbackStore(tmp_path / "feedback.jsonl")
    saved = store.append(_feedback())

    assert saved.id == "fb-001"
    assert store.read()[0].human_reason.startswith("The callbacks")
    with pytest.raises(ValueError):
        store.append({"id": "fb-bad", "human_verdict": "invalid"})


def test_contrastive_improver_receives_agent_and_human_reasoning() -> None:
    feedback = [
        # Use the validated store model so the Improver contract receives typed data.
        FeedbackRecord.model_validate(_feedback())
    ]

    captured: list[str] = []

    def _complete(prompt: str) -> dict[str, object]:
        captured.append(prompt)
        return {
            "category": "concurrency",
            "principle": "Confirm a path can execute concurrently before reporting a race.",
            "why": "Shared mutable state alone does not establish concurrent execution.",
            "source_feedback_ids": ["fb-001"],
        }

    improver = PromptImprover(_complete)
    proposal = improver.propose(feedback)

    assert proposal["category"] == "concurrency"
    assert "Agent reasoning" in captured[0]
    assert "Human correction" in captured[0]
    assert "contrastive analysis" in captured[0].lower()
    assert "source_feedback_ids" in build_contrastive_prompt(feedback)


def test_fake_improver_creates_candidate_that_is_not_loaded_until_activation(
    tmp_path: Path,
) -> None:
    feedback_store = FeedbackStore(tmp_path / "feedback.jsonl")
    feedback_store.append(_feedback())
    skill_store = SkillStore(tmp_path / "learned.jsonl")
    improver = StaticImprover(
        {
            "category": "concurrency",
            "principle": "Confirm a path can execute concurrently before reporting a race.",
            "why": "Shared mutable state alone does not establish concurrent execution.",
            "source_feedback_ids": ["fb-001"],
        }
    )

    skill = propose_skill(feedback_store, skill_store, improver)

    assert skill.status == "candidate"
    assert improver.calls == 1
    assert "Confirm a path" not in ReviewSkillLoader(tmp_path).render()

    active = skill_store.update_status(skill.id, "active")
    assert active.status == "active"
    assert "Confirm a path" in ReviewSkillLoader(tmp_path).render()

    deprecated = skill_store.update_status(skill.id, "deprecated")
    assert deprecated.status == "deprecated"
    assert "Confirm a path" not in ReviewSkillLoader(tmp_path).render()
    assert skill_store.read()[0].status == "deprecated"


def test_cli_record_propose_activate_deprecate_round_trip(tmp_path: Path) -> None:
    runner = CliRunner()
    feedback_file = tmp_path / "feedback.jsonl"
    skills_file = tmp_path / "learned.jsonl"
    proposal = json.dumps(
        {
            "category": "contracts",
            "principle": "Compare the changed behavior with unchanged callers.",
            "why": "A PR can create inconsistency across an existing boundary.",
            "source_feedback_ids": ["fb-001"],
        }
    )
    common = ["--feedback-file", str(feedback_file)]

    result = runner.invoke(
        cli,
        [
            "record",
            *common,
            "--id",
            "fb-001",
            "--finding-id",
            "F-01",
            "--human-verdict",
            "invalid",
            "--human-reason",
            "The unchanged caller retains a different contract.",
            "--finding-summary",
            "The new behavior is inconsistent.",
            "--finding-reasoning",
            "The agent inspected only the changed branch.",
        ],
    )
    assert result.exit_code == 0, result.output

    result = runner.invoke(
        cli,
        [
            "propose",
            *common,
            "--skills-file",
            str(skills_file),
            "--proposal-json",
            proposal,
        ],
    )
    assert result.exit_code == 0, result.output
    assert "created skill-001 (candidate)" in result.output

    assert "Compare the changed" not in ReviewSkillLoader(tmp_path).render()
    result = runner.invoke(
        cli, ["activate", "skill-001", "--skills-file", str(skills_file)]
    )
    assert result.exit_code == 0, result.output
    assert "Compare the changed" in ReviewSkillLoader(tmp_path).render()

    result = runner.invoke(
        cli, ["deprecate", "skill-001", "--skills-file", str(skills_file)]
    )
    assert result.exit_code == 0, result.output
    assert "Compare the changed" not in ReviewSkillLoader(tmp_path).render()
    assert SkillStore(skills_file).read()[0].status == "deprecated"
