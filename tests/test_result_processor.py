"""Tests for result processor behavior."""

from src.analyzer.context_state import ContextState
from src.analyzer.schemas import AnalysisPlan
from src.analyzer.output_formatter import (
    ReviewIssue,
    ReviewReport,
    Severity,
    triage_review_report,
)
from src.analyzer.result_processor import ResultProcessor


def test_merge_review_reports_sorts_by_severity_priority() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(severity=Severity.INFO, location="a:1", evidence="x", suggestion="x"),
            ReviewIssue(
                severity=Severity.WARNING,
                location="b:1",
                evidence="+ cache.clear() now runs on every request",
                suggestion="x",
                confidence=0.9,
            ),
            ReviewIssue(severity=Severity.STYLE, location="c:1", evidence="x", suggestion="x"),
            ReviewIssue(
                severity=Severity.CRITICAL,
                location="d:1",
                evidence="@@ -1,1 +1,1 @@\n- allow_all = True\n+ allow_all = is_admin",
                suggestion="x",
                confidence=0.9,
            ),
        ],
    )

    merged = ResultProcessor.merge_review_reports([report])
    order = [issue.severity for issue in merged.issues]
    assert order == [Severity.CRITICAL, Severity.WARNING, Severity.INFO, Severity.STYLE]


def test_merge_review_reports_keeps_high_confidence_warning_without_diff_evidence() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(
                severity=Severity.CRITICAL,
                location="src/auth.py:14",
                evidence="Authorization logic looks risky after this change.",
                suggestion="Restore the authorization guard.",
                confidence=1.0,
            ),
            ReviewIssue(
                severity=Severity.WARNING,
                location="src/cache.py:8",
                evidence="Cache invalidation may be too broad.",
                suggestion="Guard the invalidation.",
                confidence=0.95,
            ),
            ReviewIssue(
                severity=Severity.INFO,
                location="src/logging.py:3",
                evidence="Consider reducing noisy debug logs.",
                suggestion="Use trace logging instead.",
                confidence=0.7,
            ),
            ReviewIssue(
                severity=Severity.STYLE,
                location="src/style.py:1",
                evidence="Spacing is inconsistent.",
                suggestion="Normalize whitespace.",
                confidence=0.7,
            ),
        ],
    )

    merged = ResultProcessor.merge_review_reports([report])

    assert [(issue.severity, issue.location) for issue in merged.issues] == [
        (Severity.WARNING, "src/cache.py:8"),
        (Severity.INFO, "src/logging.py:3"),
        (Severity.STYLE, "src/style.py:1"),
    ]


def test_merge_review_reports_keeps_bug_findings_with_diff_evidence() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(
                severity=Severity.CRITICAL,
                location="src/auth.py:14",
                evidence="```diff\n- return True\n+ return user.is_admin\n```",
                suggestion="Restore the authorization guard.",
                confidence=0.95,
            ),
            ReviewIssue(
                severity=Severity.WARNING,
                location="src/cache.py:8",
                evidence="diff --git a/src/cache.py b/src/cache.py\n+ cache.clear()",
                suggestion="Guard the invalidation.",
                confidence=0.9,
            ),
            ReviewIssue(
                severity=Severity.WARNING,
                location="src/jobs.py:3",
                evidence="+ run_all_jobs()",
                suggestion="Keep the job filter.",
                confidence=0.9,
            ),
        ],
    )

    merged = ResultProcessor.merge_review_reports([report])

    assert [issue.location for issue in merged.issues] == [
        "src/auth.py:14",
        "src/cache.py:8",
        "src/jobs.py:3",
    ]


def test_merge_review_reports_keeps_high_confidence_critical_with_code_evidence() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(
                severity=Severity.CRITICAL,
                location="src/_pytest/fixtures.py:246-249",
                evidence=(
                    "def __eq__(self, other: object) -> bool:\n"
                    "    try:\n"
                    "        res = self.obj == other\n"
                    "        return bool(res)\n"
                    "    except Exception:\n"
                    "        return id(self.obj) == id(other)"
                ),
                suggestion="Compare against other.obj when other is a wrapper.",
                confidence=0.95,
            )
        ],
    )

    merged = ResultProcessor.merge_review_reports([report])

    assert [issue.location for issue in merged.issues] == [
        "src/_pytest/fixtures.py:246-249",
    ]


