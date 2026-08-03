"""从本地 development run 生成可提交的 Agent Search 紧凑基线。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.analyzer import finding_verifier
from src.analyzer.prompts import review_system_prompt

RAW_REPORT = ROOT / "eval/outputs/agent-baseline-v1.json"
OUTPUT = ROOT / "eval/baselines/agent-baseline-v1.json"
FIXTURE = ROOT / "eval/development_fixtures/development_agent_search_cross_file.json"
MATCHER_SOURCE = ROOT / "eval/runner.py"
CONSOLIDATOR_SOURCE = ROOT / "src/analyzer/root_cause.py"
IMPLEMENTATION_SCOPE = (
    "docs/graph-ab/current-coupling-audit.md",
    "eval/development_fixtures/development_agent_search_cross_file.json",
    "eval/run.py",
    "eval/run_summary.py",
    "eval/runner.py",
    "eval/schemas.py",
    "src/analyzer/context_mode.py",
    "src/analyzer/context_state.py",
    "src/analyzer/context_strategy.py",
    "src/analyzer/evidence_policy.py",
    "src/analyzer/finding_verifier.py",
    "src/analyzer/output_formatter.py",
    "src/analyzer/prompts.py",
    "src/analyzer/verifier_context.py",
    "src/config.py",
    "src/orchestrator/agent_loop.py",
    "src/orchestrator/tool_schemas.py",
    "tests/test_graph_ab_context_modes.py",
    "tests/test_v023_compatibility.py",
)
FORBIDDEN_GRAPH_EVENTS = {
    "relation_graph_built",
    "index_lifecycle",
    "context_manifest_created",
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _implementation_snapshot() -> str:
    digest = hashlib.sha256()
    for relative in sorted(IMPLEMENTATION_SCOPE):
        content = (
            (ROOT / relative).read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        )
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def _load_run() -> tuple[dict[str, Any], dict[str, Any], Path]:
    raw = json.loads(RAW_REPORT.read_text(encoding="utf-8"))
    assert raw["suite"] == "development"
    assert raw["fixture_count"] == 1
    assert raw["variant"] == {
        "id": "A-agent-search",
        "context_mode": "agent_search",
        "graph_cache_mode": "disabled",
    }
    result = raw["results"][0]
    assert result["schema_valid"] is True
    assert result["expected_count"] == result["matched_count"] == 1
    assert result["false_positive_count"] == 0
    metrics = result["process_metrics"]
    assert metrics["graph_status"] == "disabled"
    assert metrics["graph_cache_mode"] == "not_applicable"
    assert metrics["manifest_count"] == metrics["manifest_token_cost"] == 0
    assert metrics["parsed_file_count"] is None
    assert metrics["graph_node_count"] is None
    assert metrics["graph_edge_count"] is None
    assert metrics["graph_cache_hit"] is None
    event_log = Path(result["event_log_path"])
    events = [
        json.loads(line)
        for line in event_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_types = {event["event_type"] for event in events}
    assert not event_types.intersection(FORBIDDEN_GRAPH_EVENTS)
    assert "finding_verification_completed" in event_types
    assert "root_cause_consolidation_completed" in event_types
    return raw, result, event_log


def main() -> None:
    raw, result, event_log = _load_run()
    metrics = result["process_metrics"]
    implementation_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    verifier_prompt = (
        finding_verifier._COMMON_VERIFIER_SYSTEM_PROMPT
        + finding_verifier._AGENT_VERIFIER_POLICY
    )
    artifact = {
        "baseline_id": "agent-baseline-v1",
        "variant_id": result["variant_id"],
        "context_mode": result["context_mode"],
        "graph_cache_mode": "disabled",
        "formal_graph_ab_executed": False,
        "implementation_commit_sha": implementation_commit,
        "implementation_snapshot_sha256": _implementation_snapshot(),
        "implementation_snapshot_file_count": len(IMPLEMENTATION_SCOPE),
        "run_id": result["run_id"],
        "fixture_id": result["fixture_id"],
        "schema_valid": result["schema_valid"],
        "expected_count": result["expected_count"],
        "matched_count": result["matched_count"],
        "false_positive_count": result["false_positive_count"],
        "review_iterations": metrics["review_iterations"],
        "tool_call_count": metrics["tool_call_count"],
        "read_file_calls": metrics["read_file_calls"],
        "grep_calls": metrics["grep_calls"],
        "symbol_lookup_calls": metrics["symbol_lookup_calls"],
        "candidate_findings": metrics["candidate_issue_count"],
        "verifier_accepted": metrics["verifier_accepted_count"],
        "verifier_rejected": metrics["verifier_rejected_count"],
        "deterministic_evidence_checked": metrics[
            "deterministic_evidence_checked_count"
        ],
        "deterministic_evidence_passed": metrics["deterministic_evidence_passed_count"],
        "final_root_cause_findings": metrics["final_root_cause_count"],
        "reviewer_latency_seconds": metrics["reviewer_latency_seconds"],
        "verifier_latency_seconds": metrics["verifier_latency_seconds"],
        "consolidation_latency_seconds": metrics["consolidation_latency_seconds"],
        "end_to_end_latency_seconds": metrics["end_to_end_latency_seconds"],
        "total_tokens": metrics["total_tokens"],
        "graph_telemetry": {
            "graph_status": metrics["graph_status"],
            "graph_cache_mode": metrics["graph_cache_mode"],
            "manifest_count": metrics["manifest_count"],
            "manifest_token_cost": metrics["manifest_token_cost"],
            "parsed_file_count": metrics["parsed_file_count"],
            "graph_node_count": metrics["graph_node_count"],
            "graph_edge_count": metrics["graph_edge_count"],
            "cache_hit": metrics["graph_cache_hit"],
        },
        "contracts": {
            "reviewer_prompt_sha256": _sha256_bytes(
                review_system_prompt("agent_search").encode("utf-8")
            ),
            "verifier_prompt_sha256": _sha256_bytes(verifier_prompt.encode("utf-8")),
            "consolidator_prompt_sha256": _sha256_file(CONSOLIDATOR_SOURCE),
            "finding_schema_version": result["raw_output"]["report"]["schema_version"],
            "dataset_version": "development-agent-search-v1",
            "dataset_sha256": _sha256_file(FIXTURE),
            "matcher_version": raw["matcher_version"],
            "matcher_source_sha256": _sha256_file(MATCHER_SOURCE),
        },
        "evidence": {
            "raw_output_sha256": _sha256_file(RAW_REPORT),
            "event_log_sha256": _sha256_file(event_log),
            "source_output_path": _relative(RAW_REPORT),
            "source_event_log_path": _relative(event_log),
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
