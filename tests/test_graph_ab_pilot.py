"""Phase-two Graph A/B pilot lifecycle and contract tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.graph_ab_pilot import (
    VARIANT_IDS,
    _fixture_snapshot,
    _load_fixture,
    _runner_ready_for_suite,
    clear_index,
    inspect_index,
    validate_variant_contract,
    variant_order,
)
from eval.schemas import EvalResult, EvalVariant, Fixture, ReviewProcessMetrics


def _event_log(tmp_path: Path, *event_types: str) -> str:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"event_type": event_type, "phase": "test", "payload": {}})
            for event_type in event_types
        ),
        encoding="utf-8",
    )
    return str(path)


def _index_artifact() -> dict[str, object]:
    return {
        "exists": True,
        "sha256": "a" * 64,
        "logical_sha256": "b" * 64,
        "schema_version": 3,
        "repository_count": 1,
        "revisions": ["snapshot-sha"],
    }


def _result(
    tmp_path: Path,
    variant: EvalVariant,
    *,
    graph_status: str,
    actual_cache_mode: str,
    cache_hit: bool | None,
    manifest_count: int,
    events: tuple[str, ...] = ("phase_end",),
    fallback: str = "",
    build_latency: float = 0.0,
) -> EvalResult:
    return EvalResult(
        fixture_id="fixture",
        fixture_type="review",
        variant_id=variant.id,
        context_mode=variant.context_mode,
        graph_cache_mode=variant.graph_cache_mode,
        run_id="run-1",
        schema_valid=True,
        event_log_path=_event_log(tmp_path, *events),
        process_metrics=ReviewProcessMetrics(
            event_log_status="ok",
            context_mode=variant.context_mode,
            graph_status=graph_status,
            graph_cache_mode=actual_cache_mode,
            graph_cache_hit=cache_hit,
            manifest_count=manifest_count,
            manifest_token_cost=0 if variant.context_mode == "agent_search" else 20,
            graph_build_latency_seconds=build_latency,
            graph_fallback_reason=fallback,
        ),
    )


def test_agent_contract_accepts_graph_free_telemetry(tmp_path: Path) -> None:
    variant = EvalVariant(
        id="A-agent-search", context_mode="agent_search", graph_cache_mode="disabled"
    )
    result = _result(
        tmp_path,
        variant,
        graph_status="disabled",
        actual_cache_mode="not_applicable",
        cache_hit=None,
        manifest_count=0,
    )

    contract = validate_variant_contract(
        variant, result, {"workspace_sqlite_created": []}
    )

    assert contract.valid
    assert contract.errors == []


def test_agent_contract_rejects_graph_event(tmp_path: Path) -> None:
    variant = EvalVariant(
        id="A-agent-search", context_mode="agent_search", graph_cache_mode="disabled"
    )
    result = _result(
        tmp_path,
        variant,
        graph_status="disabled",
        actual_cache_mode="not_applicable",
        cache_hit=None,
        manifest_count=0,
        events=("relation_graph_built", "phase_end"),
    )

    contract = validate_variant_contract(variant, result)

    assert not contract.valid
    assert "agent_graph_events" in contract.errors


@pytest.mark.parametrize("fallback", ["", "index_unavailable"])
def test_cold_contract_rejects_warm_or_fallback(tmp_path: Path, fallback: str) -> None:
    variant = EvalVariant(
        id="B1-graph-hybrid-cold",
        context_mode="graph_hybrid",
        graph_cache_mode="cold",
    )
    result = _result(
        tmp_path,
        variant,
        graph_status="ready",
        actual_cache_mode="warm" if not fallback else "cold",
        cache_hit=not fallback,
        manifest_count=1,
        fallback=fallback,
        build_latency=0.1,
    )

    contract = validate_variant_contract(variant, result)

    assert not contract.valid
    assert ("cold_cache_mode" in contract.errors) or (
        "graph_fallback" in contract.errors
    )


@pytest.mark.parametrize(
    ("cache_hit", "cache_mode"), [(False, "cold"), (False, "warm")]
)
def test_warm_contract_rejects_cache_miss_or_rebuild(
    tmp_path: Path, cache_hit: bool, cache_mode: str
) -> None:
    variant = EvalVariant(
        id="B2-graph-hybrid-warm",
        context_mode="graph_hybrid",
        graph_cache_mode="warm",
    )
    lifecycle = {
        "priming": {
            "measured": False,
            "telemetry": {"graph_cache_mode": "cold", "cache_hit": False},
            "index_artifact": _index_artifact(),
        },
        "measured_index_artifact": _index_artifact(),
    }
    result = _result(
        tmp_path,
        variant,
        graph_status="ready",
        actual_cache_mode=cache_mode,
        cache_hit=cache_hit,
        manifest_count=1,
    )

    contract = validate_variant_contract(variant, result, lifecycle)

    assert not contract.valid
    assert "warm_cache_hit" in contract.errors or "warm_cache_mode" in contract.errors


def test_warm_contract_accepts_primed_cache_hit(tmp_path: Path) -> None:
    variant = EvalVariant(
        id="B2-graph-hybrid-warm",
        context_mode="graph_hybrid",
        graph_cache_mode="warm",
    )
    lifecycle = {
        "priming": {
            "measured": False,
            "telemetry": {"graph_cache_mode": "cold", "cache_hit": False},
            "index_artifact": _index_artifact(),
        },
        "measured_index_artifact": _index_artifact(),
    }
    result = _result(
        tmp_path,
        variant,
        graph_status="ready",
        actual_cache_mode="warm",
        cache_hit=True,
        manifest_count=1,
    )

    assert validate_variant_contract(variant, result, lifecycle).valid


def test_warm_contract_rejects_logical_index_change(tmp_path: Path) -> None:
    variant = EvalVariant(
        id="B2-graph-hybrid-warm",
        context_mode="graph_hybrid",
        graph_cache_mode="warm",
    )
    prime_index = _index_artifact()
    measured_index = _index_artifact()
    measured_index["logical_sha256"] = "c" * 64
    lifecycle = {
        "priming": {
            "measured": False,
            "telemetry": {"graph_cache_mode": "cold", "cache_hit": False},
            "index_artifact": prime_index,
        },
        "measured_index_artifact": measured_index,
    }
    result = _result(
        tmp_path,
        variant,
        graph_status="ready",
        actual_cache_mode="warm",
        cache_hit=True,
        manifest_count=1,
    )

    contract = validate_variant_contract(variant, result, lifecycle)

    assert not contract.valid
    assert "warm_index_logical_sha_unchanged" in contract.errors


def test_index_lifecycle_clears_and_inspects_owned_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "pilot.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version', '3')")
        connection.execute(
            "CREATE TABLE repositories (repository_id TEXT, revision TEXT)"
        )
        connection.execute("INSERT INTO repositories VALUES('repo', 'sha')")
        connection.commit()
    finally:
        connection.close()
    before = inspect_index(path)

    assert before.exists
    assert before.schema_version == 3
    assert before.repository_count == 1
    assert before.sha256
    assert before.logical_sha256

    clear_index(path)

    assert not inspect_index(path).exists


def test_pairing_order_is_seeded_balanced_and_complete() -> None:
    first = [variant_order(20260804, index) for index in range(3)]
    second = [variant_order(20260804, index) for index in range(3)]

    assert first == second
    assert all(set(order) == set(VARIANT_IDS) for order in first)
    assert len({tuple(order) for order in first}) == 3


def test_scoped_preflight_readiness_does_not_require_absent_smoke_records() -> None:
    records = [SimpleNamespace(fixture_id="golden-stable", valid=True)]
    variants = {variant: {"valid_runs": 1} for variant in VARIANT_IDS}

    assert _runner_ready_for_suite(
        suite="preflight",
        pairing_errors=[],
        records=records,
        variants=variants,
    )
    assert not _runner_ready_for_suite(
        suite="smoke",
        pairing_errors=[],
        records=records,
        variants=variants,
    )


def _workspace_fixture(*, diff_text: str, apply_fixture_diff: bool) -> Fixture:
    return Fixture.model_validate(
        {
            "id": "overlay-fixture",
            "type": "review",
            "source": {"repo_full_name": "owner/repo", "pr_number": 1},
            "input": {
                "diff_text": diff_text,
                "workspace": {
                    "repo_url": "https://example.test/owner/repo.git",
                    "checkout_sha": "a" * 40,
                    "apply_fixture_diff": apply_fixture_diff,
                },
            },
            "expected": {"issues": []},
        }
    )


def test_fixture_snapshot_includes_stable_applied_diff_hash() -> None:
    fixture = _workspace_fixture(diff_text="patch-one\n", apply_fixture_diff=True)

    first = _fixture_snapshot(fixture)
    second = _fixture_snapshot(fixture)

    assert first == second
    assert first.startswith(f"{'a' * 40}+")
    assert first.endswith(
        "74e3da89959450f958d0aecc508a5c37218d6978b173045880dcead560b72791"
    )


def test_fixture_snapshot_changes_for_different_overlay_on_same_checkout() -> None:
    first = _workspace_fixture(diff_text="patch-one\n", apply_fixture_diff=True)
    second = _workspace_fixture(diff_text="patch-two\n", apply_fixture_diff=True)

    assert _fixture_snapshot(first) != _fixture_snapshot(second)


def test_fixture_snapshot_ignores_diff_when_overlay_is_disabled() -> None:
    first = _workspace_fixture(diff_text="patch-one\n", apply_fixture_diff=False)
    second = _workspace_fixture(diff_text="patch-two\n", apply_fixture_diff=False)

    assert _fixture_snapshot(first) == _fixture_snapshot(second)


def test_held_out_fixture_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "held-out.json"
    path.write_text(
        json.dumps(
            {
                "id": "held-out-fixture",
                "type": "review",
                "source": {"repo_full_name": "owner/repo", "pr_number": 1},
                "input": {"files": {}},
                "expected": {"issues": []},
                "metadata": {
                    "suite": "held-out",
                    "tags": ["held-out"],
                    "reviewed": True,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Held-out fixture is forbidden"):
        _load_fixture(path)
