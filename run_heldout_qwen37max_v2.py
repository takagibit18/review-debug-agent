"""Held-out final validation driver — CORRECTED (applies relaxed runtime contract).

Compliance (per the audit protocol):
  * Loads eval/variants/graph-ab-qwen-heldout.yaml (runtime_contract_source=current)
    and runs the REAL _apply_runtime_contract() BEFORE any measured run, so the
    effective settings equal the frozen relaxed contract (60k/80k/12k reserve/
    4k final prompt/1.2k feedback/4k out/64 tools/3 iters/180-30-600 timeouts).
  * Does NOT modify eval.runner / eval.graph_ab_pilot / fixtures / prompts / graph.
  * Reuses the pilot's deterministic GraphHybridContextStrategy priming so each
    B2 measured run hits a warm cache (cache_hit=true); priming latency is
    recorded separately and never consumes Reviewer token budget.
  * The previous INVALID_RUNTIME_ATTEMPT runs (narrow-default budget) are kept as
    diagnostics in held_out_report.json / event_logs and are NOT part of the
    valid 6-run sample. This driver writes held_out_report_v2.json.

Order (fixture-paired A -> B2):
  #6205 reverse  A x1, B2 x1
  #15077 reverse A x1, B2 x1
  #7374 clean    A x1, B2 x1   (clean negative: expect 0 findings / 0 false positives)
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402
import eval.runner as base_runner  # noqa: E402
from eval.graph_ab_pilot import _apply_runtime_contract  # noqa: E402
from eval.schemas import EvalVariant, Fixture  # noqa: E402
from src.analyzer.context_strategy import GraphHybridContextStrategy  # noqa: E402
from src.analyzer.schemas import ReviewRequest  # noqa: E402
from src.config import get_settings  # noqa: E402

YAML_PATH = REPO / "eval" / "variants" / "graph-ab-qwen-heldout.yaml"
CONFIG = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
# Apply the contract FIRST (so every later fresh get_settings() is relaxed).
_apply_runtime_contract(CONFIG)
SETTINGS = get_settings()  # fresh, post-apply

TARGETS: list[str] = [
    "golden_pydantic_pydantic-ai_pr6205_reverse",
    "golden_fastapi_fastapi_pr15077_reverse",
    "golden_pydantic_pydantic-ai_pr7374",
]
VARIANTS = {
    "A-agent-search": EvalVariant(id="A-agent-search", context_mode="agent_search", graph_cache_mode="disabled"),
    "B2-graph-hybrid-warm": EvalVariant(id="B2-graph-hybrid-warm", context_mode="graph_hybrid", graph_cache_mode="warm"),
}
RUN_ORDER = ["A-agent-search", "B2-graph-hybrid-warm"]
OUTPUT_DIR = REPO / "eval" / "outputs" / "held-out-qwen37max"
REPORT_PATH = OUTPUT_DIR / "held_out_report_v2.json"


def _load_fixture(fid: str) -> Fixture:
    return Fixture.model_validate_json((REPO / "eval" / "fixtures" / f"{fid}.json").read_text(encoding="utf-8"))


def _extract_cache_hit(event_log_path: str | None) -> bool | None:
    if not event_log_path:
        return None
    p = Path(event_log_path)
    if not p.is_file():
        return None
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if isinstance(o, dict) and o.get("cache_hit") is True:
            return True
        if isinstance(o, dict):
            g = o.get("graph") or {}
            if isinstance(g, dict) and g.get("cache_hit") is True:
                return True
    return False


def _summarize(res, priming_seconds: float | None = None) -> dict:
    d = res.model_dump(mode="json")
    exp = d.get("expected_count", 0) or 0
    matched = d.get("matched_count", 0) or 0
    actual = d.get("actual_count", 0) or 0
    fp = d.get("false_positive_count", 0) or 0
    if exp > 0:
        hit_rate = matched / exp
        pass_at_k = 1.0 if matched >= exp else 0.0
    else:
        hit_rate = 1.0 if actual == 0 else 0.0
        pass_at_k = 1.0 if actual == 0 else 0.0
    cache_hit = _extract_cache_hit(d.get("event_log_path"))
    return {
        "fixture_id": d.get("fixture_id"),
        "variant_id": d.get("variant_id"),
        "context_mode": d.get("context_mode"),
        "graph_cache_mode": d.get("graph_cache_mode"),
        "schema_valid": d.get("schema_valid"),
        "expected_count": exp,
        "actual_count": actual,
        "matched_count": matched,
        "false_positive_count": fp,
        "expected_root_cause_count": d.get("expected_root_cause_count", 0) or 0,
        "matched_root_cause_count": d.get("matched_root_cause_count", 0) or 0,
        "final_finding_count": d.get("final_finding_count", 0) or 0,
        "latency_seconds": round(d.get("latency_seconds", 0.0) or 0.0, 2),
        "total_tokens": d.get("total_tokens", 0) or 0,
        "budget_exhausted": d.get("budget_exhausted"),
        "budget_state": d.get("budget_state"),
        "finish_reasons": d.get("finish_reasons"),
        "graph_cache_hit": cache_hit,
        "priming_seconds": round(priming_seconds, 2) if priming_seconds is not None else None,
        "placeholder_summary": d.get("placeholder_summary"),
        "submit_review_seen_any": d.get("submit_review_seen_any"),
        "workflow_invalid": d.get("workflow_invalid"),
        "workflow_missing_steps": d.get("workflow_missing_steps"),
        "error": d.get("error"),
        "event_log_path": d.get("event_log_path"),
        "run_id": d.get("run_id"),
        "hit_rate": round(hit_rate, 3),
        "pass_at_k": round(pass_at_k, 3),
    }


async def _prime(fix: Fixture) -> float:
    """Deterministic cold graph build to the shared warm cache (no LLM)."""
    index_path = base_runner._eval_relation_graph_index_path(fix)
    with tempfile.TemporaryDirectory(prefix="eval-prime-") as td:
        repo_root = await asyncio.to_thread(
            base_runner._prepare_fixture_workspace,
            fix,
            Path(td) / "repo",
            workspace_cache_dir=Path(SETTINGS.eval_workspace_cache_dir),
        )
        await asyncio.to_thread(base_runner._validate_diff_added_lines_against_workspace, fix, repo_root)
        await asyncio.to_thread(base_runner._validate_expected_locations_against_diff, fix, repo_root)
        original_diff = fix.input.diff_text or ""
        started = asyncio.get_event_loop().time()
        primed = await GraphHybridContextStrategy(
            settings=SETTINGS,
            workspace_root=repo_root,
            relation_graph_index_path=index_path,
        ).prepare(
            ReviewRequest(
                repo_path=str(repo_root),
                diff_mode=bool(original_diff),
                diff_text=original_diff,
                verbose=False,
            )
        )
        elapsed = asyncio.get_event_loop().time() - started
        tele = primed.graph_telemetry
        assert tele.get("graph_status") == "ready", tele
        assert tele.get("graph_cache_mode") == "cold", tele
        assert tele.get("cache_hit") is False, tele
        assert index_path.is_file(), "warm index not created"
        return elapsed


async def main() -> int:
    print("=" * 78)
    print("HELD-OUT FINAL VALIDATION — relaxed contract applied")
    print("=" * 78)
    print(f"model_name                = {SETTINGS.model_name}")
    print(f"model_provider            = {SETTINGS.model_provider}")
    print(f"token_budget / hard       = {SETTINGS.token_budget} / {SETTINGS.token_hard_budget}")
    print(f"prompt_input_token_budget = {SETTINGS.prompt_input_token_budget}")
    print(f"model_max_tokens          = {SETTINGS.model_max_tokens}")
    print(f"review_max_iterations     = {SETTINGS.review_max_iterations} (effective cap may clamp)")
    print(f"agent_max_tool_calls      = {SETTINGS.agent_max_tool_calls}")
    print("=" * 78)

    fixtures = {fid: _load_fixture(fid) for fid in TARGETS}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    plan = [(fid, vid) for fid in TARGETS for vid in RUN_ORDER]
    total = len(plan)

    for i, (fid, vid) in enumerate(plan, 1):
        fix = fixtures[fid]
        variant = VARIANTS[vid]
        print("\n" + "-" * 78)
        print(f"[{i}/{total}] START  {fid}  {vid}")
        priming_seconds = None
        if variant.context_mode == "graph_hybrid":
            print("  priming graph index (deterministic, no LLM) ...")
            priming_seconds = await _prime(fix)
            print(f"  priming done in {priming_seconds:.1f}s (cache will be warm)")
        try:
            res = await base_runner.run_single(
                fix,
                temperature=0.0,
                review_max_iterations=3,
                variant=variant,
            )
            summary = _summarize(res, priming_seconds)
        except Exception as e:  # noqa: BLE001
            summary = {
                "fixture_id": fid,
                "variant_id": vid,
                "schema_valid": False,
                "error": f"UNCAUGHT: {type(e).__name__}: {e}",
            }
            print(f"[{i}/{total}] UNCAUGHT EXCEPTION: {e!r}")
        results.append(summary)
        print(
            f"[{i}/{total}] DONE   {fid}  {vid}  -> "
            f"schema={summary.get('schema_valid')} exp={summary.get('expected_count')} "
            f"act={summary.get('actual_count')} matched={summary.get('matched_count')} "
            f"fp={summary.get('false_positive_count')} budget={summary.get('budget_state')} "
            f"cache_hit={summary.get('graph_cache_hit')} err={summary.get('error')}"
        )
        _save(results)

    print("\n" + "=" * 78)
    print("FINAL MATRIX (valid measured runs)")
    print("=" * 78)
    _print_matrix(results)
    _save(results)
    print(f"\nReport written to: {REPORT_PATH}")
    return 0


def _print_matrix(results: list[dict]) -> None:
    header = (
        f"{'fixture':<42} {'variant':<22} {'schema':<6} {'exp':>3} {'act':>3} "
        f"{'mat':>3} {'fp':>3} {'rc/m':>5} {'pass@k':>6} {'cache':>6} {'lat_s':>7}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{str(r.get('fixture_id')):<42} {str(r.get('variant_id')):<22} "
            f"{str(r.get('schema_valid')):<6} {str(r.get('expected_count')):>3} "
            f"{str(r.get('actual_count')):>3} {str(r.get('matched_count')):>3} "
            f"{str(r.get('false_positive_count')):>3} "
            f"{str(r.get('matched_root_cause_count')):>3}/{str(r.get('expected_root_cause_count')):>3} "
            f"{str(r.get('pass_at_k')):>6} {str(r.get('graph_cache_hit')):>6} "
            f"{str(r.get('latency_seconds')):>7}"
        )


def _save(results: list[dict]) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model_name": SETTINGS.model_name,
        "model_provider": SETTINGS.model_provider,
        "note": "VALID measured runs. Prior held_out_report.json runs are INVALID_RUNTIME_ATTEMPT (narrow default budget).",
        "run_order": RUN_ORDER,
        "targets": TARGETS,
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
