"""Provider-free Review Skill retrieval evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from eval.schemas import Fixture
from src.analyzer.review_skills import (
    DEFAULT_LEGACY_FALLBACK_LIMIT,
    ReviewSkillLoader,
    build_skill_query,
)


def evaluate_retrieval(
    fixtures_dir: Path,
    skill_bank: Path,
    *,
    top_k: int = 5,
    char_budget: int = 4_000,
    legacy_fallback_limit: int = DEFAULT_LEGACY_FALLBACK_LIMIT,
) -> dict[str, Any]:
    """Evaluate annotated fixtures without invoking a model provider."""

    loader = ReviewSkillLoader(
        skill_bank,
        max_chars=char_budget,
        legacy_fallback_limit=legacy_fallback_limit,
    )
    results: list[dict[str, Any]] = []
    expected_total = 0
    retrieved_total = 0
    matched_total = 0
    budget_losses = 0
    status_violations = 0
    budget_violations = 0
    for path in sorted(fixtures_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
        expected = fixture.expected.expected_skill_ids
        if expected is None:
            continue
        selection = loader.retrieve(
            build_skill_query(fixture.input.diff_text or ""), top_k=top_k
        )
        retrieved = [match.skill.id for match in selection.selected]
        expected_set = set(expected)
        retrieved_set = set(retrieved)
        matched = len(expected_set & retrieved_set)
        fixture_budget_losses = sum(
            skill_id in expected_set and reason == "budget"
            for skill_id, reason in selection.skipped
        )
        expected_total += len(expected_set)
        retrieved_total += len(retrieved_set)
        matched_total += matched
        budget_losses += fixture_budget_losses
        status_violations += sum(
            match.skill.status != "active" for match in selection.selected
        )
        budget_violations += int(selection.total_chars > char_budget)
        results.append(
            {
                "fixture_id": fixture.id,
                "expected_skill_ids": sorted(expected_set),
                "retrieved_skill_ids": retrieved,
                "matches": [
                    {
                        "id": match.skill.id,
                        "score": match.score,
                        "reasons": list(match.reasons),
                    }
                    for match in selection.selected
                ],
                "skipped": [
                    {"id": skill_id, "reason": reason}
                    for skill_id, reason in selection.skipped
                ],
                "recall_at_k": matched / len(expected_set) if expected_set else 1.0,
                "precision_at_k": (
                    matched / len(retrieved_set)
                    if retrieved_set
                    else (1.0 if not expected_set else 0.0)
                ),
                "budget_loss_count": fixture_budget_losses,
                "total_chars": selection.total_chars,
            }
        )
    return {
        "retrieval_version": "deterministic-v1",
        "bank_path": skill_bank.as_posix(),
        "bank_digest": loader.bank_digest(),
        "top_k": top_k,
        "char_budget": char_budget,
        "legacy_fallback_limit": legacy_fallback_limit,
        "annotated_fixture_count": len(results),
        "metrics": {
            "recall_at_k": matched_total / expected_total if expected_total else 1.0,
            "precision_at_k": (
                matched_total / retrieved_total if retrieved_total else 1.0
            ),
            "irrelevant_rate": (
                (retrieved_total - matched_total) / retrieved_total
                if retrieved_total
                else 0.0
            ),
            "budget_loss_rate": (
                budget_losses / expected_total if expected_total else 0.0
            ),
            "candidate_or_deprecated_selection_count": status_violations,
            "hard_budget_violation_count": budget_violations,
        },
        "results": results,
    }


@click.command()
@click.option(
    "--fixtures-dir",
    default="eval/fixtures",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--skill-bank",
    default="eval/skill_banks/retrieval-v1",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option("--top-k", default=5, type=click.IntRange(min=0))
@click.option("--char-budget", default=4_000, type=click.IntRange(min=1))
@click.option(
    "--legacy-fallback-limit",
    default=DEFAULT_LEGACY_FALLBACK_LIMIT,
    type=click.IntRange(min=0),
)
@click.option("--output-json", type=click.Path(dir_okay=False, path_type=Path))
def main(
    fixtures_dir: Path,
    skill_bank: Path,
    top_k: int,
    char_budget: int,
    legacy_fallback_limit: int,
    output_json: Path | None,
) -> None:
    report = evaluate_retrieval(
        fixtures_dir,
        skill_bank,
        top_k=top_k,
        char_budget=char_budget,
        legacy_fallback_limit=legacy_fallback_limit,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(rendered, encoding="utf-8")
    click.echo(rendered, nl=False)


if __name__ == "__main__":
    main()
