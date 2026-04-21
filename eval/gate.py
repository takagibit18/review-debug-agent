"""CI gate validation for golden eval report metrics."""

from __future__ import annotations

import json
from pathlib import Path

import click


@click.command()
@click.option("--report", "report_path", required=True, type=click.Path(exists=True))
@click.option("--schema-validity-min", default=1.0, type=float)
@click.option("--hit-rate-min", default=0.8, type=float)
@click.option("--false-positive-rate-max", default=0.5, type=float)
def main(
    report_path: str,
    schema_validity_min: float,
    hit_rate_min: float,
    false_positive_rate_max: float,
) -> None:
    payload = json.loads(Path(report_path).read_text(encoding="utf-8"))
    metrics = payload.get("metrics", {})
    schema_validity_raw = metrics.get("schema_validity_rate", 0.0)
    hit_rate_raw = metrics.get("hit_rate", 0.0)
    false_positive_rate_raw = metrics.get("false_positive_rate", 1.0)
    schema_validity = float(0.0 if schema_validity_raw is None else schema_validity_raw)
    hit_rate = float(0.0 if hit_rate_raw is None else hit_rate_raw)
    false_positive_rate = float(
        1.0 if false_positive_rate_raw is None else false_positive_rate_raw
    )

    failures: list[str] = []
    if schema_validity < schema_validity_min:
        failures.append(
            f"schema_validity_rate={schema_validity:.3f} < {schema_validity_min:.3f}"
        )
    if hit_rate < hit_rate_min:
        failures.append(f"hit_rate={hit_rate:.3f} < {hit_rate_min:.3f}")
    if false_positive_rate > false_positive_rate_max:
        failures.append(
            f"false_positive_rate={false_positive_rate:.3f} > {false_positive_rate_max:.3f}"
        )
    if failures:
        raise click.ClickException("Eval gate failed: " + "; ".join(failures))
    click.echo(
        "Eval gate passed: "
        f"schema_validity_rate={schema_validity:.3f}, "
        f"hit_rate={hit_rate:.3f}, "
        f"false_positive_rate={false_positive_rate:.3f}"
    )


if __name__ == "__main__":
    main()
