"""Small deterministic loader for reviewer engineering principles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILL_STATUSES = frozenset({"candidate", "active", "deprecated"})
DEFAULT_SKILL_CHAR_BUDGET = 4_000


@dataclass(frozen=True)
class ReviewSkill:
    """A persisted review principle and its lifecycle metadata."""

    id: str
    status: str
    category: str
    principle: str
    why: str
    source_feedback_ids: tuple[str, ...] = ()

    @classmethod
    def from_record(cls, value: Any) -> "ReviewSkill | None":
        if not isinstance(value, dict):
            return None
        status = str(value.get("status", "")).strip().lower()
        required = ("id", "category", "principle", "why")
        if status not in SKILL_STATUSES or any(
            not str(value.get(field, "")).strip() for field in required
        ):
            return None
        raw_sources = value.get("source_feedback_ids", [])
        if not isinstance(raw_sources, list) or not all(
            isinstance(item, str) for item in raw_sources
        ):
            return None
        return cls(
            id=str(value["id"]).strip(),
            status=status,
            category=str(value["category"]).strip(),
            principle=str(value["principle"]).strip(),
            why=str(value["why"]).strip(),
            source_feedback_ids=tuple(
                item.strip() for item in raw_sources if item.strip()
            ),
        )

    def to_record(self) -> dict[str, Any]:
        """Return the intentionally small persisted skill representation."""

        return {
            "id": self.id,
            "status": self.status,
            "category": self.category,
            "principle": self.principle,
            "why": self.why,
            "source_feedback_ids": list(self.source_feedback_ids),
        }


class ReviewSkillLoader:
    """Load core and active learned skills within a deterministic char budget."""

    def __init__(
        self,
        skills_dir: Path | str | None = None,
        *,
        max_chars: int = DEFAULT_SKILL_CHAR_BUDGET,
    ) -> None:
        default_dir = Path(__file__).resolve().parents[2] / "review_skills"
        self.skills_dir = Path(skills_dir).resolve() if skills_dir else default_dir
        self.max_chars = max(1, int(max_chars))

    @property
    def core_path(self) -> Path:
        return self.skills_dir / "core.md"

    @property
    def learned_path(self) -> Path:
        return self.skills_dir / "learned.jsonl"

    def load_core(self) -> str:
        """Return the short always-on core principle text."""

        try:
            return self.core_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""

    def load_active_skills(self) -> list[ReviewSkill]:
        """Return valid active records in their stable file order."""

        try:
            lines = self.learned_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []
        skills: list[ReviewSkill] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            skill = ReviewSkill.from_record(record)
            if skill is not None and skill.status == "active":
                skills.append(skill)
        return skills

    def render(self) -> str:
        """Render the bounded prompt section with an explicit prompt boundary."""

        prefix = "<review_skills>\n"
        suffix = "\n</review_skills>"
        available = max(0, self.max_chars - len(prefix) - len(suffix))
        core = self.load_core()
        body = core[:available].rstrip()
        for skill in self.load_active_skills():
            rendered = self._render_skill(skill)
            candidate = f"{body}\n\n{rendered}" if body else rendered
            if len(candidate) > available:
                break
            body = candidate
        return f"{prefix}{body}{suffix}"

    def load_context(self) -> str:
        """Compatibility-friendly alias for the rendered prompt context."""

        return self.render()

    @staticmethod
    def _render_skill(skill: ReviewSkill) -> str:
        return f"- [{skill.category}] {skill.principle}\n  Why: {skill.why}"


def review_skill_context(
    skills_dir: Path | str | None = None,
    *,
    max_chars: int = DEFAULT_SKILL_CHAR_BUDGET,
) -> str:
    """Build the bounded review-skill prompt section."""

    return ReviewSkillLoader(skills_dir, max_chars=max_chars).render()
