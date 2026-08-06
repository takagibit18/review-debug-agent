"""agent-baseline-v1 封板产物的一致性合同。"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "eval/contracts/agent-baseline-v1.yaml"
ARTIFACT_PATH = ROOT / "eval/baselines/agent-baseline-v1.json"
FORBIDDEN_GRAPH_EVENTS = {
    "relation_graph_built",
    "index_lifecycle",
    "context_manifest_created",
}
FROZEN_TAG = "eval/agent-baseline-v1"
MATCHER_FUNCTIONS = {
    "_severity_rank",
    "_match_issues",
    "_repair_unit_matches",
    "_meets_expected_severity_floor",
    "_is_eval_expected_location_issue",
    "_location_matches",
    "_semantic_location_matches",
    "_issue_matches_expected_location",
    "_semantic_text_matches",
    "_semantic_match_tokens",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _frozen_runner_source() -> bytes:
    return subprocess.run(
        ["git", "show", f"{FROZEN_TAG}:eval/runner.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout


def _matcher_ast(source: bytes) -> dict[str, str]:
    tree = ast.parse(source.decode("utf-8"))
    functions = {
        node.name: ast.dump(node, include_attributes=False)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in MATCHER_FUNCTIONS
    }
    assert functions.keys() == MATCHER_FUNCTIONS
    return functions


def _load() -> tuple[dict, dict]:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    artifact = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    return contract, artifact


def test_agent_baseline_contract_matches_compact_artifact() -> None:
    contract, artifact = _load()

    assert contract["contract_id"] == artifact["baseline_id"]
    assert contract["variant"]["id"] == artifact["variant_id"]
    assert contract["variant"]["context_mode"] == artifact["context_mode"]
    assert contract["variant"]["graph_cache_mode"] == artifact["graph_cache_mode"]
    assert (
        contract["repository"]["implementation_commit_sha"]
        == artifact["implementation_commit_sha"]
    )
    assert (
        contract["repository"]["implementation_snapshot_sha256"]
        == artifact["implementation_snapshot_sha256"]
    )
    assert contract["baseline_run"]["run_id"] == artifact["run_id"]
    assert (
        contract["baseline_run"]["compact_artifact"]
        == ARTIFACT_PATH.relative_to(ROOT).as_posix()
    )
    assert contract["baseline_run"]["compact_artifact_sha256"] == _sha256(ARTIFACT_PATH)
    for key in (
        "reviewer_prompt_sha256",
        "verifier_prompt_sha256",
        "consolidator_prompt_sha256",
        "finding_schema_version",
        "dataset_version",
        "dataset_sha256",
        "matcher_version",
        "matcher_source_sha256",
    ):
        assert contract["contracts"][key] == artifact["contracts"][key]


def test_agent_baseline_frozen_agent_search_graph_isolation() -> None:
    contract, artifact = _load()
    telemetry = artifact["graph_telemetry"]

    assert artifact["schema_valid"] is True
    assert artifact["expected_count"] == artifact["matched_count"] == 1
    assert artifact["false_positive_count"] == 0
    assert artifact["formal_graph_ab_executed"] is False
    assert telemetry == {
        "graph_status": "disabled",
        "graph_cache_mode": "not_applicable",
        "manifest_count": 0,
        "manifest_token_cost": 0,
        "parsed_file_count": None,
        "graph_node_count": None,
        "graph_edge_count": None,
        "cache_hit": None,
    }
    assert contract["freeze"] == {
        "implementation_ready": True,
        "git_commit_ready": True,
        "tag_name": "eval/agent-baseline-v1",
        "tag_created": True,
        "status": "frozen",
        "formal_graph_ab_executed": False,
    }


def test_agent_baseline_source_hashes_when_local_outputs_exist() -> None:
    contract, artifact = _load()
    raw_report = ROOT / artifact["evidence"]["source_output_path"]
    event_log = ROOT / artifact["evidence"]["source_event_log_path"]

    if raw_report.exists():
        assert _sha256(raw_report) == artifact["evidence"]["raw_output_sha256"]
        assert _sha256(raw_report) == contract["baseline_run"]["raw_report_sha256"]
    if event_log.exists():
        assert _sha256(event_log) == artifact["evidence"]["event_log_sha256"]
        assert _sha256(event_log) == contract["baseline_run"]["event_log_sha256"]
        event_types = {
            json.loads(line)["event_type"]
            for line in event_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        assert not event_types.intersection(FORBIDDEN_GRAPH_EVENTS)
        assert "finding_verification_completed" in event_types
        assert "root_cause_consolidation_completed" in event_types


def test_agent_baseline_source_contract_hashes() -> None:
    contract, artifact = _load()
    frozen_runner = _frozen_runner_source()

    assert (
        _sha256(
            ROOT / "eval/development_fixtures/development_agent_search_cross_file.json"
        )
        == artifact["contracts"]["dataset_sha256"]
    )
    frozen_hashes = {
        hashlib.sha256(frozen_runner).hexdigest(),
        hashlib.sha256(frozen_runner.replace(b"\n", b"\r\n")).hexdigest(),
    }
    assert artifact["contracts"]["matcher_source_sha256"] in frozen_hashes
    assert _matcher_ast((ROOT / "eval/runner.py").read_bytes()) == _matcher_ast(
        frozen_runner
    )
    assert contract["formal_graph_ab_executed"] is False
