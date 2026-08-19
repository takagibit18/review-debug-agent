"""Held-out execution driver — qwen3.7-max.

Runs the 3 frozen held-out fixtures through eval.runner.run_single in the
two context modes the user requested:

    #6205 reverse   -> Agent Search x1, Graph Warm x1
    #15077 reverse   -> Agent Search x1, Graph Warm x1
    #7374 clean     -> Agent Search x1, Graph Warm x1

Compliance notes:
  * Does NOT touch graph_ab_pilot / eval.runner source (no Runner change).
  * Does NOT modify any fixture file (the `held-out` tag stays; eval.runner
    has no held-out guard, unlike the pilot).
  * Uses eval.runner.run_single directly with explicit EvalVariant objects,
    fixture-paired ordering (A then B2 per fixture).

Runtime contract: model / provider / budgets come from get_settings() (.env),
audited below. No YAML shared-block override is applied (this harness path
does not read the pilot YAML).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# Ensure diff-first changed-files behavior (run_single also hardcodes this).
os.environ.setdefault("REVIEW_DIFF_FIRST_CHANGED_FILES", "true")

from eval.runner import run_single  # noqa: E402
from eval.schemas import EvalVariant, Fixture  # noqa: E402
from src.config import get_settings  # noqa: E402

TARGETS: list[str] = [
    "golden_pydantic_pydantic-ai_pr6205_reverse",
    "golden_fastapi_fastapi_pr15077_reverse",
    "golden_pydantic_pydantic-ai_pr7374",
]

VARIANTS: dict[str, EvalVariant] = {
    "A-agent-search": EvalVariant(
        id="A-agent-search", context_mode="agent_search", graph_cache_mode="disabled"
    ),
    "B2-graph-hybrid-warm": EvalVariant(
        id="B2-graph-hybrid-warm", context_mode="graph_hybrid", graph_cache_mode="warm"
    ),
}

RUN_ORDER: list[str] = ["A-agent-search", "B2-graph-hybrid-warm"]
OUTPUT_DIR = REPO / "eval" / "outputs" / "held-out-qwen37max"


def _load_fixture(fid: str) -> Fixture:
    path = REPO / "eval" / "fixtures" / f"{fid}.json"
    return Fixture.model_validate_json(path.read_text(encoding="utf-8"))


def _summarize(res) -> dict:
    d = res.model_dump(mode="json")
    exp = d.get("expected_count", 0) or 0
    matched = d.get("matched_count", 0) or 0
    actual = d.get("actual_count", 0) or 0
    fp = d.get("false_positive_count", 0) or 0
    if exp > 0:
        hit_rate = matched / exp
        pass_at_k = 1.0 if matched >= exp else 0.0
    else:
        # clean control: pass == produced zero findings (no false positives)
        hit_rate = 1.0 if actual == 0 else 0.0
        pass_at_k = 1.0 if actual == 0 else 0.0
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
        "over_merge_count": d.get("over_merge_count", 0) or 0,
        "under_merge_count": d.get("under_merge_count", 0) or 0,
        "final_finding_count": d.get("final_finding_count", 0) or 0,
        "latency_seconds": round(d.get("latency_seconds", 0.0) or 0.0, 2),
        "total_tokens": d.get("total_tokens", 0) or 0,
        "budget_exhausted": d.get("budget_exhausted"),
        "budget_state": d.get("budget_state"),
        "finish_reasons": d.get("finish_reasons"),
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


async def main() -> int:
    s = get_settings()
    print("=" * 72)
    print("HELD-OUT EXECUTION — qwen3.7-max (eval.runner path)")
    print("=" * 72)
    print("EFFECTIVE RUNTIME CONTRACT (get_settings):")
    print(f"  model_name                = {s.model_name!r}")
    print(f"  model_provider            = {s.model_provider!r}")
    print(f"  token_budget              = {s.token_budget}")
    print(f"  token_hard_budget         = {s.token_hard_budget}")
    print(f"  review_max_iterations     = {s.review_max_iterations}")
    print(f"  model_max_tokens          = {s.model_max_tokens}")
    print(f"  agent_max_tool_calls      = {s.agent_max_tool_calls}")
    print(f"  model_request_timeout_s   = {s.model_request_timeout_seconds}")
    print(f"  agent_run_timeout_s       = {s.agent_run_timeout_seconds}")
    print(f"  prompt_input_token_budget = {s.prompt_input_token_budget}")
    print(
        "  api_key_present           = "
        f"{bool(s.openai_api_key)} (never printed; "
        f"base_url={s.openai_base_url!r})"
    )
    print("=" * 72)

    fixtures = {}
    for fid in TARGETS:
        try:
            fixtures[fid] = _load_fixture(fid)
            print(f"loaded fixture: {fid}")
        except Exception as e:  # noqa: BLE001
            print(f"FAILED to load fixture {fid}: {e!r}")
            return 2

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    plan = [(fid, vid) for fid in TARGETS for vid in RUN_ORDER]
    total = len(plan)
    print(f"\nPLAN: {total} runs (fixture-paired A->B2):")
    for i, (fid, vid) in enumerate(plan, 1):
        print(f"  [{i}/{total}] {fid}  {vid}")

    for i, (fid, vid) in enumerate(plan, 1):
        fix = fixtures[fid]
        variant = VARIANTS[vid]
        print("\n" + "-" * 72)
        print(f"[{i}/{total}] START  {fid}  {vid}")
        print("-" * 72)
        try:
            res = await run_single(
                fix,
                temperature=0.0,
                review_max_iterations=3,
                variant=variant,
            )
            summary = _summarize(res)
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
            f"schema_valid={summary.get('schema_valid')} "
            f"exp={summary.get('expected_count')} "
            f"act={summary.get('actual_count')} "
            f"matched={summary.get('matched_count')} "
            f"fp={summary.get('false_positive_count')} "
            f"lat={summary.get('latency_seconds')}s "
            f"err={summary.get('error')}"
        )
        # incremental save so partial progress survives interruptions
        _save(results)

    print("\n" + "=" * 72)
    print("FINAL MATRIX")
    print("=" * 72)
    _print_matrix(results)
    _save(results)
    print(f"\nReport written to: {OUTPUT_DIR / 'held_out_report.json'}")
    return 0


def _print_matrix(results: list[dict]) -> None:
    header = (
        f"{'fixture':<42} {'variant':<22} {'schema':<6} "
        f"{'exp':>3} {'act':>3} {'mat':>3} {'fp':>3} "
        f"{'rc/exp':>6} {'rc/mat':>6} {'pass@k':>6} {'lat_s':>7}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{str(r.get('fixture_id')):<42} {str(r.get('variant_id')):<22} "
            f"{str(r.get('schema_valid')):<6} "
            f"{str(r.get('expected_count')):>3} {str(r.get('actual_count')):>3} "
            f"{str(r.get('matched_count')):>3} {str(r.get('false_positive_count')):>3} "
            f"{str(r.get('expected_root_cause_count')):>3}/{str(r.get('matched_root_cause_count')):>3} "
            f"{str(r.get('pass_at_k')):>6} {str(r.get('latency_seconds')):>7}"
        )


def _save(results: list[dict]) -> None:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model_name": get_settings().model_name,
        "model_provider": get_settings().model_provider,
        "run_order": RUN_ORDER,
        "targets": TARGETS,
        "results": results,
    }
    (OUTPUT_DIR / "held_out_report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8"
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
