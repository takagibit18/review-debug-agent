"""Phase-two Graph A/B pilot lifecycle and contract tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eval.graph_ab_pilot import (
    VARIANT_IDS,
    _load_fixture,
    clear_index,
    inspect_index,
    validate_variant_contract,
    variant_order,
)
from eval.schemas import EvalResult, EvalVariant, ReviewProcessMetrics


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
