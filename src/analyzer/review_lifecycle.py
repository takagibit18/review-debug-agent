"""Minimal human-feedback and review-skill lifecycle primitives."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from pydantic import BaseModel, Field, model_validator

from src.analyzer.review_skills import ReviewSkill


class FeedbackRecord(BaseModel):
    """Compact human correction retained for later skill improvement."""

    id: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    human_verdict: str = Field(min_length=1)
    human_reason: str = Field(min_length=1)
    finding_summary: str = Field(min_length=1)
    finding_reasoning: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_nonblank_text(self) -> "FeedbackRecord":
        for field in (
            "id",
            "finding_id",
            "human_verdict",
            "human_reason",
            "finding_summary",
            "finding_reasoning",
        ):
            value = getattr(self, field).strip()
            if not value:
                raise ValueError(f"feedback field {field} must not be blank")
            setattr(self, field, value)
        return self


class SkillProposal(BaseModel):
    """Small model response required from an Improver."""

    category: str = Field(min_length=1)
    principle: str = Field(min_length=1)
    why: str = Field(min_length=1)
    source_feedback_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_nonblank_text(self) -> "SkillProposal":
        for field in ("category", "principle", "why"):
            value = getattr(self, field).strip()
            if not value:
                raise ValueError(f"proposal field {field} must not be blank")
            setattr(self, field, value)
        self.source_feedback_ids = [
            item.strip() for item in self.source_feedback_ids if item.strip()
        ]
        if not self.source_feedback_ids:
            raise ValueError("proposal must cite source feedback ids")
        return self


class FeedbackStore:
    """Append-only JSONL storage for compact human feedback records."""

    def __init__(self, path: Path | str | None = None) -> None:
        default = Path(__file__).resolve().parents[2] / "review_experience" / "feedback.jsonl"
        self.path = Path(path).resolve() if path is not None else default

    def read(self) -> list[FeedbackRecord]:
        if not self.path.exists():
            return []
        records: list[FeedbackRecord] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                records.append(FeedbackRecord.model_validate(value))
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise ValueError(
                    f"malformed feedback at {self.path}:{line_number}"
                ) from exc
        return records

    def append(self, value: FeedbackRecord | Mapping[str, Any]) -> FeedbackRecord:
        record = (
            value
            if isinstance(value, FeedbackRecord)
            else FeedbackRecord.model_validate(value)
        )
        if any(item.id == record.id for item in self.read()):
            raise ValueError(f"feedback id already exists: {record.id}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(record.model_dump_json() + "\n")
        return record


class SkillStore:
    """Small JSONL store supporting only the allowed skill lifecycle states."""

    def __init__(self, path: Path | str | None = None) -> None:
        default = Path(__file__).resolve().parents[2] / "review_skills" / "learned.jsonl"
        self.path = Path(path).resolve() if path is not None else default

    def read(self) -> list[ReviewSkill]:
        if not self.path.exists():
            return []
        skills: list[ReviewSkill] = []
        for line_number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                skill = ReviewSkill.from_record(json.loads(line))
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError(
                    f"malformed skill at {self.path}:{line_number}"
                ) from exc
            if skill is None:
                raise ValueError(f"malformed skill at {self.path}:{line_number}")
            skills.append(skill)
        return skills

    def add_candidate(self, proposal: SkillProposal | Mapping[str, Any]) -> ReviewSkill:
        normalized = (
            proposal
            if isinstance(proposal, SkillProposal)
            else SkillProposal.model_validate(proposal)
        )
        skills = self.read()
        skill = ReviewSkill(
            id=self._next_id(skills),
            status="candidate",
            category=normalized.category,
            principle=normalized.principle,
            why=normalized.why,
            source_feedback_ids=tuple(normalized.source_feedback_ids),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(skill.to_record(), ensure_ascii=False) + "\n")
        return skill

    def update_status(self, skill_id: str, status: str) -> ReviewSkill:
        if status not in {"active", "deprecated"}:
            raise ValueError("skill status must be active or deprecated")
        skills = self.read()
        target = next((item for item in skills if item.id == skill_id), None)
        if target is None:
            raise ValueError(f"skill not found: {skill_id}")
        if status == "active" and target.status != "candidate":
            raise ValueError("only candidate skills can be activated")
        if status == "deprecated" and target.status != "active":
            raise ValueError("only active skills can be deprecated")
        updated = ReviewSkill(
            id=target.id,
            status=status,
            category=target.category,
            principle=target.principle,
            why=target.why,
            source_feedback_ids=target.source_feedback_ids,
        )
        rewritten = [updated if item.id == skill_id else item for item in skills]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "".join(
                json.dumps(item.to_record(), ensure_ascii=False) + "\n"
                for item in rewritten
            ),
            encoding="utf-8",
        )
        return updated

    @staticmethod
    def _next_id(skills: Sequence[ReviewSkill]) -> str:
        used = {item.id for item in skills}
        numbers = [
            int(match.group(1))
            for item in skills
            if (match := re.fullmatch(r"skill-(\d+)", item.id)) is not None
        ]
        number = max(numbers, default=0) + 1
        candidate = f"skill-{number:03d}"
        while candidate in used:
            number += 1
            candidate = f"skill-{number:03d}"
        return candidate


class Improver(Protocol):
    """The deliberately tiny interface used by the proposal workflow."""

    def propose(
        self, feedback: Sequence[FeedbackRecord]
    ) -> SkillProposal | Mapping[str, Any]: ...


def build_contrastive_prompt(feedback: Sequence[FeedbackRecord]) -> str:
    """Describe the missing reusable reasoning step, not the concrete incident."""

    examples = "\n\n".join(
        "Agent reasoning:\n"
        + item.finding_reasoning
        + "\nHuman verdict: "
        + item.human_verdict
        + "\nHuman correction:\n"
        + item.human_reason
        for item in feedback
    )
    return (
        "Perform contrastive analysis over the review feedback below. Compare the agent's "
        "reasoning with the human correction, identify the missing reusable engineering "
        "check, and return JSON with category, principle, why, and source_feedback_ids. "
        "Write a transferable principle, not a case-specific file/line reminder and not "
        "a vague instruction such as 'do not make this mistake'.\n\n"
        + examples
    )


class PromptImprover:
    """Adapt one injected completion callable without introducing a provider layer."""

    def __init__(self, complete: Callable[[str], Mapping[str, Any]]) -> None:
        self._complete = complete
        self.last_prompt = ""

    def propose(
        self, feedback: Sequence[FeedbackRecord]
    ) -> SkillProposal | Mapping[str, Any]:
        self.last_prompt = build_contrastive_prompt(feedback)
        return self._complete(self.last_prompt)


class StaticImprover:
    """Offline fake response used by tests and the CLI's explicit JSON mode."""

    def __init__(self, response: Mapping[str, Any]) -> None:
        self.response = dict(response)
        self.calls = 0
        self.last_feedback: tuple[FeedbackRecord, ...] = ()

    def propose(
        self, feedback: Sequence[FeedbackRecord]
    ) -> SkillProposal | Mapping[str, Any]:
        self.calls += 1
        self.last_feedback = tuple(feedback)
        return dict(self.response)


