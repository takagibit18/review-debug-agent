"""CLI entrypoint for crawl/eval/report workflows."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from eval.artifacts import write_eval_artifacts
from eval.crawler.fixture_generator import FixtureGenerator
from eval.diagnostics import load_diagnostics_report
from eval.metrics import build_eval_report, write_human_review_template
from eval.report import render_report, save_report_json
from eval.run_summary import summarize_event_log
from eval.runner import load_fixtures, run_suite
from eval.schemas import EVAL_MATCHER_VERSION, EvalReport, EvalResult, EvalVariant
from eval.trend import build_quality_trend, expand_report_inputs
from src.config import get_settings


@click.group()
def main() -> None:
    """Golden set pipeline and evaluation runner."""


@main.command("crawl")
@click.option("--suite", default="golden", help="Fixture suite name.")
@click.option("--max-repos", default=10, type=int, help="Max discovered repositories.")
@click.option("--max-prs-per-repo", default=5, type=int, help="Max PRs per repository.")
@click.option(
    "--curated/--no-curated",
    default=True,
    help="Use curated repository list instead of GitHub search.",
)
@click.option(
    "--curated-file",
    default=(Path("eval") / "crawler" / "curated_repos.json").as_posix(),
    type=click.Path(exists=False),
    help="Path to curated repository JSON file.",
)
@click.option(
    "--concurrency",
    default=3,
    type=int,
    help="Max concurrent PR processing tasks.",
)
@click.option(
    "--min-expected-issues",
    default=0,
    type=int,
    help="Minimum expected issue count required to keep a fixture.",
)
@click.option(
    "--candidate-mode",
    default="merged-bugfix",
    type=click.Choice(["merged-bugfix", "rejected-pr"]),
    help="PR discovery strategy: merged bug-fix PRs or rejected unmerged PRs with review evidence.",
)
def crawl_cmd(
    suite: str,
    max_repos: int,
    max_prs_per_repo: int,
    curated: bool,
    curated_file: str,
    concurrency: int,
    min_expected_issues: int,
    candidate_mode: str,
) -> None:
    """Discover PRs and generate fixtures."""
    curated_repos = _load_curated_repos(Path(curated_file), enabled=curated)
    asyncio.run(
        _crawl(
            suite=suite,
            max_repos=max_repos,
            max_prs_per_repo=max_prs_per_repo,
            curated_repos=curated_repos,
            concurrency=concurrency,
            min_expected_issues=min_expected_issues,
            candidate_mode=candidate_mode,
        )
    )


@main.command("eval")
@click.option("--suite", default="golden", help="Fixture suite to run.")
@click.option(
    "--fixtures-dir",
    default=(Path("eval") / "fixtures").as_posix(),
    type=click.Path(exists=True, file_okay=False),
    help="Fixture directory. Development baselines use eval/development_fixtures.",
)
@click.option(
    "--include-unreviewed",
    is_flag=True,
    default=False,
    help="Include fixtures with metadata.reviewed=false.",
)
@click.option(
    "--samples",
    default=None,
    type=int,
    help="How many sampled runs per fixture. Defaults to EVAL_SAMPLES or 1.",
)
@click.option(
    "--concurrency",
    default=None,
    type=int,
    help="Max concurrent sampled runs per fixture. Defaults to EVAL_CONCURRENCY or 1.",
)
@click.option(
    "--fixture-concurrency",
    default=None,
    type=int,
    help="Max concurrent fixtures. Defaults to EVAL_FIXTURE_CONCURRENCY or 3.",
)
@click.option(
    "--review-max-iterations",
    default=None,
    type=int,
    help="Review loop iterations for eval runs. Defaults to EVAL_REVIEW_MAX_ITERATIONS or 2.",
)
@click.option(
    "--temperature",
    default=None,
    type=float,
    help="Model sampling temperature for eval runs. Defaults to EVAL_TEMPERATURE or 0.0.",
)
@click.option(
    "--variant-id",
    default=None,
    help="Stable eval variant id recorded in every result.",
)
@click.option(
    "--context-mode",
    default=None,
    type=click.Choice(["agent_search", "graph_hybrid"]),
    help="Explicit context strategy; overrides REVIEW_CONTEXT_MODE for this eval.",
)
@click.option(
    "--graph-cache-mode",
    default=None,
    type=click.Choice(["disabled", "cold", "warm"]),
    help="Graph cache contract for this variant.",
)
@click.option(
    "--skill-retrieval-mode",
    default=None,
    type=click.Choice(["sequential", "deterministic"]),
    help="Review Skill loading mode for this variant.",
)
@click.option(
    "--skill-bank-path",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Optional fixed Review Skill Bank directory.",
)
@click.option(
    "--output-json",
    default=None,
    type=click.Path(exists=False),
    help="Optional fixed output path for report JSON.",
)
@click.option(
    "--repo",
    "repo_filters",
    multiple=True,
    help="Only run fixtures from this owner/repo. May be provided multiple times.",
)
@click.option(
    "--fixture-id",
    "fixture_id_filters",
    multiple=True,
    help="Only run the named fixture id. May be provided multiple times.",
)
def eval_cmd(
    suite: str,
    fixtures_dir: str,
    include_unreviewed: bool,
    samples: int | None,
    concurrency: int | None,
    fixture_concurrency: int | None,
    review_max_iterations: int | None,
    temperature: float | None,
    variant_id: str | None,
    context_mode: str | None,
    graph_cache_mode: str | None,
    skill_retrieval_mode: str | None,
    skill_bank_path: str | None,
    output_json: str | None,
    repo_filters: tuple[str, ...],
    fixture_id_filters: tuple[str, ...],
) -> None:
    """Run evaluation for one suite."""
    settings = get_settings()
    selected_mode = context_mode or settings.review_context_mode
    selected_cache_mode = graph_cache_mode or (
        "disabled" if selected_mode == "agent_search" else "warm"
    )
    try:
        variant = EvalVariant(
            id=variant_id
            or (
                "A-agent-search"
                if selected_mode == "agent_search"
                else "B-graph-hybrid"
            ),
            context_mode=selected_mode,
            graph_cache_mode=selected_cache_mode,
            skill_retrieval_mode=(
                skill_retrieval_mode or settings.review_skill_retrieval_mode
            ),
            skill_bank_path=skill_bank_path or "",
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    asyncio.run(
        _evaluate(
            suite=suite,
            fixtures_dir=fixtures_dir,
            include_unreviewed=include_unreviewed,
            samples=samples if samples is not None else settings.eval_samples,
            concurrency=(
                concurrency if concurrency is not None else settings.eval_concurrency
            ),
            fixture_concurrency=(
                fixture_concurrency
                if fixture_concurrency is not None
                else settings.eval_fixture_concurrency
            ),
            review_max_iterations=(
                review_max_iterations
                if review_max_iterations is not None
                else settings.eval_review_max_iterations
            ),
            temperature=(
                temperature if temperature is not None else settings.eval_temperature
            ),
            output_json=output_json,
            repo_filters=repo_filters,
            fixture_id_filters=fixture_id_filters,
            variant=variant,
        )
    )


@main.command("report")
@click.option("--input", "input_path", required=True, type=click.Path(exists=True))
@click.option(
    "--diagnostics",
    is_flag=True,
    default=False,
    help="Render per-fixture diagnostics derived from event logs.",
)
def report_cmd(input_path: str, diagnostics: bool) -> None:
    """Render report from existing JSON."""
    report = EvalReport.model_validate_json(
        Path(input_path).read_text(encoding="utf-8")
    )
    render_report(report, diagnostics=diagnostics)


@main.command("diagnose")
@click.option("--input", "input_path", required=True, type=click.Path(exists=True))
def diagnose_cmd(input_path: str) -> None:
    """Emit JSON diagnostics for an existing eval report."""
    diagnostics = load_diagnostics_report(input_path)
    click.echo(diagnostics.model_dump_json(indent=2))


@main.command("summarize-log")
@click.option("--input", "input_path", required=True, type=click.Path(exists=False))
def summarize_log_cmd(input_path: str) -> None:
    """Emit a compact JSON summary for one event log."""
    summary = summarize_event_log(input_path)
    click.echo(summary.model_dump_json(indent=2))


@main.command("trend")
@click.argument("inputs", nargs=-1, required=True)
def trend_cmd(inputs: tuple[str, ...]) -> None:
    """Emit JSON trend data across multiple eval reports.

    INPUTS are report paths or glob patterns, for example
    eval/outputs/*_report.json
    """
    paths = expand_report_inputs(inputs)
    if not paths:
        raise click.ClickException("No report files matched the provided inputs.")
    trend = build_quality_trend(paths)
    click.echo(trend.model_dump_json(indent=2))


@main.command("merge-reports")
@click.argument(
    "inputs",
    nargs=-1,
    required=True,
    type=click.Path(exists=True),
)
@click.option(
    "--suite",
    default="golden",
    help="Suite name for the merged report.",
)
@click.option(
    "--output-json",
    required=True,
    type=click.Path(exists=False),
    help="Output path for the merged report JSON.",
)
def merge_reports_cmd(inputs: tuple[str, ...], suite: str, output_json: str) -> None:
    """Merge batch eval reports and recompute aggregate metrics."""
    reports = [
        EvalReport.model_validate_json(Path(path).read_text(encoding="utf-8"))
        for path in inputs
    ]
    results: list[EvalResult] = [
        result for report in reports for result in report.results
    ]
    sampled_results = [
        result for report in reports for result in report.sampled_results
    ]
    bank_digests = {report.skill_bank_digest for report in reports}
    if len(bank_digests) > 1:
        raise click.ClickException(
            "Cannot merge reports produced with different or unspecified Skill Banks."
        )
    report = build_eval_report(
        suite=suite,
        results=results,
        sampled_results=sampled_results,
    )
    report.skill_bank_digest = next(iter(bank_digests), "")
    report_path = save_report_json(report, output_path=output_json)
    artifact_paths = write_eval_artifacts(report, report_path)
    render_report(report)
    click.echo(f"Merged {len(reports)} reports into {report_path.as_posix()}")
    click.echo(
        f"Diagnostics saved to: {Path(artifact_paths.diagnostics_path).as_posix()}"
    )


async def _crawl(
    suite: str,
    max_repos: int,
    max_prs_per_repo: int,
    curated_repos: list[str] | None,
    concurrency: int,
    min_expected_issues: int,
    candidate_mode: str,
) -> None:
    generator = FixtureGenerator(min_expected_issues=min_expected_issues)
    try:
        written = await generator.generate(
            suite=suite,
            max_repos=max_repos,
            max_prs_per_repo=max_prs_per_repo,
            curated_repos=curated_repos,
            concurrency=concurrency,
            candidate_mode=candidate_mode,  # type: ignore[arg-type]
        )
    finally:
        await generator.close()
    click.echo(f"Generated {len(written)} fixtures.")
    for path in written:
        click.echo(f"- {path.as_posix()}")


def _load_curated_repos(path: Path, *, enabled: bool) -> list[str] | None:
    if not enabled:
        return None
    if not path.exists():
        raise click.ClickException(
            f"Curated repository file not found: {path.as_posix()}"
        )

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise click.ClickException(
            "Curated repository file must contain a JSON object."
        )

    repos = raw.get("repos")
    if not isinstance(repos, list):
        raise click.ClickException(
            "Curated repository file must include a 'repos' list."
        )

    names: list[str] = []
    for item in repos:
        if isinstance(item, dict):
            full_name = str(item.get("full_name", "")).strip()
            if full_name:
                names.append(full_name)
            continue
        if isinstance(item, str) and item.strip():
            names.append(item.strip())

    if not names:
        raise click.ClickException("Curated repository list is empty.")
    return names


async def _evaluate(
    suite: str,
    include_unreviewed: bool = False,
    *,
    fixtures_dir: str | Path = Path("eval") / "fixtures",
    samples: int = 1,
    concurrency: int = 1,
    fixture_concurrency: int = 1,
    review_max_iterations: int | None = None,
    temperature: float = 0.0,
    output_json: str | None = None,
    repo_filters: tuple[str, ...] = (),
    fixture_id_filters: tuple[str, ...] = (),
    variant: EvalVariant | None = None,
) -> None:
    fixtures = load_fixtures(
        fixtures_dir=fixtures_dir,
        suite=suite,
        reviewed_only=not include_unreviewed,
    )
    fixtures = _filter_fixtures(
        fixtures,
        repo_filters=repo_filters,
        fixture_id_filters=fixture_id_filters,
    )
    if not fixtures:
        raise click.ClickException(f"No fixtures found for suite '{suite}'.")

    sampled_results = await run_suite(
        fixtures,
        samples=max(1, samples),
        concurrency=max(1, concurrency),
        fixture_concurrency=max(1, fixture_concurrency),
        review_max_iterations=max(1, review_max_iterations),
        temperature=temperature,
        variant=variant,
    )
    results: list[EvalResult] = [item.runs[0] for item in sampled_results if item.runs]
    report = build_eval_report(
        suite=suite,
        results=results,
        sampled_results=sampled_results,
    )
    report.variant = variant
    report.matcher_version = EVAL_MATCHER_VERSION
    bank_digests = {
        item.skill_bank_digest for item in results if item.skill_bank_digest
    }
    if len(bank_digests) > 1:
        raise click.ClickException(
            "Skill Bank changed during eval; discard the run and rerun with a fixed bank."
        )
    report.skill_bank_digest = next(iter(bank_digests), "")
    report_path = save_report_json(report, output_path=output_json)
    artifact_paths = write_eval_artifacts(report, report_path)
    review_sheet = write_human_review_template(
        report,
        output_path=Path("eval") / "outputs" / f"{suite}_human_review.md",
    )
    render_report(report)
    click.echo(f"Report saved to: {report_path.as_posix()}")
    click.echo(
        f"Diagnostics saved to: {Path(artifact_paths.diagnostics_path).as_posix()}"
    )
    click.echo(
        f"Run summaries saved to: {Path(artifact_paths.run_summaries_path).as_posix()}"
    )
    click.echo(f"Human review sheet: {review_sheet.as_posix()}")


def _filter_fixtures(
    fixtures: list[object],
    *,
    repo_filters: tuple[str, ...] = (),
    fixture_id_filters: tuple[str, ...] = (),
) -> list[object]:
    """Filter fixtures for batch eval runs."""
    repos = {item.strip() for item in repo_filters if item.strip()}
    fixture_ids = {item.strip() for item in fixture_id_filters if item.strip()}
    filtered = []
    for fixture in fixtures:
        if repos and getattr(fixture.source, "repo_full_name", "") not in repos:
            continue
        if fixture_ids and getattr(fixture, "id", "") not in fixture_ids:
            continue
        filtered.append(fixture)
    return filtered


if __name__ == "__main__":
    main()