def test_merge_review_reports_keeps_high_confidence_critical_with_single_line_code_evidence() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(
                severity=Severity.CRITICAL,
                location="src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs:77",
                evidence="using IDisposable region = _gcKeeper.TryStartNoGCRegion();",
                suggestion="Guard this runtime-sensitive path.",
                confidence=0.85,
            )
        ],
    )

    merged = ResultProcessor.merge_review_reports([report])

    assert [issue.location for issue in merged.issues] == [
        "src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs:77",
    ]


def test_merge_review_reports_keeps_critical_with_inline_backtick_code_evidence() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(
                severity=Severity.CRITICAL,
                location="src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs:24",
                evidence=(
                    "The diff adds `private readonly GCKeeper _gcKeeper;` but no "
                    "constructor assignment. When `_gcKeeper.TryStartNoGCRegion()` "
                    "runs at line 80, it will throw NullReferenceException."
                ),
                suggestion="Assign _gcKeeper before using it in NewPayload.",
                confidence=0.95,
            )
        ],
    )

    merged = ResultProcessor.merge_review_reports([report])

    assert [issue.location for issue in merged.issues] == [
        "src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs:24",
    ]


def test_merge_review_reports_filters_low_confidence_warning_and_critical() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(
                severity=Severity.CRITICAL,
                location="src/auth.py:10",
                evidence="@@ -1,1 +1,1 @@\n- return True\n+ return user.is_admin",
                suggestion="Restore guard.",
                confidence=0.8,
            ),
            ReviewIssue(
                severity=Severity.WARNING,
                location="src/cache.py:8",
                evidence="+ cache.clear()",
                suggestion="Narrow invalidation.",
                confidence=0.84,
            ),
            ReviewIssue(
                severity=Severity.INFO,
                location="src/logging.py:1",
                evidence="FYI",
                suggestion="Optional improvement.",
                confidence=0.2,
            ),
        ],
    )
    merged = ResultProcessor.merge_review_reports([report])
    assert [(issue.severity, issue.location) for issue in merged.issues] == [
        (Severity.INFO, "src/logging.py:1"),
    ]


def test_merge_review_reports_keeps_specific_risk_warning_below_standard_threshold() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(
                severity=Severity.WARNING,
                location="src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs:24",
                evidence=(
                    "private readonly GCKeeper _gcKeeper; is declared, but line 80 calls "
                    "using IDisposable region = _gcKeeper.TryStartNoGCRegion();. "
                    "If _gcKeeper is not initialized this throws NullReferenceException."
                ),
                suggestion="Initialize _gcKeeper before calling TryStartNoGCRegion.",
                confidence=0.70,
            ),
            ReviewIssue(
                severity=Severity.WARNING,
                location="src/_pytest/python.py:1200",
                evidence=(
                    "if modified_val is None or len(modified_val) > 100:\n"
                    "        return str(argname) + str(idx)"
                ),
                suggestion=(
                    "The 100-character hard limit is a user-visible behavior change for "
                    "long parameter IDs."
                ),
                confidence=0.80,
            ),
        ],
    )

    merged = ResultProcessor.merge_review_reports([report])

    assert [issue.location for issue in merged.issues] == [
        "src/Nethermind/Nethermind.Merge.Plugin/EngineRpcModule.Paris.cs:24",
        "src/_pytest/python.py:1200",
    ]


