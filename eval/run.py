"""CLI entrypoint for crawl/eval/report workflows."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click

from eval.crawler.fixture_generator import FixtureGenerator
from eval.metrics import build_eval_report, write_human_review_template
from eval.report import render_report, save_report_json
from eval.runner import load_fixtures, run_suite
from eval.schemas import EvalReport, EvalResult
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
def crawl_cmd(
    suite: str,
    max_repos: int,
    max_prs_per_repo: int,
    curated: bool,
    curated_file: str,
    concurrency: int,
    min_expected_issues: int,
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
        )
    )


@main.command("eval")
@click.option("--suite", default="golden", help="Fixture suite to run.")
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
    "--temperature",
    default=None,
    type=float,
    help="Model sampling temperature for eval runs. Defaults to EVAL_TEMPERATURE or 0.0.",
)
def eval_cmd(
    suite: str,
    include_unreviewed: bool,
    samples: int | None,
    concurrency: int | None,
    temperature: float | None,
) -> None:
    """Run evaluation for one suite."""
    settings = get_settings()
    asyncio.run(
        _evaluate(
            suite=suite,
            include_unreviewed=include_unreviewed,
            samples=samples if samples is not None else settings.eval_samples,
            concurrency=(
                concurrency if concurrency is not None else settings.eval_concurrency
            ),
            temperature=(
                temperature if temperature is not None else settings.eval_temperature
            ),
        )
    )


@main.command("report")
@click.option("--input", "input_path", required=True, type=click.Path(exists=True))
def report_cmd(input_path: str) -> None:
    """Render report from existing JSON."""
    report = EvalReport.model_validate_json(Path(input_path).read_text(encoding="utf-8"))
    render_report(report)


async def _crawl(
    suite: str,
    max_repos: int,
    max_prs_per_repo: int,
    curated_repos: list[str] | None,
    concurrency: int,
    min_expected_issues: int,
) -> None:
    generator = FixtureGenerator(min_expected_issues=min_expected_issues)
    try:
        written = await generator.generate(
            suite=suite,
            max_repos=max_repos,
            max_prs_per_repo=max_prs_per_repo,
            curated_repos=curated_repos,
            concurrency=concurrency,
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
        raise click.ClickException(f"Curated repository file not found: {path.as_posix()}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise click.ClickException("Curated repository file must contain a JSON object.")

    repos = raw.get("repos")
    if not isinstance(repos, list):
        raise click.ClickException("Curated repository file must include a 'repos' list.")

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
    samples: int = 1,
    concurrency: int = 1,
    temperature: float = 0.0,
) -> None:
    fixtures = load_fixtures(suite=suite, reviewed_only=not include_unreviewed)
    if not fixtures:
        raise click.ClickException(f"No fixtures found for suite '{suite}'.")

    sampled_results = await run_suite(
        fixtures,
        samples=max(1, samples),
        concurrency=max(1, concurrency),
        temperature=temperature,
    )
    results: list[EvalResult] = [item.runs[0] for item in sampled_results if item.runs]
    report = build_eval_report(
        suite=suite,
        results=results,
        sampled_results=sampled_results,
    )
    report_path = save_report_json(report)
    review_sheet = write_human_review_template(
        report,
        output_path=Path("eval") / "outputs" / f"{suite}_human_review.md",
    )
    render_report(report)
    click.echo(f"Report saved to: {report_path.as_posix()}")
    click.echo(f"Human review sheet: {review_sheet.as_posix()}")


if __name__ == "__main__":
    main()

