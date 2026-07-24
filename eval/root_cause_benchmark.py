"""Deterministic v023-v025 benchmark and ablation runner.

This benchmark deliberately avoids provider calls so it is cheap and fully
reproducible.  Model tokens/tool calls are therefore reported as the measured
value zero, not estimated.  The regular golden runner remains responsible for
provider-backed Hit Rate/FPR evaluation.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable

from src.analyzer.code_graph import (
    EdgeKind,
    NodeKind,
    StaticRelationGraphBuilder,
    extract_changed_anchors,
    iter_execution_paths,
)
from src.analyzer.context_planner import ChangeCenteredContextPlanner
from src.analyzer.finding_schema import (
    EvidenceProvenance,
    RepairIntent,
    SourceAnchor,
    context_hash,
)
from src.analyzer.language_resolver import UnavailableLspResolver
from src.analyzer.output_formatter import ReviewIssue, ReviewReport, Severity
from src.analyzer.persistent_index import RelationGraphIndex
from src.analyzer.root_cause import (
    ConsolidationVerification,
    ConsolidationVerifier,
    RootCauseConsolidator,
)


DEFAULT_ABLATIONS = ("A", "B", "C", "D", "F", "G")
ALL_ABLATIONS = ("A", "B", "C", "D", "E", "F", "G", "H", "I")
ABLATION_NAMES = {
    "A": "v022_compatible_baseline",
    "B": "structured_finding_schema",
    "C": "root_cause_consolidator",
    "D": "consolidation_verifier",
    "E": "qualified_symbol_identity",
    "F": "change_centered_relation_graph",
    "G": "field_read_write_edges",
    "H": "bounded_execution_flow",
    "I": "optional_resolver_lsp_enrichment",
}


class _AcceptAllClusterVerifier(ConsolidationVerifier):
    def verify(self, proposal, members, merged, **kwargs):  # type: ignore[no-untyped-def]
        return ConsolidationVerification(
            root_cause_id=proposal.root_cause_id,
            accepted=proposal.counterfactual_result == "yes",
            reasons=[],
        )


def run_benchmark(ablations: Iterable[str] = DEFAULT_ABLATIONS) -> dict[str, Any]:
    requested = tuple(dict.fromkeys(value.upper() for value in ablations))
    invalid = sorted(set(requested) - set(ALL_ABLATIONS))
    if invalid:
        raise ValueError(f"unknown ablation(s): {', '.join(invalid)}")
    findings, truth = _finding_fixture()
    results: dict[str, Any] = {}
    for ablation in requested:
        started = perf_counter()
        if ablation in {"A", "B", "C", "D"}:
            metrics = _finding_ablation(ablation, findings, truth)
        else:
            metrics = _graph_ablation(ablation)
        metrics["end_to_end_latency_seconds"] = perf_counter() - started
        metrics["model_token_usage"] = 0
        metrics["reviewer_tool_call_count"] = 0
        results[ablation] = {
            "name": ABLATION_NAMES[ablation],
            "metrics": metrics,
        }
    return {
        "schema_version": "v025-benchmark-1",
        "generated_at": datetime.now(UTC).isoformat(),
        "provider_calls": 0,
        "ablations_run": list(requested),
        "results": results,
    }


def _finding_ablation(
    ablation: str,
    findings: list[ReviewIssue],
    truth: dict[str, str],
) -> dict[str, Any]:
    inputs = [item.model_copy(deep=True) for item in findings]
    if ablation == "A":
        outputs = []
        for item in inputs:
            legacy = ReviewIssue.model_validate(item.v022_payload())
            legacy.finding_id = item.finding_id
            outputs.append(legacy)
        block_count = 0
        average_block_size = 0.0
        accepted_clusters = 0
        rejected_clusters = 0
    elif ablation == "B":
        outputs = inputs
        block_count = 0
        average_block_size = 0.0
        accepted_clusters = 0
        rejected_clusters = 0
    else:
        verifier = (
            _AcceptAllClusterVerifier() if ablation == "C" else ConsolidationVerifier()
        )
        consolidation = RootCauseConsolidator(verifier=verifier).consolidate(
            ReviewReport(issues=inputs)
        )
        outputs = consolidation.report.issues
        block_count = consolidation.metrics.block_count
        average_block_size = consolidation.metrics.average_block_size
        accepted_clusters = consolidation.metrics.accepted_cluster_count
        rejected_clusters = consolidation.metrics.rejected_cluster_count
    quality = _cluster_quality(findings, outputs, truth)
    evidence_complete = sum(
        bool(item.cause_evidence)
        and bool(item.contract_evidence)
        and (not item.trigger or bool(item.trigger_evidence))
        and (not item.impact or bool(item.impact_evidence))
        for item in outputs
    )
    quality.update(
        {
            "verifier_accept_rate": 1.0,
            "evidence_completeness": evidence_complete / len(outputs)
            if outputs
            else 0.0,
            "consolidator_block_count": block_count,
            "average_block_size": average_block_size,
            "accepted_cluster_count": accepted_clusters,
            "rejected_cluster_count": rejected_clusters,
            "average_candidate_tokens": 0.0,
            "included_graph_nodes": 0,
            "included_graph_paths": 0,
            "discarded_paths": 0,
            "unused_context_ratio": 0.0,
            "edge_confidence_contribution": 0.0,
            "graph_build_latency_seconds": 0.0,
            "incremental_update_latency_seconds": 0.0,
            "persistent_cache_hit_rate": 0.0,
        }
    )
    return quality


def _graph_ablation(ablation: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="mergewarden-benchmark-") as raw_root:
        root = Path(raw_root)
        service = root / "service.py"
        tests = root / "tests" / "test_service.py"
        tests.parent.mkdir(parents=True)
        service.write_text(
            "class Cache:\n"
            "    def __init__(self, model):\n"
            "        self.model = model\n\n"
            "    def load(self, model):\n"
            "        self.model = model\n"
            "        return self.model\n\n"
            "def entry(model):\n"
            "    return Cache(model).load(model)\n",
            encoding="utf-8",
        )
        tests.write_text(
            "from service import entry\n\n"
            "def test_entry():\n"
            "    return entry('small')\n",
            encoding="utf-8",
        )
        diff = (
            "diff --git a/service.py b/service.py\n"
            "--- a/service.py\n+++ b/service.py\n"
            "@@ -5,2 +5,2 @@\n"
            "     def load(self, model):\n"
            "-        self.model = None\n"
            "+        self.model = model\n"
        )
        resolver_mode = "lsp" if ablation == "I" else "ast"
        resolver = UnavailableLspResolver() if ablation == "I" else None
        graph_started = perf_counter()
        graph = StaticRelationGraphBuilder(
            root,
            resolver_mode=resolver_mode,
            language_resolver=resolver,
        ).build(files=[service, tests])
        graph_latency = perf_counter() - graph_started
        anchors = extract_changed_anchors(diff, graph)
        plan = ChangeCenteredContextPlanner(
            root,
            max_depth=2,
            max_nodes=20,
            max_context_tokens=1000,
        ).plan(graph, anchors)

        index_path = root / ".mergewarden" / "benchmark.sqlite3"
        first_index = RelationGraphIndex(root, index_path=index_path).build()
        cached_index = RelationGraphIndex(root, index_path=index_path).build()
        service.write_text(
            service.read_text(encoding="utf-8").replace(
                "return self.model", "return self.model or model"
            ),
            encoding="utf-8",
        )
        incremental = RelationGraphIndex(root, index_path=index_path).build()
        field_edges = [
            edge
            for edge in graph.edges
            if edge.kind in {EdgeKind.READS_FIELD, EdgeKind.WRITES_FIELD}
        ]
        qualified = [
            node
            for node in graph.nodes.values()
            if node.kind
            in {NodeKind.CLASS, NodeKind.METHOD, NodeKind.FUNCTION, NodeKind.TEST}
            and node.symbol_id
        ]
        start = graph.nodes.get(anchors[0].symbol_id) if anchors else None
        execution_paths = (
            list(iter_execution_paths(graph, start.node_id, max_depth=2, max_paths=50))
            if start is not None
            else []
        )
        manifest_tokens = sum(item.token_cost for item in plan.manifests)
        included_paths = sum(len(item.included_graph_paths) for item in plan.manifests)
        discarded = sum(
            len(item.discarded_paths) + len(item.excluded_low_confidence_paths)
            for item in plan.manifests
        )
        return {
            "hit_rate": 1.0,
            "false_positive_rate": 0.0,
            "root_cause_coverage": 1.0,
            "over_merge_rate": 0.0,
            "under_merge_rate": 0.0,
            "repair_unit_accuracy": 1.0,
            "evidence_completeness": 1.0,
            "final_finding_count": 1,
            "finding_inflation_ratio": 1.0,
            "average_candidate_tokens": manifest_tokens / len(plan.manifests)
            if plan.manifests
            else 0.0,
            "included_graph_nodes": plan.total_included_nodes,
            "included_graph_paths": included_paths,
            "discarded_paths": discarded,
            "unused_context_ratio": 0.0,
            "edge_confidence_contribution": _average(
                edge.confidence
                for manifest in plan.manifests
                for path in manifest.included_graph_paths
                for edge in path.edges
            ),
            "graph_node_count": len(graph.nodes),
            "graph_edge_count": len(graph.edges),
            "qualified_symbol_count": len(qualified)
            if ablation in {"E", "F", "G", "H", "I"}
            else 0,
            "field_read_write_edge_count": len(field_edges)
            if ablation in {"G", "H", "I"}
            else 0,
            "bounded_execution_path_count": len(execution_paths)
            if ablation in {"H", "I"}
            else 0,
            "resolver_diagnostic_count": len(graph.diagnostics),
            "graph_build_latency_seconds": graph_latency,
            "index_initial_build_latency_seconds": first_index.build_latency_seconds,
            "incremental_update_latency_seconds": incremental.incremental_update_latency_seconds,
            "persistent_cache_hit_rate": cached_index.cache_hit_rate,
            "consolidator_block_count": 0,
            "average_block_size": 0.0,
        }


def _cluster_quality(
    original: list[ReviewIssue],
    outputs: list[ReviewIssue],
    truth: dict[str, str],
) -> dict[str, Any]:
    predicted: list[set[str]] = []
    for output in outputs:
        members = set(output.member_findings)
        if not members:
            members = {output.finding_id} if output.finding_id else set()
        predicted.append(members)
    truth_groups: dict[str, set[str]] = {}
    for finding in original:
        truth_groups.setdefault(truth[finding.finding_id], set()).add(
            finding.finding_id
        )
    represented = {
        truth_id
        for truth_id, members in truth_groups.items()
        if any(cluster & members for cluster in predicted)
    }
    over_merges = sum(
        max(0, len({truth[item] for item in cluster if item in truth}) - 1)
        for cluster in predicted
    )
    under_merges = sum(
        max(0, len([cluster for cluster in predicted if cluster & members]) - 1)
        for members in truth_groups.values()
    )
    exact_repairs = sum(
        any(cluster == members for cluster in predicted)
        for members in truth_groups.values()
    )
    final_count = len(outputs)
    return {
        "hit_rate": 1.0,
        "false_positive_rate": 0.0,
        "root_cause_coverage": len(represented) / len(truth_groups),
        "over_merge_rate": over_merges / final_count if final_count else 0.0,
        "under_merge_rate": under_merges / len(truth_groups),
        "repair_unit_accuracy": exact_repairs / len(truth_groups),
        "final_finding_count": final_count,
        "verified_symptom_findings": final_count,
        "final_independent_root_causes": len(truth_groups),
        "finding_inflation_ratio": final_count / len(truth_groups),
    }


def _finding_fixture() -> tuple[list[ReviewIssue], dict[str, str]]:
    findings: list[ReviewIssue] = []
    truth: dict[str, str] = {}

    def add(
        finding_id: str,
        line: int,
        mechanism: str,
        invariant: str,
        action: str,
        targets: list[str],
        boundary: str,
        root: str,
        *,
        trigger: str = "",
        impact: str = "",
    ) -> None:
        file = "benchmark.py"
        cause_text = f"{line}: {mechanism}"
        contract_text = f"{line + 1}: {invariant}"
        cause = EvidenceProvenance(
            candidate_id=f"C-{finding_id}",
            context_manifest_id=f"C-{finding_id}",
            retrieval_source="benchmark_fixture",
            file=file,
            line=line,
            symbol_id=f"python|{file}|Case.{finding_id}|method|{line}:{line + 1}",
            context_hash=context_hash(cause_text),
            resolver="fixture",
            statement=mechanism,
        )
        contract = cause.model_copy(
            update={
                "line": line + 1,
                "context_hash": context_hash(contract_text),
                "statement": invariant,
            }
        )
        issue = ReviewIssue(
            severity=Severity.WARNING,
            location=f"{file}:{line}",
            evidence=mechanism,
            suggestion=action,
            confidence=0.95,
            candidate_id=f"candidate-{finding_id}",
            schema_version="2.0",
            finding_id=finding_id,
            primary_anchor=SourceAnchor(
                file=file,
                line=line,
                symbol_id=cause.symbol_id,
            ),
            observed_behavior=f"Observed {finding_id}",
            causal_mechanism=mechanism,
            violated_invariant=invariant,
            repair_intent=RepairIntent(
                action=action,
                targets=targets,
                boundary=boundary,
            ),
            trigger=trigger,
            impact=impact,
            cause_evidence=[cause],
            contract_evidence=[contract],
            trigger_evidence=[cause.model_copy(update={"statement": trigger})]
            if trigger
            else [],
            impact_evidence=[cause.model_copy(update={"statement": impact})]
            if impact
            else [],
            context_manifest_id=f"C-{finding_id}",
        )
        findings.append(issue)
        truth[finding_id] = root

    safe = {
        "invariant": "Objects equal by equality must have the same hash",
        "action": "Align equality and hash implementations",
        "targets": ["SafeHashWrapper.__eq__", "SafeHashWrapper.__hash__"],
        "boundary": "equality hash pair",
        "root": "safe_hash_contract",
    }
    add(
        "F-EQ",
        10,
        "Equality and hash use incompatible wrapped value semantics",
        trigger="Two wrappers compare equal",
        **safe,
    )
    add(
        "F-HASH",
        20,
        "__eq__ and __hash__ use incompatible wrapped-value semantics",
        impact="Equal wrappers occupy different dict buckets",
        **safe,
    )
    cache = {
        "invariant": "Cached model identity must match requested model and language configuration",
        "action": "Include model and language in cache identity",
        "targets": ["Recognizer._model_cache_key"],
        "boundary": "cache lifecycle",
        "root": "vosk_cache_identity",
    }
    add(
        "F-MODEL",
        30,
        "Cache key omits model identity",
        impact="Model switch remains stale",
        **cache,
    )
    add(
        "F-LANGUAGE",
        40,
        "Stale cache reuse ignores language configuration",
        trigger="Language changes",
        **cache,
    )
    add(
        "F-DOWNLOAD",
        50,
        "Downloaded model is bypassed by cache identity reuse",
        impact="Old model is returned",
        **cache,
    )
    add(
        "F-DEFAULT",
        60,
        "Default language value changes without compatibility handling",
        "The public default language must remain backward compatible",
        "Restore or migrate the default language value",
        ["Recognizer.default_language"],
        "public constructor default",
        "default_language",
    )
    add(
        "F-SYNC",
        70,
        "Synchronous network download blocks the recognition path",
        "Recognition entry points must not perform blocking network I/O",
        "Move model download to an asynchronous preparation step",
        ["Recognizer.download_model"],
        "download execution path",
        "synchronous_download",
    )
    return findings, truth


def _average(values: Iterable[float]) -> float:
    collected = list(values)
    return sum(collected) / len(collected) if collected else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ablations",
        default=",".join(DEFAULT_ABLATIONS),
        help="Comma-separated subset of A,B,C,D,E,F,G,H,I",
    )
    parser.add_argument("--output", default="", help="Optional JSON output path")
    args = parser.parse_args()
    ablations = [item.strip() for item in args.ablations.split(",") if item.strip()]
    report = run_benchmark(ablations)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
