"""Durable Graph A/B checkpoint and resume tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import eval.graph_ab_pilot as pilot
from eval.graph_ab_checkpoint import CheckpointJournal, StableRunKey
from eval.graph_ab_pilot import (
    ControlledPilotInterruption,
    IndexArtifact,
    PilotRunRecord,
    VariantContractResult,
    compact_summary,
)
from eval.schemas import EvalResult, Fixture


def _key(*, snapshot: str = "snapshot", contract_hash: str = "a" * 64) -> StableRunKey:
    return StableRunKey(
        experiment_id="experiment",
        fixture_id="fixture",
        sample_index=1,
        variant_id="A-agent-search",
        repository_snapshot=snapshot,
        experiment_contract_hash=contract_hash,
    )


def _contract(variant_id: str, *, valid: bool = True) -> VariantContractResult:
    graph = variant_id != "A-agent-search"
    return VariantContractResult(
        expected_variant_id=variant_id,
        expected_context_mode="graph_hybrid" if graph else "agent_search",
        expected_graph_cache_mode="warm" if graph else "not_applicable",
        actual_context_mode="graph_hybrid" if graph else "agent_search",
        actual_graph_status="ready" if graph else "disabled",
        actual_graph_cache_mode="warm" if graph else "not_applicable",
        actual_cache_hit=True if graph else None,
        actual_manifest_count=1 if graph else 0,
        fallback_reason="",
        valid=valid,
        errors=[] if valid else ["schema_invalid"],
    )


def _run_record(
    *, valid: bool = True, workspace_failure: bool = False
) -> PilotRunRecord:
    result = EvalResult(
        fixture_id="fixture",
        fixture_type="review",
        variant_id="A-agent-search",
        context_mode="agent_search",
        graph_cache_mode="disabled",
        run_id="" if workspace_failure else "run",
        schema_valid=valid,
        error=None if valid else "failed",
        stage_timings={} if workspace_failure else {"prepare_workspace_seconds": 0.1},
    )
    return PilotRunRecord(
        fixture_id="fixture",
        fixture_types=["test"],
        repository_snapshot="snapshot",
        sample=1,
        order=1,
        variant_id="A-agent-search",
        run_id=result.run_id or "failed:fixture:A-agent-search:1",
        valid=valid,
        invalid_reasons=[] if valid else ["schema_invalid"],
        contract=_contract("A-agent-search", valid=valid),
        result=result,
    )


def test_completed_run_is_immediately_present_in_checkpoint(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    journal = CheckpointJournal(path)
    journal.append(
        key=_key(),
        status="measured",
        valid=True,
        run_record=_run_record().model_dump(mode="json"),
    )

    assert path.is_file()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert CheckpointJournal(path).completed(_key()) is not None


def test_priming_record_never_completes_measured_run(tmp_path: Path) -> None:
    journal = CheckpointJournal(tmp_path / "checkpoint.jsonl")
    journal.append(key=_key(), status="priming", valid=False, run_record=None)

    assert journal.completed(_key()) is None
    assert journal.records[0].status == "priming"


def test_invalid_and_workspace_failure_are_distinguished() -> None:
    assert pilot._checkpoint_status(_run_record(valid=False)) == "invalid"
    assert (
        pilot._checkpoint_status(_run_record(valid=False, workspace_failure=True))
        == "workspace_failure"
    )


def test_corrupt_jsonl_fails_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    path.write_text('{"not": "complete"}\n{broken\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="Corrupt checkpoint"):
        CheckpointJournal(path)


def test_duplicate_stable_attempt_fails_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "checkpoint.jsonl"
    journal = CheckpointJournal(path)
    record = journal.append(
        key=_key(),
        status="measured",
        valid=True,
        run_record=_run_record().model_dump(mode="json"),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record.model_dump_json() + "\n")

    with pytest.raises(RuntimeError, match="Duplicate checkpoint record"):
        CheckpointJournal(path)


def test_snapshot_and_contract_hash_are_part_of_stable_key(tmp_path: Path) -> None:
    journal = CheckpointJournal(tmp_path / "checkpoint.jsonl")
    journal.append(
        key=_key(),
        status="measured",
        valid=True,
        run_record=_run_record().model_dump(mode="json"),
    )

    assert journal.completed(_key(snapshot="changed")) is None
    assert journal.completed(_key(contract_hash="b" * 64)) is None


def test_checkpoint_sanitizer_removes_prompt_code_keys_and_paths() -> None:
    record = _run_record()
    record.result.raw_output = {"private_code": "secret source"}
    record.result.event_log_path = "C:/private/events.jsonl"
    record.lifecycle = {
        "prompt": "full prompt",
        "api_key": "secret-key",
        "index_artifact": {"path": "C:/private/index.sqlite3"},
    }

    serialized = str(pilot._checkpoint_run_record(record))

    assert "secret source" not in serialized
    assert "full prompt" not in serialized
    assert "secret-key" not in serialized
    assert "C:/private" not in serialized


def _pilot_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outcome: Callable[[str, int], bool] | None = None,
) -> tuple[dict[str, Any], Fixture, list[str]]:
    config: dict[str, Any] = {
        "experiment_id": "checkpoint-test",
        "formal_graph_ab": False,
        "held_out_executed": False,
        "shared": {"temperature": 0.0, "max_iterations": 1},
        "variants": [
            {
                "id": "A-agent-search",
                "context_mode": "agent_search",
                "graph_cache_mode": "disabled",
            },
            {
                "id": "B1-graph-hybrid-cold",
                "context_mode": "graph_hybrid",
                "graph_cache_mode": "cold",
            },
            {
                "id": "B2-graph-hybrid-warm",
                "context_mode": "graph_hybrid",
                "graph_cache_mode": "warm",
                "requires_priming": True,
            },
        ],
    }
    fixture = Fixture.model_validate(
        {
            "id": "fixture",
            "type": "review",
            "source": {"repo_full_name": "acme/repo", "pr_number": 1},
            "input": {"files": {"module.py": "value = 1\n"}},
            "expected": {"issues": []},
            "metadata": {"reviewed": True},
        }
    )
    executions: list[str] = []
    index_calls: dict[str, int] = {}

    monkeypatch.setattr(pilot, "_load_config", lambda _path: config)
    monkeypatch.setattr(pilot, "_frozen_contract", lambda _config: {"frozen": True})
    monkeypatch.setattr(
        pilot,
        "_fixture_entries",
        lambda _config, _suite: [(fixture, ["test"], "validation")],
    )
    monkeypatch.setattr(pilot, "_run_git", lambda *args: "d" * 40)
    monkeypatch.setattr(
        pilot, "clear_index", lambda path: index_calls.update({str(path): 0})
    )

    def fake_inspect(path: Path) -> IndexArtifact:
        key = str(path)
        count = index_calls.get(key, 0)
        index_calls[key] = count + 1
        return IndexArtifact(
            path=str(path),
            exists=count % 2 == 1,
            schema_version=3 if count % 2 == 1 else None,
        )

    async def fake_run(
        fixture: Fixture,
        *,
        variant,
        **_kwargs: Any,  # type: ignore[no-untyped-def]
    ) -> tuple[EvalResult, dict[str, Any]]:
        executions.append(variant.id)
        call_index = len(executions)
        valid = outcome(variant.id, call_index) if outcome else True
        lifecycle = (
            {"priming": {"measured": False}}
            if variant.id == "B2-graph-hybrid-warm"
            else {}
        )
        return (
            EvalResult(
                fixture_id=fixture.id,
                fixture_type=fixture.type,
                variant_id=variant.id,
                context_mode=variant.context_mode,
                graph_cache_mode=variant.graph_cache_mode,
                run_id=f"run-{call_index}",
                schema_valid=valid,
                error=None if valid else "invalid output",
                stage_timings={"prepare_workspace_seconds": 0.1},
            ),
            lifecycle,
        )

    def fake_validate(variant, result, _lifecycle):  # type: ignore[no-untyped-def]
        return _contract(variant.id, valid=result.schema_valid)

    monkeypatch.setattr(pilot, "inspect_index", fake_inspect)
    monkeypatch.setattr(pilot, "run_single_lifecycle", fake_run)
    monkeypatch.setattr(pilot, "validate_variant_contract", fake_validate)
    return config, fixture, executions


def _run_harness(
    tmp_path: Path,
    checkpoint: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    return asyncio.run(
        pilot.run_pilot(
            config_path=tmp_path / "config.yaml",
            suite="validation",
            samples=1,
            seed=7,
            checkpoint_path=checkpoint,
            **kwargs,
        )
    )


def test_controlled_interruption_resumes_only_remaining_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, _fixture, executions = _pilot_harness(monkeypatch, tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"

    with pytest.raises(ControlledPilotInterruption):
        _run_harness(tmp_path, checkpoint, stop_after_measured=2)
    durable = CheckpointJournal(checkpoint)
    assert sum(record.status == "measured" for record in durable.records) == 2

    resumed = _run_harness(tmp_path, checkpoint)

    assert len(executions) == 3
    assert len(resumed["records"]) == 3
    assert resumed["checkpoint"]["reused_run_count"] == 2
    assert resumed["checkpoint"]["attempted_run_count"] == 1


def test_invalid_is_preserved_and_retried_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, _fixture, executions = _pilot_harness(
        monkeypatch, tmp_path, outcome=lambda _variant, call: call != 1
    )
    checkpoint = tmp_path / "checkpoint.jsonl"
    with pytest.raises(ControlledPilotInterruption):
        _run_harness(tmp_path, checkpoint, stop_after_measured=1)

    resumed = _run_harness(tmp_path, checkpoint)
    records = CheckpointJournal(checkpoint).records

    assert len(executions) == 4
    assert [record.status for record in records].count("invalid") == 1
    assert [record.status for record in records].count("measured") == 3
    assert len(resumed["records"]) == 3


def test_no_retry_invalid_reuses_failure_without_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, _fixture, executions = _pilot_harness(
        monkeypatch, tmp_path, outcome=lambda _variant, call: call != 1
    )
    checkpoint = tmp_path / "checkpoint.jsonl"
    with pytest.raises(ControlledPilotInterruption):
        _run_harness(tmp_path, checkpoint, stop_after_measured=1)

    resumed = _run_harness(tmp_path, checkpoint, retry_invalid=False)

    assert len(executions) == 3
    assert resumed["checkpoint"]["reused_run_count"] == 1
    assert sum(not record["valid"] for record in resumed["records"]) == 1


def test_snapshot_change_does_not_reuse_completed_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, fixture, executions = _pilot_harness(monkeypatch, tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    _run_harness(tmp_path, checkpoint)
    fixture.input.files["module.py"] = "value = 2\n"

    changed = _run_harness(tmp_path, checkpoint)

    assert len(executions) == 6
    assert changed["checkpoint"]["reused_run_count"] == 0


def test_variant_contract_change_does_not_reuse_completed_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, _fixture, executions = _pilot_harness(monkeypatch, tmp_path)
    checkpoint = tmp_path / "checkpoint.jsonl"
    first = _run_harness(tmp_path, checkpoint)
    config["variants"][0]["contract_revision"] = 2

    changed = _run_harness(tmp_path, checkpoint)

    assert first["experiment_contract_hash"] != changed["experiment_contract_hash"]
    assert len(executions) == 6
    assert changed["checkpoint"]["reused_run_count"] == 0


def test_resumed_summary_equals_uninterrupted_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _config, _fixture, _executions = _pilot_harness(monkeypatch, tmp_path)
    resumed_path = tmp_path / "resumed.jsonl"
    with pytest.raises(ControlledPilotInterruption):
        _run_harness(tmp_path, resumed_path, stop_after_measured=2)
    resumed = compact_summary(_run_harness(tmp_path, resumed_path))

    full = compact_summary(
        _run_harness(tmp_path, tmp_path / "full.jsonl", resume=False)
    )

    for variant_id in pilot.VARIANT_IDS:
        assert resumed["variants"][variant_id]["valid_runs"] == 1
        assert (
            resumed["variants"][variant_id]["aggregate_quality"]
            == full["variants"][variant_id]["aggregate_quality"]
        )
        assert (
            resumed["variants"][variant_id]["structural_metrics"]
            == full["variants"][variant_id]["structural_metrics"]
        )