def test_merge_review_reports_keeps_compatibility_warning_at_relaxed_threshold() -> None:
    report = ReviewReport(
        summary="summary",
        issues=[
            ReviewIssue(
                severity=Severity.WARNING,
                location="src/_pytest/python.py:1195-1198",
                evidence=(
                    "+    if modified_val is None or len(modified_val) > 100:\n"
                    "+        return str(argname) + str(idx)\n"
                    "+    return modified_val"
                ),
                suggestion=(
                    "Any parameter ID over 100 chars will be replaced with "
                    "auto-generated IDs."
                ),
                confidence=0.70,
            ),
            ReviewIssue(
                severity=Severity.WARNING,
                location="src/_pytest/python.py:1200",
                evidence=(
                    "if modified_val is None or len(modified_val) > 100:\n"
                    "        return str(argname) + str(idx)"
                ),
                suggestion=(
                    "The limit silently truncates long parameter IDs and can "
                    "break test selection scripts."
                ),
                confidence=0.75,
            ),
        ],
    )

    merged = ResultProcessor.merge_review_reports([report])

    assert [issue.location for issue in merged.issues] == [
        "src/_pytest/python.py:1195-1198",
        "src/_pytest/python.py:1200",
    ]


def test_result_processor_budget_from_constructor() -> None:
    processor = ResultProcessor(token_budget=10)
    assert processor.is_budget_exhausted(9) is False
    assert processor.is_budget_exhausted(10) is True
    assert ContextState() is not None


def test_result_processor_uses_explicit_hard_budget() -> None:
    processor = ResultProcessor(token_budget=10, token_hard_budget=15)

    assert processor.budget_state(9) == "none"
    assert processor.budget_state(10) == "soft_capped"
    assert processor.budget_state(14) == "soft_capped"
    assert processor.budget_state(15) == "hard_capped"


def test_triage_review_report_separates_must_fix_bugs_and_optimizations() -> None:
    report = ReviewReport(
        summary="triaged",
        issues=[
            ReviewIssue(
                severity=Severity.CRITICAL,
                location="src/app.py:10",
                evidence="@@ -10,1 +10,1 @@\n- allow_all = True\n+ allow_all = is_admin(user)",
                suggestion="Restore the access check before merging.",
                confidence=0.97,
            ),
            ReviewIssue(
                severity=Severity.CRITICAL,
                location="src/app.py:22",
                evidence="Authorization logic looks risky after this change.",
                suggestion="Double-check the branch conditions.",
                confidence=0.99,
            ),
            ReviewIssue(
                severity=Severity.WARNING,
                location="src/cache.py:8",
                evidence="+ cache.clear() now runs on every request",
                suggestion="Guard the cache clear behind a narrower condition.",
                confidence=0.91,
            ),
            ReviewIssue(
                severity=Severity.INFO,
                location="src/logging.py:3",
                evidence="+ logger.debug('payload=%s', payload)",
                suggestion="Consider reducing noisy debug logging in hot paths.",
                confidence=0.80,
            ),
            ReviewIssue(
                severity=Severity.STYLE,
                location="src/logging.py:5",
                evidence="+ return  x",
                suggestion="Normalize whitespace.",
                confidence=0.75,
            ),
        ],
    )

    triage = triage_review_report(report)

    assert [issue.location for issue in triage.must_fix_critical] == ["src/app.py:10"]
    assert [issue.location for issue in triage.other_bug_findings] == [
        "src/app.py:22",
        "src/cache.py:8",
    ]
    assert [issue.location for issue in triage.optimization_suggestions] == [
        "src/logging.py:3",
        "src/logging.py:5",
    ]


def test_format_review_keeps_review_response_contract_shape() -> None:
    processor = ResultProcessor()
    plan = AnalysisPlan(
        draft_review=ReviewReport(
            summary="found one critical regression",
            issues=[
                ReviewIssue(
                    severity=Severity.CRITICAL,
                    location="src/auth.py:14",
                    evidence="+ if user.is_admin:\n+     return True",
                    suggestion="Restore the original authorization guard.",
                    confidence=0.90,
                )
            ],
        )
    )

    response, blocking_error = processor.format_review(plan, [], ContextState())

    assert blocking_error is False
    assert set(response.model_dump().keys()) == {
        "run_id",
        "report",
        "context",
        "workflow_invalid",
        "workflow_missing_steps",
    }
