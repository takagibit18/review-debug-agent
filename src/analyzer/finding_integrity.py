"""Thin, run-scoped integrity checks for reviewer findings.

The reviewer owns the engineering judgment behind a finding.  This module only
checks that the resulting finding is structurally usable and that its cited
locations can be tied to code the reviewer actually received during this run.
It intentionally does not decide whether the reported behavior is a bug.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.analyzer.diff_lines import changed_new_lines_by_file
from src.analyzer.evidence_binding import bind_candidate_evidence, bind_issue_candidate_id
from src.analyzer.finding_schema import normalize_repo_path
from src.analyzer.location import LocationParseResult, normalize_location
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.schemas import FindingCandidate, ReviewRequest
from src.analyzer.verifier_context import (
    build_candidate_verifier_context,
    location_in_candidate_context,
    provenance_in_candidate_context,
)

_RISK_SEVERITIES = {Severity.CRITICAL, Severity.WARNING}


def build_candidates(
    report: ReviewReport,
    *,
    iteration: int,
) -> list[FindingCandidate]:
    """Build stable risk candidates for the integrity stage."""

    candidates: list[FindingCandidate] = []
    seen: set[str] = set()
    for source_issue_index, issue in enumerate(report.issues):
        if issue.severity not in _RISK_SEVERITIES:
            continue
        candidate_id = _candidate_id(issue)
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        bound_issue = bind_issue_candidate_id(issue, candidate_id)
        issue.candidate_id = candidate_id
        for evidence in issue.all_evidence():
            evidence.candidate_id = candidate_id
        if not issue.finding_id:
            issue.finding_id = "F-" + candidate_id[:12].upper()
        bound_issue.finding_id = issue.finding_id
        candidates.append(
            FindingCandidate(
                candidate_id=candidate_id,
                issue=bound_issue,
                claim=issue.suggestion.strip(),
                evidence_locations=(
                    [issue.location] if issue.location.strip() else []
                ),
                originating_iteration=max(0, iteration),
                source_issue_index=source_issue_index,
            )
        )
    return candidates


def _candidate_id(issue: ReviewIssue) -> str:
    normalized = "\n".join(
        (
            issue.severity.value,
            issue.location.strip().replace("\\", "/"),
            issue.evidence.strip(),
            issue.suggestion.strip(),
        )
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class IntegrityFailure:
    """One objective integrity failure for a finding."""

    code: str
    message: str
    field: str = ""
    location: str = ""
    file: str = ""
    start_line: int | None = None
    end_line: int | None = None
    retrieval_source: str = ""
    context_manifest_id: str = ""
    manifest_hash_prefix: str = ""

    def as_detail(self) -> dict[str, Any]:
        """Return a stable structured representation for event logs and reports."""

        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "location": self.location,
            "file": self.file,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "retrieval_source": self.retrieval_source,
            "context_manifest_id": self.context_manifest_id,
            "manifest_hash_prefix": self.manifest_hash_prefix,
        }


@dataclass(frozen=True)
class FindingIntegrityResult:
    """Integrity result for one candidate."""

    candidate_id: str
    passed: bool
    failures: tuple[IntegrityFailure, ...] = ()


@dataclass(frozen=True)
class IntegrityGuardResult:
    """Batch result returned by :class:`FindingIntegrityGuard`."""

    results: tuple[FindingIntegrityResult, ...] = ()
    bound_candidates: tuple[FindingCandidate, ...] = ()

    @property
    def checked_count(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(item.passed for item in self.results)

    @property
    def rejected_count(self) -> int:
        return self.checked_count - self.passed_count

    @property
    def accepted_candidate_ids(self) -> frozenset[str]:
        return frozenset(item.candidate_id for item in self.results if item.passed)

    @property
    def rejected_candidate_ids(self) -> frozenset[str]:
        return frozenset(item.candidate_id for item in self.results if not item.passed)

    @property
    def failures(self) -> dict[str, tuple[IntegrityFailure, ...]]:
        return {
            item.candidate_id: item.failures
            for item in self.results
            if item.failures
        }

    @property
    def failure_details(self) -> dict[str, list[dict[str, Any]]]:
        """Return detailed failures keyed by candidate while preserving code views."""

        return {
            candidate_id: [failure.as_detail() for failure in failures]
            for candidate_id, failures in self.failures.items()
        }


class FindingIntegrityGuard:
    """Check finding integrity without re-evaluating the reported bug."""

    def __init__(self, repo_root: Path | str | None = None) -> None:
        self._repo_root = Path(repo_root).resolve() if repo_root is not None else None

    def validate(
        self,
        candidates: list[FindingCandidate],
        request: ReviewRequest,
        *,
        tool_evidence: list[dict[str, Any]] | None = None,
        context_manifests: list[dict[str, Any]] | None = None,
        candidate_context: list[dict[str, Any]] | None = None,
        context_mode: str = "graph_hybrid",
    ) -> IntegrityGuardResult:
        """Validate candidates against repository and retained run context."""

        if not candidates:
            return IntegrityGuardResult()

        evidence = tool_evidence or []
        manifests = context_manifests or []
        binding_failures = {
            candidate.candidate_id: self._candidate_binding_failures(candidate)
            for candidate in candidates
        }
        bound_candidates = bind_candidate_evidence(
            candidates,
            request,
            evidence,
            context_manifests=manifests,
        )
        contexts = (
            candidate_context
            if candidate_context is not None
            else build_candidate_verifier_context(
                bound_candidates,
                request,
                evidence,
                context_manifests=manifests,
                context_mode=context_mode,
            )
        )
        contexts_by_id = {
            str(item.get("candidate_id", "")): item
            for item in contexts
            if isinstance(item, dict)
        }
        changed = changed_new_lines_by_file(request.diff_text or "")
        results = tuple(
            self._validate_candidate(
                candidate,
                request=request,
                repo_root=self._root_for(request),
                changed=changed,
                context=contexts_by_id.get(candidate.candidate_id),
                initial_failures=binding_failures.get(candidate.candidate_id, ()),
            )
            for candidate in bound_candidates
        )
        return IntegrityGuardResult(
            results=results,
            bound_candidates=tuple(bound_candidates),
        )

    def _validate_candidate(
        self,
        candidate: FindingCandidate,
        *,
        request: ReviewRequest,
        repo_root: Path,
        changed: dict[str, set[int]],
        context: dict[str, Any] | None,
        initial_failures: tuple[IntegrityFailure, ...],
    ) -> FindingIntegrityResult:
        issue = candidate.issue
        failures: list[IntegrityFailure] = list(initial_failures)
        candidate_id = str(candidate.candidate_id or "").strip()

        if not candidate_id:
            failures.append(
                IntegrityFailure(
                    "candidate_binding_missing",
                    "Finding candidate id is empty.",
                    field="candidate_id",
                )
            )
        if not isinstance(issue, ReviewIssue):
            failures.append(
                IntegrityFailure(
                    "finding_structure_invalid",
                    "Finding could not be parsed as a ReviewIssue.",
                    field="issue",
                )
            )
            return FindingIntegrityResult(candidate_id, False, tuple(failures))

        if issue.candidate_id and issue.candidate_id != candidate_id:
            failures.append(
                IntegrityFailure(
                    "candidate_binding_mismatch",
                    "Finding candidate_id does not match the runtime candidate.",
                    field="candidate_id",
                )
            )
        for evidence in issue.all_evidence():
            if evidence.candidate_id and evidence.candidate_id != candidate_id:
                failures.append(
                    IntegrityFailure(
                        "candidate_binding_mismatch",
                        "Evidence candidate_id does not match its finding candidate.",
                        field="evidence.candidate_id",
                        location=evidence.location,
                        **_evidence_failure_metadata(evidence),
                    )
                )

        display = normalize_location(issue.location)
        failures.extend(
            self._validate_location(
                display,
                repo_root=repo_root,
                field="location",
                require_line=issue.severity in _RISK_SEVERITIES,
            )
        )
        if display.valid and display.line is not None:
            failures.extend(
                self._observed_failures(
                    display,
                    context=context,
                    changed=changed,
                    field="location",
                )
            )

        if issue.primary_anchor is not None:
            anchor_location = normalize_location(issue.primary_anchor.location)
            failures.extend(
                self._validate_location(
                    anchor_location,
                    repo_root=repo_root,
                    field="primary_anchor",
                    require_line=True,
                )
            )
            if anchor_location.valid:
                failures.extend(
                    self._observed_failures(
                        anchor_location,
                        context=context,
                        changed=changed,
                        field="primary_anchor",
                    )
                )

        for index, related in enumerate(issue.related_locations):
            related_location = normalize_location(related.location)
            field = f"related_locations[{index}]"
            failures.extend(
                self._validate_location(
                    related_location,
                    repo_root=repo_root,
                    field=field,
                    require_line=True,
                )
            )
            if related_location.valid:
                failures.extend(
                    self._observed_failures(
                        related_location,
                        context=context,
                        changed=changed,
                        field=field,
                    )
                )

        for role, evidence_items in (
            ("cause", issue.cause_evidence),
            ("contract", issue.contract_evidence),
            ("trigger", issue.trigger_evidence),
            ("impact", issue.impact_evidence),
        ):
            for index, evidence_item in enumerate(evidence_items):
                field = f"{role}_evidence[{index}]"
                evidence_location = self._evidence_location(evidence_item)
                failures.extend(
                    _decorate_failures(
                        self._validate_location(
                            evidence_location,
                            repo_root=repo_root,
                            field=field,
                            require_line=True,
                        ),
                        evidence=evidence_item,
                    )
                )
                if not evidence_item.retrieval_source:
                    failures.append(
                        IntegrityFailure(
                            "evidence_binding_missing",
                            "Evidence does not identify a retrieval source.",
                            field=f"{field}.retrieval_source",
                            location=evidence_item.location,
                            **_evidence_failure_metadata(evidence_item),
                        )
                    )
                if not evidence_item.statement.strip():
                    failures.append(
                        IntegrityFailure(
                            "evidence_binding_missing",
                            "Evidence has no statement tying the location to the finding.",
                            field=f"{field}.statement",
                            location=evidence_item.location,
                            **_evidence_failure_metadata(evidence_item),
                        )
                    )
                if evidence_location.valid:
                    if not provenance_in_candidate_context(context, evidence_item):
                        failures.append(
                            IntegrityFailure(
                                "evidence_not_observed",
                                "Evidence provenance is not present in retained reviewer context.",
                                field=field,
                                location=evidence_item.location,
                                **_evidence_failure_metadata(evidence_item),
                            )
                        )

        if issue.severity in _RISK_SEVERITIES and self._requires_changed_anchor(
            request, changed
        ):
            if not self._has_changed_anchor(candidate, issue, changed):
                failures.append(
                    IntegrityFailure(
                        "changed_anchor_missing",
                        "Risk finding has no location on a line changed by this PR.",
                        field="changed_anchor",
                    )
                )

        unique_failures = tuple(
            dict.fromkeys(failures)
        )
        return FindingIntegrityResult(candidate_id, not unique_failures, unique_failures)

    @staticmethod
    def _candidate_binding_failures(
        candidate: FindingCandidate,
    ) -> tuple[IntegrityFailure, ...]:
        candidate_id = str(candidate.candidate_id or "").strip()
        failures: list[IntegrityFailure] = []
        issue = candidate.issue
        if not candidate_id:
            return ()
        if isinstance(issue, ReviewIssue):
            if issue.candidate_id and issue.candidate_id != candidate_id:
                failures.append(
                    IntegrityFailure(
                        "candidate_binding_mismatch",
                        "Finding candidate_id does not match the runtime candidate.",
                        field="candidate_id",
                    )
                )
            for evidence in issue.all_evidence():
                if evidence.candidate_id and evidence.candidate_id != candidate_id:
                    failures.append(
                        IntegrityFailure(
                            "candidate_binding_mismatch",
                            "Evidence candidate_id does not match its finding candidate.",
                            field="evidence.candidate_id",
                            location=evidence.location,
                            **_evidence_failure_metadata(evidence),
                        )
                    )
        return tuple(failures)

    @staticmethod
    def _requires_changed_anchor(
        request: ReviewRequest, changed: dict[str, set[int]]
    ) -> bool:
        """Require PR anchoring in diff reviews, while preserving full-repo reviews."""

        return request.diff_mode or bool(changed)

    @staticmethod
    def _has_changed_anchor(
        candidate: FindingCandidate,
        issue: ReviewIssue,
        changed: dict[str, set[int]],
    ) -> bool:
        locations: list[LocationParseResult] = [normalize_location(issue.location)]
        if issue.primary_anchor is not None:
            locations.append(normalize_location(issue.primary_anchor.location))
        locations.extend(
            normalize_location(item.location) for item in issue.related_locations
        )
        locations.extend(
            normalize_location(item.location) for item in issue.all_evidence()
        )
        locations.extend(normalize_location(item) for item in candidate.evidence_locations)
        return any(
            location.valid
            and location.path in changed
            and location.line is not None
            and any(
                line in changed[location.path]
                for line in range(
                    location.line, (location.end_line or location.line) + 1
                )
            )
            for location in locations
        )

    @staticmethod
    def _evidence_location(evidence: Any) -> LocationParseResult:
        file = normalize_repo_path(getattr(evidence, "file", ""))
        line = getattr(evidence, "line", None)
        end_line = getattr(evidence, "end_line", None)
        if not file or line is None:
            return normalize_location("")
        suffix = str(line)
        if end_line is not None:
            suffix += f"-{end_line}"
        return normalize_location(f"{file}:{suffix}")

    @staticmethod
    def _validate_location(
        location: LocationParseResult,
        *,
        repo_root: Path,
        field: str,
        require_line: bool,
    ) -> list[IntegrityFailure]:
        if not location.valid or not location.path:
            return [
                IntegrityFailure(
                    "location_invalid",
                    location.warning or "Location is not a valid repository-relative path.",
                    field=field,
                    location=location.raw,
                )
            ]
        if location.line is None:
            if require_line:
                return [
                    IntegrityFailure(
                        "location_line_missing",
                        "Risk finding location must identify a source line.",
                        field=field,
                        location=location.canonical,
                    )
                ]
            return FindingIntegrityGuard._validate_repo_path(
                repo_root, location.path, field=field, location=location.canonical
            )

        failures = FindingIntegrityGuard._validate_repo_path(
            repo_root, location.path, field=field, location=location.canonical
        )
        if failures:
            return failures
        path = FindingIntegrityGuard._resolve_repo_path(repo_root, location.path)
        if path is None or not path.is_file():
            return failures
        try:
            line_count = _line_count(path)
        except OSError:
            return [
                IntegrityFailure(
                    "location_unreadable",
                    "Referenced repository file could not be read.",
                    field=field,
                    location=location.canonical,
                )
            ]
        end_line = location.end_line or location.line
        if end_line > line_count:
            return [
                IntegrityFailure(
                    "location_line_out_of_range",
                    f"Referenced line range exceeds the file's {line_count} lines.",
                    field=field,
                    location=location.canonical,
                )
            ]
        return failures

    @staticmethod
    def _validate_repo_path(
        repo_root: Path,
        relative_path: str,
        *,
        field: str,
        location: str,
    ) -> list[IntegrityFailure]:
        resolved = FindingIntegrityGuard._resolve_repo_path(repo_root, relative_path)
        if resolved is None:
            return [
                IntegrityFailure(
                    "repository_path_invalid",
                    "Referenced path escapes the repository root.",
                    field=field,
                    location=location,
                )
            ]
        if not resolved.exists() or not resolved.is_file():
            return [
                IntegrityFailure(
                    "repository_path_missing",
                    "Referenced repository file does not exist.",
                    field=field,
                    location=location,
                )
            ]
        return []

    @staticmethod
    def _resolve_repo_path(repo_root: Path, relative_path: str) -> Path | None:
        normalized = normalize_repo_path(relative_path)
        if not normalized or normalized.startswith("/"):
            return None
        try:
            resolved = (repo_root / Path(*normalized.split("/"))).resolve()
            if not resolved.is_relative_to(repo_root.resolve()):
                return None
        except (OSError, ValueError):
            return None
        return resolved

    @staticmethod
    def _observed_failures(
        location: LocationParseResult,
        *,
        context: dict[str, Any] | None,
        changed: dict[str, set[int]],
        field: str,
    ) -> list[IntegrityFailure]:
        if not location.valid or location.line is None:
            return []
        if _location_intersects_changed_lines(location, changed):
            return []
        if location_in_candidate_context(context, location):
            return []
        return [
            IntegrityFailure(
                "evidence_not_observed",
                "Referenced code was not present in retained reviewer context.",
                field=field,
                location=location.canonical,
                **_location_failure_metadata(location),
            )
        ]

    def _root_for(self, request: ReviewRequest) -> Path:
        return self._repo_root or Path(request.repo_path).resolve()


def _evidence_failure_metadata(evidence: Any) -> dict[str, Any]:
    """Extract safe provenance fields for structured failure telemetry."""

    digest = str(getattr(evidence, "context_hash", "") or "").strip()
    file = normalize_repo_path(str(getattr(evidence, "file", "") or ""))
    start_line = getattr(evidence, "line", None)
    end_line = getattr(evidence, "end_line", None) or start_line
    return {
        "file": file,
        "start_line": start_line if isinstance(start_line, int) else None,
        "end_line": end_line if isinstance(end_line, int) else None,
        "retrieval_source": str(
            getattr(evidence, "retrieval_source", "") or ""
        ).strip(),
        "context_manifest_id": str(
            getattr(evidence, "context_manifest_id", "") or ""
        ).strip(),
        "manifest_hash_prefix": digest[:12],
    }


def _location_failure_metadata(location: LocationParseResult) -> dict[str, Any]:
    """Extract structured location fields for non-evidence failures."""

    return {
        "file": location.path or "",
        "start_line": location.line,
        "end_line": location.end_line or location.line,
    }


def _decorate_failures(
    failures: list[IntegrityFailure],
    *,
    evidence: Any,
) -> list[IntegrityFailure]:
    """Fill structured evidence metadata without changing failure identity."""

    metadata = _evidence_failure_metadata(evidence)
    decorated: list[IntegrityFailure] = []
    for failure in failures:
        updates = {
            key: value
            for key, value in metadata.items()
            if not getattr(failure, key)
            and value not in ("", None)
        }
        decorated.append(replace(failure, **updates) if updates else failure)
    return decorated


def _location_intersects_changed_lines(
    location: LocationParseResult,
    changed: dict[str, set[int]],
) -> bool:
    if not location.valid or not location.path or location.line is None:
        return False
    end_line = location.end_line or location.line
    return any(
        line in changed.get(location.path, set())
        for line in range(location.line, end_line + 1)
    )


def _line_count(path: Path) -> int:
    data = path.read_bytes()
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
