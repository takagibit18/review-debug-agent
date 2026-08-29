"""Offline tests for feedback recording and skill lifecycle transitions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scripts import review_experience
from scripts.review_experience import cli
from src.analyzer.review_lifecycle import (
    FeedbackRecord,
    FeedbackStore,
    PromptImprover,
    SkillStore,
    StaticImprover,
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


def _proposal(feedback_id: str) -> dict[str, object]:
    return {
        "category": "concurrency",
        "principle": "Confirm a path can execute concurrently before reporting a race.",
        "why": "Shared mutable state alone does not establish concurrent execution.",
        "source_feedback_ids": [feedback_id],
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
        FeedbackRecord.model_validate(_feedback("github-review-comment-123")),
        FeedbackRecord.model_validate(_feedback("github-review-comment-456")),
    ]

    captured: list[str] = []

    def _complete(prompt: str) -> dict[str, object]:
        captured.append(prompt)
        return {
            "category": "concurrency",
            "principle": "Confirm a path can execute concurrently before reporting a race.",
            "why": "Shared mutable state alone does not establish concurrent execution.",
            "source_feedback_ids": ["github-review-comment-123"],
        }

    improver = PromptImprover(_complete)
    proposal = improver.propose(feedback)

    assert proposal["category"] == "concurrency"
    assert "Feedback ID: github-review-comment-123" in captured[0]
    assert "Feedback ID: github-review-comment-456" in captured[0]
    assert "Agent reasoning" in captured[0]
    assert "Human verdict" in captured[0]
    assert "Human correction" in captured[0]
    assert "contrastive analysis" in captured[0].lower()
    assert "Return JSON only with exactly these fields:" in captured[0]
    assert "source_feedback_ids" in captured[0]


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


def test_propose_does_not_reuse_feedback_after_creating_candidate(tmp_path: Path) -> None:
    feedback_store = FeedbackStore(tmp_path / "feedback.jsonl")
    feedback_store.append(_feedback("fb-001"))
    skill_store = SkillStore(tmp_path / "learned.jsonl")

    first_improver = StaticImprover(_proposal("fb-001"))
    first = propose_skill(feedback_store, skill_store, first_improver)
    assert first.status == "candidate"

    feedback_store.append(_feedback("fb-002"))
    second_improver = StaticImprover(_proposal("fb-002"))
    propose_skill(feedback_store, skill_store, second_improver)

    assert [item.id for item in second_improver.last_feedback] == ["fb-002"]


def test_propose_excludes_feedback_cited_by_existing_skill(tmp_path: Path) -> None:
    feedback_store = FeedbackStore(tmp_path / "feedback.jsonl")
    feedback_store.append(_feedback("fb-001"))
    feedback_store.append(_feedback("fb-002"))
    skill_store = SkillStore(tmp_path / "learned.jsonl")
    skill_store.add_candidate(_proposal("fb-001"))

    improver = StaticImprover(_proposal("fb-002"))
    propose_skill(feedback_store, skill_store, improver)

    assert [item.id for item in improver.last_feedback] == ["fb-002"]


@pytest.mark.parametrize("status", ["candidate", "active", "deprecated"])
def test_all_skill_statuses_consume_source_feedback(
    tmp_path: Path, status: str
) -> None:
    feedback_store = FeedbackStore(tmp_path / "feedback.jsonl")
    feedback_store.append(_feedback("fb-001"))
    feedback_store.append(_feedback("fb-002"))
    skill_store = SkillStore(tmp_path / "learned.jsonl")
    skill = skill_store.add_candidate(_proposal("fb-001"))
    if status in {"active", "deprecated"}:
        skill_store.update_status(skill.id, "active")
    if status == "deprecated":
        skill_store.update_status(skill.id, "deprecated")

    improver = StaticImprover(_proposal("fb-002"))
    propose_skill(feedback_store, skill_store, improver)

    assert [item.id for item in improver.last_feedback] == ["fb-002"]


def test_propose_fails_when_all_feedback_is_consumed(tmp_path: Path) -> None:
    feedback_store = FeedbackStore(tmp_path / "feedback.jsonl")
    feedback_store.append(_feedback("fb-001"))
    skill_store = SkillStore(tmp_path / "learned.jsonl")
    skill_store.add_candidate(_proposal("fb-001"))
    improver = StaticImprover(_proposal("fb-001"))

    with pytest.raises(ValueError, match="^no unconsumed feedback is available$"):
        propose_skill(feedback_store, skill_store, improver)

    assert improver.calls == 0


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


def test_cli_ingest_github_uses_token_and_reports_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeGitHubClient:
        def __init__(self, token: str) -> None:
            assert token == "test-token"

        async def list_review_comments(
            self,
            owner_repo: str,
            pr_number: int,
        ) -> list[dict[str, object]]:
            assert owner_repo == "owner/repo"
            assert pr_number == 88
            return [
                {
                    "id": 100,
                    "body": "\n\n".join(
                        [
                            "Finding summary",
                            "Evidence: published agent judgment",
                            "<!-- mergewarden:comment -->",
                            (
                                '<!-- mergewarden:{"fingerprint":"fp",'
                                '"finding_id":"F-02"} -->'
                            ),
                        ]
                    ),
                    "user": {"login": "mergewarden[bot]", "type": "Bot"},
                },
                {
                    "id": 200,
                    "in_reply_to_id": 100,
                    "body": "/mw-feedback invalid\nThe path is serialized.",
                    "user": {"login": "alice", "type": "User"},
                },
            ]

        async def close(self) -> None:
            pass

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(review_experience, "GitHubApiClient", FakeGitHubClient)
    feedback_file = tmp_path / "feedback.jsonl"

    result = CliRunner().invoke(
        cli,
        [
            "ingest-github",
            "--repo",
            "owner/repo",
            "--pr",
            "88",
            "--feedback-file",
            str(feedback_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "imported: 1" in result.output
    assert "duplicate: 0" in result.output
    assert "ignored: 1" in result.output
    assert FeedbackStore(feedback_file).read()[0].finding_id == "F-02"
