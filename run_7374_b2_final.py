"""Complete the single missing cell: #7374 clean B2-graph-hybrid-warm.

The prior run_remaining_3.py finished the model run (event_log afc28527) but
crashed on a Windows PermissionError while quarantining the OLD placeholder
log, so the validated result was never written into the report. This script
re-runs #7374 B2 and replaces the report entry, tolerating a locked old log.
No experimental params change: model qwen3.7-max-2026-05-17, 4-iter ceiling,
identical relaxed contract.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
import run_heldout_qwen37max_p0_4iter as drv  # applies contract + cap=4 gate

REPORT = drv.REPORT_PATH
FID = "golden_pydantic_pydantic-ai_pr7374"
VID = "B2-graph-hybrid-warm"


async def run_one():
    fix = drv._load_fixture(FID)
    variant = drv.VARIANTS[VID]
    priming = await drv._prime(fix) if variant.context_mode == "graph_hybrid" else None
    res = await drv.base_runner.run_single(
        fix, temperature=0.0, review_max_iterations=4, variant=variant
    )
    return drv._summarize(res, priming)


def replace_entry(summary: dict) -> None:
    d = json.loads(REPORT.read_text(encoding="utf-8"))
    old_log = next(
        (r.get("event_log_path") for r in d["results"]
         if r["fixture_id"] == FID and r["variant_id"] == VID),
        None,
    )
    replaced = False
    for i, r in enumerate(d["results"]):
        if r["fixture_id"] == FID and r["variant_id"] == VID:
            d["results"][i] = summary
            replaced = True
            break
    if not replaced:
        d["results"].append(summary)
    if old_log and os.path.isfile(old_log):
        dst = old_log[: -len(".jsonl")] + ".PREV.jsonl"
        try:
            os.rename(old_log, dst)
            print(f"  quarantined previous event_log -> {os.path.basename(dst)}")
        except PermissionError:
            # Old log is locked by an external handle (IDE/AV); keep it.
            print(f"  WARN: could not quarantine locked old log (kept): {os.path.basename(old_log)}")
    d["generated_at"] = drv.datetime.now(drv.UTC).isoformat()
    REPORT.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"  report updated; results={len(d['results'])}")


async def main() -> int:
    assert drv.EFFECTIVE_CAP == 4, "iteration gate failed"
    print(f"model under test: {drv.SETTINGS.model_name}")
    print(f"=== completing {FID} / {VID} ===")
    s = await run_one()
    if (not s) or (not s.get("schema_valid")) or s.get("error"):
        print(f"  RUN FAILED/placeholder: {(s or {}).get('error')} (kept previous entry)")
        return 1
    replace_entry(s)
    print(
        f"  OK: matched={s['matched_count']}/{s['expected_count']} "
        f"final_findings={s['final_finding_count']} FP={s['false_positive_count']} "
        f"iter={s['review_iterations']} cap_hit={s['iteration_cap_hit']} "
        f"nat={s['natural_completion']} tok={s['total_tokens']} budget={s['budget_state']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