def propose_skill(
    feedback_store: FeedbackStore,
    skill_store: SkillStore,
    improver: Improver,
    *,
    max_feedback: int = 10,
) -> ReviewSkill:
    """Batch detailed feedback through the Improver and persist one candidate."""

    if max_feedback < 1:
        raise ValueError("max_feedback must be positive")
    consumed_feedback_ids = {
        feedback_id
        for skill in skill_store.read()
        for feedback_id in skill.source_feedback_ids
    }
    all_feedback = feedback_store.read()
    unconsumed_feedback = [
        item for item in all_feedback if item.id not in consumed_feedback_ids
    ]
    feedback = [
        item
        for item in unconsumed_feedback
        if item.human_reason.strip() and item.finding_reasoning.strip()
    ][:max_feedback]
    if not feedback:
        if all_feedback and not unconsumed_feedback:
            raise ValueError("no unconsumed feedback is available")
        raise ValueError("no detailed human feedback is available")
    raw_proposal = improver.propose(feedback)
    proposal = (
        raw_proposal
        if isinstance(raw_proposal, SkillProposal)
        else SkillProposal.model_validate(raw_proposal)
    )
    known_ids = {item.id for item in feedback}
    if any(item not in known_ids for item in proposal.source_feedback_ids):
        raise ValueError("proposal cites feedback that was not supplied")
    return skill_store.add_candidate(proposal)
