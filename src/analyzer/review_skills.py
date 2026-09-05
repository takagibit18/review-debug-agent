"""Deterministic loading and retrieval for reviewer engineering principles."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from src.analyzer.diff_lines import parse_unified_diff_hunks

SKILL_STATUSES = frozenset({"candidate", "active", "deprecated"})
DEFAULT_SKILL_CHAR_BUDGET = 4_000
DEFAULT_SKILL_TOP_K = 5
DEFAULT_LEGACY_FALLBACK_LIMIT = 1
MAX_QUERY_CORPUS_CHARS = 32_000
TRIGGER_SCORE = 100
PATH_SCORE = 40
LANGUAGE_SCORE = 20
GRAPH_SCORE = 15
LEGACY_SCORE = 1
RETRIEVAL_VERSION = "deterministic-v1"
_PREFIX = "<review_skills>\n"
_SUFFIX = "\n</review_skills>"
_LANGUAGE_BY_SUFFIX = {
    ".c": "c", ".cc": "cpp", ".cpp": "cpp", ".cs": "csharp",
    ".css": "css", ".go": "go", ".h": "c", ".hpp": "cpp",
    ".html": "html", ".java": "java", ".js": "javascript",
    ".jsx": "javascript", ".kt": "kotlin", ".php": "php",
    ".py": "python", ".rb": "ruby", ".rs": "rust", ".scala": "scala",
    ".sh": "shell", ".sql": "sql", ".swift": "swift", ".toml": "toml",
    ".ts": "typescript", ".tsx": "typescript", ".vue": "vue",
    ".yaml": "yaml", ".yml": "yaml",
}

_OPTIONAL_METADATA_FIELDS = ("languages", "path_globs", "triggers")


def _optional_tuple(
    value: Any, *, lower: bool = False, path: bool = False
) -> tuple[str, ...]:
    if not _is_valid_optional_metadata(value):
        return ()
    normalized: list[str] = []
    for item in value:
        text = item.strip()
        if path:
            text = text.replace("\\", "/")
            while "//" in text:
                text = text.replace("//", "/")
        if lower:
            text = text.lower()
        if text and text not in normalized:
            normalized.append(text)
    return tuple(normalized)


def _is_valid_optional_metadata(value: Any) -> bool:
    """Return whether an optional routing field has the persisted list shape."""

    return isinstance(value, (list, tuple)) and all(
        isinstance(item, str) for item in value
    )


def _metadata_warnings(value: Mapping[str, Any]) -> tuple[str, ...]:
    """Identify optional fields that were present but could not be normalized.

    Missing optional metadata remains a supported legacy shape.  A field that is
    present with a non-list or non-string value is different: treating it as
    missing silently widens the skill's applicability.  The warning marker is
    persisted by ``to_record`` so the bank digest remains sensitive to the
    malformed state after a lifecycle rewrite.
    """

    warnings: list[str] = []
    persisted = value.get("metadata_warnings")
    if _is_valid_optional_metadata(persisted):
        warnings.extend(item.strip() for item in persisted if item.strip())
    for field in _OPTIONAL_METADATA_FIELDS:
        if field in value and not _is_valid_optional_metadata(value[field]):
            warnings.append(field)
    return tuple(dict.fromkeys(warnings))


@dataclass(frozen=True)
class ReviewSkill:
    """A persisted review principle and its lifecycle/routing metadata."""

    id: str
    status: str
    category: str
    principle: str
    why: str
    source_feedback_ids: tuple[str, ...] = ()
    description: str = ""
    languages: tuple[str, ...] = ()
    path_globs: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    metadata_warnings: tuple[str, ...] = ()

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
        principle = str(value["principle"]).strip()
        metadata_warnings = _metadata_warnings(value)
        return cls(
            id=str(value["id"]).strip(),
            status=status,
            category=str(value["category"]).strip(),
            principle=principle,
            why=str(value["why"]).strip(),
            source_feedback_ids=_optional_tuple(raw_sources),
            description=str(value.get("description", "")).strip() or principle,
            languages=_optional_tuple(value.get("languages"), lower=True),
            path_globs=_optional_tuple(value.get("path_globs"), path=True),
            triggers=_optional_tuple(value.get("triggers"), lower=True),
            metadata_warnings=metadata_warnings,
        )

    @property
    def scoped(self) -> bool:
        return bool(self.languages or self.path_globs or self.triggers)

    def to_record(self) -> dict[str, Any]:
        """Return the backward-compatible persisted skill representation."""

        record: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "category": self.category,
            "principle": self.principle,
            "why": self.why,
            "source_feedback_ids": list(self.source_feedback_ids),
        }
        if self.description and self.description != self.principle:
            record["description"] = self.description
        if self.languages:
            record["languages"] = list(self.languages)
        if self.path_globs:
            record["path_globs"] = list(self.path_globs)
        if self.triggers:
            record["triggers"] = list(self.triggers)
        if self.metadata_warnings:
            record["metadata_warnings"] = list(self.metadata_warnings)
        return record


@dataclass(frozen=True)
class SkillQuery:
    changed_files: tuple[str, ...]
    languages: tuple[str, ...]
    lexical_corpus: str
    changed_symbols: tuple[str, ...] = ()
    change_kinds: tuple[str, ...] = ()
    graph_edge_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillMatch:
    skill: ReviewSkill
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SkillSelection:
    context: str
    selected: tuple[SkillMatch, ...]
    skipped: tuple[tuple[str, str], ...]
    core_chars: int
    learned_chars: int
    total_chars: int
    estimated_tokens: int
    bank_digest: str
    total_records: int = 0
    active_records: int = 0
    scoped_active_records: int = 0
    unscoped_active_records: int = 0
    candidate_count: int = 0
    malformed_active_records: int = 0
    legacy_only_fallback: bool = False


def build_skill_query(
    diff_text: str,
    context_manifests: Sequence[Mapping[str, Any]] | None = None,
) -> SkillQuery:
    """Extract bounded, provider-free routing signals from a diff and manifests."""

    hunks = parse_unified_diff_hunks(diff_text)
    changed_files = tuple(sorted(path.replace("\\", "/") for path in hunks))
    languages = tuple(sorted({
        language
        for path in changed_files
        if (language := _LANGUAGE_BY_SUFFIX.get(PurePosixPath(path).suffix.lower()))
    }))
    corpus_parts: list[str] = list(changed_files)
    for file_hunks in hunks.values():
        for hunk in file_hunks:
            corpus_parts.append(hunk.header)
            corpus_parts.extend(
                line[1:] for line in hunk.lines if line.startswith(("+", "-"))
            )
    lexical_corpus = "\n".join(corpus_parts).lower()[:MAX_QUERY_CORPUS_CHARS]
    symbols: set[str] = set()
    change_kinds: set[str] = set()
    edge_kinds: set[str] = set()
    for manifest in context_manifests or ():
        if not isinstance(manifest, Mapping):
            continue
        anchor = manifest.get("changed_anchor")
        if isinstance(anchor, Mapping):
            _collect_string(symbols, anchor.get("symbol_id"))
            _collect_string(change_kinds, anchor.get("change_kind"))
        for span in _mapping_list(manifest.get("included_spans")):
            _collect_string(symbols, span.get("symbol_id"))
        for graph_path in _mapping_list(manifest.get("included_graph_paths")):
            for edge in _mapping_list(graph_path.get("edges")):
                _collect_string(edge_kinds, edge.get("kind"))
            for edge_kind in _optional_tuple(
                graph_path.get("edge_kinds"), lower=True
            ):
                _collect_string(edge_kinds, edge_kind)
            for node_id in _optional_tuple(graph_path.get("node_ids"), lower=True):
                _collect_string(symbols, node_id)
    return SkillQuery(
        changed_files=changed_files,
        languages=languages,
        lexical_corpus=lexical_corpus,
        changed_symbols=tuple(sorted(symbols)),
        change_kinds=tuple(sorted(change_kinds)),
        graph_edge_kinds=tuple(sorted(edge_kinds)),
    )


class ReviewSkillLoader:
    """Load Core and atomically pack eligible learned review skills."""

    def __init__(
        self,
        skills_dir: Path | str | None = None,
        *,
        max_chars: int = DEFAULT_SKILL_CHAR_BUDGET,
        legacy_fallback_limit: int = DEFAULT_LEGACY_FALLBACK_LIMIT,
    ) -> None:
        default_dir = Path(__file__).resolve().parents[2] / "review_skills"
        self.skills_dir = Path(skills_dir).resolve() if skills_dir else default_dir
        self.max_chars = max(1, int(max_chars))
        self.legacy_fallback_limit = max(0, int(legacy_fallback_limit))

    @property
    def core_path(self) -> Path:
        return self.skills_dir / "core.md"

    @property
    def learned_path(self) -> Path:
        return self.skills_dir / "learned.jsonl"

    def load_core(self) -> str:
        try:
            return self.core_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""

    def load_skills(self) -> list[ReviewSkill]:
        try:
            lines = self.learned_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []
        skills: list[ReviewSkill] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                skill = ReviewSkill.from_record(json.loads(line))
            except json.JSONDecodeError:
                continue
            if skill is not None:
                skills.append(skill)
        return skills

    def load_active_skills(self) -> list[ReviewSkill]:
        """Return valid active records in their stable legacy file order."""

        return [skill for skill in self.load_skills() if skill.status == "active"]

    def render(self) -> str:
        """Preserve the legacy sequential prompt contract."""

        available = max(0, self.max_chars - len(_PREFIX) - len(_SUFFIX))
        body = self.load_core()[:available].rstrip()
        for skill in self.load_active_skills():
            candidate = _append_skill(body, self._render_skill(skill))
            if len(candidate) > available:
                break
            body = candidate
        return f"{_PREFIX}{body}{_SUFFIX}"

    def select_sequential(self) -> SkillSelection:
        """Represent legacy loading as a selection for explicit runtime injection."""

        skills = self.load_skills()
        active = [skill for skill in skills if skill.status == "active"]
        return self._pack(
            [SkillMatch(skill, 0, ("legacy_sequential",)) for skill in active],
            skills=skills,
            top_k=len(active),
            stop_on_budget=True,
        )

    def core_only(self) -> SkillSelection:
        """Return a validated Core-only selection for fail-safe runtime fallback."""

        return self._pack([], skills=self.load_skills(), top_k=0)

    def retrieve(
        self, query: SkillQuery, *, top_k: int = DEFAULT_SKILL_TOP_K
    ) -> SkillSelection:
        """Filter, rank, and atomically pack active skills deterministically."""

        skills = self.load_skills()
        matches: list[SkillMatch] = []
        skipped: list[tuple[str, str]] = []
        legacy: list[SkillMatch] = []
        for skill in skills:
            if skill.status != "active":
                skipped.append((skill.id, "status"))
                continue
            if skill.metadata_warnings:
                skipped.append((skill.id, "malformed_metadata"))
                continue
            match, reason = _match_skill(skill, query)
            if match is None:
                skipped.append((skill.id, reason))
            elif skill.scoped:
                matches.append(match)
            else:
                legacy.append(match)
        matches.sort(key=lambda item: (-item.score, item.skill.id))
        legacy.sort(key=lambda item: item.skill.id)
        legacy_only_fallback = not matches and bool(legacy)
        if matches:
            matches.extend(legacy[: self.legacy_fallback_limit])
            skipped.extend(
                (item.skill.id, "legacy_fallback_limit")
                for item in legacy[self.legacy_fallback_limit :]
            )
        else:
            matches.extend(legacy[: self.legacy_fallback_limit])
            skipped.extend(
                (item.skill.id, "legacy_fallback_limit")
                for item in legacy[self.legacy_fallback_limit :]
            )
        selection = self._pack(matches, skills=skills, top_k=max(0, int(top_k)))
        return SkillSelection(
            context=selection.context,
            selected=selection.selected,
            skipped=tuple(skipped) + selection.skipped,
            core_chars=selection.core_chars,
            learned_chars=selection.learned_chars,
            total_chars=selection.total_chars,
            estimated_tokens=selection.estimated_tokens,
            bank_digest=selection.bank_digest,
            total_records=selection.total_records,
            active_records=selection.active_records,
            scoped_active_records=selection.scoped_active_records,
            unscoped_active_records=selection.unscoped_active_records,
            candidate_count=selection.candidate_count,
            malformed_active_records=selection.malformed_active_records,
            legacy_only_fallback=legacy_only_fallback,
        )

    def bank_digest(self, skills: Sequence[ReviewSkill] | None = None) -> str:
        bank_skills = self.load_skills() if skills is None else skills
        payload = {
            "core": self.load_core(),
            "records": [
                skill.to_record()
                for skill in sorted(bank_skills, key=lambda item: item.id)
            ],
        }
        canonical = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _pack(
        self,
        matches: Sequence[SkillMatch],
        *,
        skills: Sequence[ReviewSkill],
        top_k: int,
        stop_on_budget: bool = False,
    ) -> SkillSelection:
        core = self.load_core()
        body_budget = self.max_chars - len(_PREFIX) - len(_SUFFIX)
        if len(core) > body_budget:
            raise ValueError("review skill Core exceeds the configured hard char budget")
        body = core
        selected: list[SkillMatch] = []
        skipped: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            if len(selected) >= top_k:
                skipped.append((match.skill.id, "top_k"))
                continue
            candidate = _append_skill(body, self._render_skill(match.skill))
            if len(candidate) > body_budget:
                skipped.append((match.skill.id, "budget"))
                if stop_on_budget:
                    skipped.extend(
                        (remaining.skill.id, "after_budget_break")
                        for remaining in matches[index + 1 :]
                    )
                    break
                continue
            body = candidate
            selected.append(match)
        context = f"{_PREFIX}{body}{_SUFFIX}"
        learned_chars = max(0, len(body) - len(core) - (2 if core and selected else 0))
        active = [skill for skill in skills if skill.status == "active"]
        valid_active = [skill for skill in active if not skill.metadata_warnings]
        return SkillSelection(
            context=context,
            selected=tuple(selected),
            skipped=tuple(skipped),
            core_chars=len(core),
            learned_chars=learned_chars,
            total_chars=len(context),
            estimated_tokens=max(1, (len(context) + 3) // 4),
            bank_digest=self.bank_digest(skills),
            total_records=len(skills),
            active_records=len(active),
            scoped_active_records=sum(skill.scoped for skill in valid_active),
            unscoped_active_records=sum(not skill.scoped for skill in valid_active),
            candidate_count=sum(skill.status == "candidate" for skill in skills),
            malformed_active_records=sum(
                bool(skill.metadata_warnings) for skill in active
            ),
        )

    def load_context(self) -> str:
        return self.render()

    @staticmethod
    def _render_skill(skill: ReviewSkill) -> str:
        return f"- [{skill.category}] {skill.principle}\n  Why: {skill.why}"


def _match_skill(skill: ReviewSkill, query: SkillQuery) -> tuple[SkillMatch | None, str]:
    query_languages = set(query.languages)
    if skill.languages and not query_languages.intersection(skill.languages):
        return None, "language_mismatch"
    matching_globs = [
        pattern
        for pattern in skill.path_globs
        if any(_path_matches(path, pattern) for path in query.changed_files)
    ]
    if skill.path_globs and not matching_globs:
        return None, "path_mismatch"
    graph_corpus = " ".join(
        (*query.changed_symbols, *query.change_kinds, *query.graph_edge_kinds)
    ).lower()
    lexical_hits = [
        trigger for trigger in skill.triggers if _literal_hit(trigger, query.lexical_corpus)
    ]
    graph_hits = [trigger for trigger in skill.triggers if _literal_hit(trigger, graph_corpus)]
    if skill.triggers and not (lexical_hits or graph_hits):
        return None, "trigger_mismatch"
    score = 0
    reasons: list[str] = []
    if lexical_hits:
        score += TRIGGER_SCORE
        reasons.append(f"trigger:{lexical_hits[0]}")
    if matching_globs:
        score += PATH_SCORE
        reasons.append(
            f"path:{sorted(matching_globs, key=lambda item: (-len(item), item))[0]}"
        )
    language_hits = sorted(query_languages.intersection(skill.languages))
    if language_hits:
        score += LANGUAGE_SCORE
        reasons.append(f"language:{language_hits[0]}")
    if graph_hits:
        score += GRAPH_SCORE
        reasons.append(f"graph:{graph_hits[0]}")
    if not skill.scoped:
        score = LEGACY_SCORE
        reasons.append("unscoped_legacy")
    return SkillMatch(skill=skill, score=score, reasons=tuple(reasons)), ""


def _path_matches(path: str, pattern: str) -> bool:
    normalized = path.replace("\\", "/")
    glob = pattern.replace("\\", "/")
    variants = {glob}
    pending = [glob]
    while pending:
        candidate = pending.pop()
        marker = candidate.find("**/")
        if marker < 0:
            continue
        collapsed = candidate[:marker] + candidate[marker + 3 :]
        if collapsed not in variants:
            variants.add(collapsed)
            pending.append(collapsed)
    return any(fnmatch.fnmatchcase(normalized, candidate) for candidate in variants)


def _literal_hit(trigger: str, corpus: str) -> bool:
    if not trigger or not corpus:
        return False
    if re.fullmatch(r"[a-z0-9_]+", trigger):
        return re.search(
            rf"(?<![a-z0-9_]){re.escape(trigger)}(?![a-z0-9_])", corpus
        ) is not None
    return trigger in corpus


def _append_skill(body: str, rendered: str) -> str:
    return f"{body}\n\n{rendered}" if body else rendered


def _mapping_list(value: Any) -> Iterable[Mapping[str, Any]]:
    if not isinstance(value, list):
        return ()
    return (item for item in value if isinstance(item, Mapping))


def _collect_string(target: set[str], value: Any) -> None:
    if isinstance(value, str) and value.strip():
        target.add(value.strip().lower())


def review_skill_context(
    skills_dir: Path | str | None = None,
    *,
    max_chars: int = DEFAULT_SKILL_CHAR_BUDGET,
) -> str:
    return ReviewSkillLoader(skills_dir, max_chars=max_chars).render()
