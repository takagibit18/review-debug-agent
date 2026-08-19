"""Rerun ONLY #7374 B2 (graph-hybrid-warm) after the original run hit
AuthenticationError (403 auth_failed x3) and produced a fake 0-finding
(candidate_count=0). Reuses the EXACT 4-iter held-out contract -- no
parameter change. This is an infra/auth-failure redo, permitted by the
protocol (the original run never executed a real review).

Behavior:
  - gate: asserts effective_review_max_iterations == 4 (via importing the
    driver module, which sets EVAL_REVIEW_MAX_ITERATIONS_CAP=4 at import).
  - runs #7374 B2 once with review_max_iterations=4.
  - if run_single raises (e.g. auth still failing), exit 1 WITHOUT touching
    the report or quarantining the original (so we can diagnose the key).
  - on success: quarantine the original INVALID event_log, append the new
    summary to held_out_report_4iter.json, update status/note.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, os.getcwd())

import run_heldout_qwen37max_p0_4iter as drv  # applies 4-iter cap + gate at import

REPORT = drv.REPORT_PATH
STALE_LOG = (
    "eval/outputs/event_logs/"
    "golden_pydantic_pydantic-ai_pr7374_f77e83ef-d429-49c5-bb55-c0a0bc788acd.jsonl"
)
FID = "golden_pydantic_pydantic-ai_pr7374"
VID = "B2-graph-hybrid-warm"


async def main() -> int:
    print(f"effective_review_max_iterations = {drv.EFFECTIVE_CAP} (gate)")
    assert drv.EFFECTIVE_CAP == 4, "iteration ceiling gate failed"

    fix = drv._load_fixture(FID)
    variant = drv.VARIANTS[VID]
    print(f"[rerun] priming graph index for {FID} ...")
    priming = await drv._prime(fix)
    print(f"[rerun] priming done {priming:.1f}s; running {VID} ...")
    try:
        res = await drv.base_runner.run_single(
            fix, temperature=0.0, review_max_iterations=4, variant=variant
        )
    except Exception as e:  # noqa: BLE001
        print(f"[rerun] FAILED: {type(e).__name__}: {e}")
        print("[rerun] NOT merging. Original report untouched; diagnose key.")
        return 1

    summary = drv._summarize(res, priming)
    print(json.dumps(summary, indent=2, ensure_ascii=True))

    # quarantine the INVALID original (fake 0-finding) so it is never confused
    # with the valid rerun.
    stale = Path(STALE_LOG)
    if stale.is_file():
        dest = stale.with_suffix(".INVALID_AUTH.jsonl")
        stale.rename(dest)
        print(f"[rerun] quarantined original INVALID run -> {dest.name}")

    d = json.loads(REPORT.read_text(encoding="utf-8"))
    existing = [(r.get("fixture_id"), r.get("variant_id")) for r in d["results"]]
    if (FID, VID) not in existing:
        d["results"].append(summary)
    d["status"] = "VALID_4ITER_MEASURED (7374 B2 rerun; original auth-failed excluded)"
    d["note"] += (
        " | 7374 B2 original run INVALID_AUTH (auth_failed x3, fake 0-finding); "
        "rerun appended under identical 4-iter contract."
    )
    d["generated_at"] = drv.datetime.now(drv.UTC).isoformat()
    REPORT.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"[rerun] report updated: {len(d['results'])} results")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
