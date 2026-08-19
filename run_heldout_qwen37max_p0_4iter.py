"""Held-out final validation driver — 4-ITER CEILING (the only treatment change).

SINGLE treatment-level change vs run_heldout_qwen37max_v2.py:
  review iteration ceiling  2 -> 4  (requested = 4, effective = 4)

The real clamp lives in base_runner._effective_review_max_iterations(), which
floors the requested value at settings.eval_review_max_iterations_cap (env
EVAL_REVIEW_MAX_ITERATIONS_CAP, default 2). The relaxed YAML `max_iterations: 4`
alone would still be clamped to 2, so this driver sets the eval-only env var
EVAL_REVIEW_MAX_ITERATIONS_CAP=4 BEFORE any get_settings() call. The production
default (2) is never touched.

Everything else is identical to the verified held-out contract:
  60k/80k/12k reserve/4k final prompt/1.2k feedback/4k out/64 tools/
  180-30-600 timeouts/temperature 0.0/model qwen3.7-max.

Order (fixture-paired A -> B2):
  #6205 reverse  A x1, B2 x1
  #15077 reverse A x1, B2 x1
  #7374 clean    A x1, B2 x1   (clean negative: expect 0 findings / 0 false positives)

Per-run iteration-efficiency telemetry (section 13 of the protocol):
  review_iterations, finish_reasons, iteration_cap_hit, natural_completion,
  model_requests, tool_calls, read_file_calls, grep/search/symbol calls,
  total_tokens, agent_run_latency, plus Graph manifest_count / graph_cache_hit.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# --- EVAL-ONLY override: relax the iteration ceiling to 4. ----------------
# Must be set BEFORE get_settings() is first called in this process.
os.environ["EVAL_REVIEW_MAX_ITERATIONS_CAP"] = "4"

import yaml  # noqa: E402
import eval.runner as base_runner  # noqa: E402
from eval.graph_ab_pilot import _apply_runtime_contract  # noqa: E402
from eval.schemas import EvalVariant, Fixture  # noqa: E402
from src.analyzer.context_strategy import GraphHybridContextStrategy  # noqa: E402
from src.analyzer.schemas import ReviewRequest  # noqa: E402
from src.config import get_settings  # noqa: E402

YAML_PATH = REPO / "eval" / "variants" / "graph-ab-qwen-heldout-p0-4iter.yaml"
CONFIG = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
# Apply the contract FIRST (so every later fresh get_settings() is relaxed).
_apply_runtime_contract(CONFIG)
SETTINGS = get_settings()  # fresh, post-apply

# --- Hard gate: refuse any paid call unless the effective ceiling is 4. ----
REQUESTED_CAP = 4
EFFECTIVE_CAP = base_runner._effective_review_max_iterations(REQUESTED_CAP)
if EFFECTIVE_CAP != REQUESTED_CAP:
    sys.stderr.write(
        f"[FATAL] effective_review_max_iterations={EFFECTIVE_CAP} != "
        f"{REQUESTED_CAP}. Refusing to launch paid runs.\n"
    )
    sys.exit(2)

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
OUTPUT_DIR = REPO / "eval" / "outputs" / "held-out-qwen37max-p0-4iter"
REPORT_PATH = OUTPUT_DIR / "held_out_report_4iter.json"


def _load_fixture(fid: str) -> Fixture:
    return Fixture.model_validate_json((REPO / "eval" / "fixtures" / f"{fid}.json").read_text(encoding="utf-8"))


def _count_model_calls(event_log_path: str | None) -> int | None:
    if not event_log_path:
        return None
    p = Path(event_log_path)
    if not p.is_file():
        return None
    n = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if isinstance(o, dict) and o.get("event_type") == "model_call":
            n += 1
    return n


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
    pm = res.process_metrics  # ReviewProcessMetrics (rich, per-run telemetry)
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
    finish_reasons = d.get("finish_reasons") or []
    cap_hit = "max_iterations" in finish_reasons
    natural = not cap_hit and d.get("schema_valid", False)
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
        # --- iteration-efficiency block (section 13) ---
        "review_iterations": pm.review_iterations,
        "finish_reasons": finish_reasons,
        "iteration_cap_hit": "YES" if cap_hit else "NO",
        "natural_completion": "YES" if natural else "NO",
        "model_requests": _count_model_calls(d.get("event_log_path")),
        "tool_calls": pm.tool_call_count,
        "read_file_calls": pm.read_file_calls,
        "grep_calls": pm.grep_calls,
        "symbol_lookup_calls": pm.symbol_lookup_calls,
        "total_tokens": d.get("total_tokens", 0) or 0,
        "agent_run_latency": round(d.get("latency_seconds", 0.0) or 0.0, 2),
        "manifest_count": pm.manifest_count,
        "graph_cache_hit": pm.graph_cache_hit if pm.graph_cache_hit is not None else _extract_cache_hit(d.get("event_log_path")),
        # --- end iteration-efficiency block ---
        "priming_seconds": round(priming_seconds, 2) if priming_seconds is not None else None,
        "budget_exhausted": d.get("budget_exhausted"),
        "budget_state": d.get("budget_state"),
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


def _save(results: list[dict]) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "experiment_id": "graph-ab-qwen-heldout-p0-4iter",
        "status": "VALID_4ITER_MEASURED",
        "model_name": SETTINGS.model_name,
        "model_provider": SETTINGS.model_provider,
        "requested_review_max_iterations": REQUESTED_CAP,
        "effective_review_max_iterations": EFFECTIVE_CAP,
        "note": (
            "VALID 4-iter measured runs. Effective ceiling = 4 via eval-only "
            "EVAL_REVIEW_MAX_ITERATIONS_CAP=4. Prior held_out_report_v2.json runs "
            "are PRE_FREEZE_2ITER_ATTEMPT (effective cap 2) and are EXCLUDED from "
            "this table."
        ),
        "run_order": RUN_ORDER,
        "targets": TARGETS,
        "results": results,
    }
    REPORT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _print_matrix(results: list[dict]) -> None:
    header = (
        f"{'fixture':<42} {'variant':<22} {'Gold':>4} {'iter':>4} {'cap':>4} "
        f"{'nat':>4} {'tools':>5} {'tok':>8} {'lat_s':>7}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{str(r.get('fixture_id')):<42} {str(r.get('variant_id')):<22} "
            f"{str(r.get('expected_count')):>4} {str(r.get('review_iterations')):>4} "
            f"{str(r.get('iteration_cap_hit')):>4} {str(r.get('natural_completion')):>4} "
            f"{str(r.get('tool_calls')):>5} {str(r.get('total_tokens')):>8} "
            f"{str(r.get('agent_run_latency')):>7}"
        )


async def main() -> int:
    print("=" * 78)
    print("HELD-OUT FINAL VALIDATION — 4-ITER CEILING (single treatment change)")
    print("=" * 78)
    print(f"model_name                = {SETTINGS.model_name}")
    print(f"model_provider            = {SETTINGS.model_provider}")
    print(f"token_budget / hard       = {SETTINGS.token_budget} / {SETTINGS.token_hard_budget}")
    print(f"prompt_input_token_budget = {SETTINGS.prompt_input_token_budget}")
    print(f"model_max_tokens          = {SETTINGS.model_max_tokens}")
    print(f"requested_review_iters    = {REQUESTED_CAP}")
    print(f"effective_review_iters    = {EFFECTIVE_CAP}  (gate PASSED)")
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
                review_max_iterations=REQUESTED_CAP,
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
            f"fp={summary.get('false_positive_count')} iter={summary.get('review_iterations')} "
            f"cap_hit={summary.get('iteration_cap_hit')} nat={summary.get('natural_completion')} "
            f"budget={summary.get('budget_state')} cache_hit={summary.get('graph_cache_hit')} "
            f"err={summary.get('error')}"
        )
        _save(results)

    print("\n" + "=" * 78)
    print("FINAL MATRIX (valid 4-iter measured runs)")
    print("=" * 78)
    _print_matrix(results)
    _save(results)
    print(f"\nReport written to: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
