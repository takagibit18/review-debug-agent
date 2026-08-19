"""Run the remaining 3 measured runs for #15077 / #7374 on the new model
qwen3.7-max-2026-05-17, after the enable_thinking + tool_choice compat fix.

#15077 A is already a valid run (verified separately) and is NOT re-run here.
Targets (3 measured runs):
  #15077 reverse  B2-graph-hybrid-warm
  #7374 clean     A-agent-search
  #7374 clean     B2-graph-hybrid-warm

Each completed run REPLACES its existing report entry (placeholder or prior
model result); the original event_log is quarantined to *.PREV.jsonl (kept).
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
TARGETS = [
    ("golden_fastapi_fastapi_pr15077_reverse", "B2-graph-hybrid-warm"),
    ("golden_pydantic_pydantic-ai_pr7374", "A-agent-search"),
    ("golden_pydantic_pydantic-ai_pr7374", "B2-graph-hybrid-warm"),
]


async def run_one(fid: str, vid: str):
    fix = drv._load_fixture(fid)
    variant = drv.VARIANTS[vid]
    priming = await drv._prime(fix) if variant.context_mode == "graph_hybrid" else None
    res = await drv.base_runner.run_single(
        fix, temperature=0.0, review_max_iterations=4, variant=variant
    )
    return drv._summarize(res, priming)


def replace_entry(summary: dict) -> None:
    d = json.loads(REPORT.read_text(encoding="utf-8"))
    fid, vid = summary["fixture_id"], summary["variant_id"]
    old_log = next(
        (r.get("event_log_path") for r in d["results"]
         if r["fixture_id"] == fid and r["variant_id"] == vid),
        None,
    )
    replaced = False
    for i, r in enumerate(d["results"]):
        if r["fixture_id"] == fid and r["variant_id"] == vid:
            d["results"][i] = summary
            replaced = True
            break
    if not replaced:
        d["results"].append(summary)
    if old_log and os.path.isfile(old_log):
        dst = old_log[: -len(".jsonl")] + ".PREV.jsonl"
        os.rename(old_log, dst)
        print(f"  quarantined previous event_log -> {os.path.basename(dst)}")
    d["generated_at"] = drv.datetime.now(drv.UTC).isoformat()
    REPORT.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"  report updated; results={len(d['results'])}")


async def main() -> int:
    assert drv.EFFECTIVE_CAP == 4, "iteration gate failed"
    print(f"model under test: {drv.SETTINGS.model_name}")
    print("=== running remaining 3 measured runs (#15077 B2, #7374 A, #7374 B2) ===")
    failures = 0
    for fid, vid in TARGETS:
        print(f"\n--- {fid} / {vid} ---")
        s = await run_one(fid, vid)
        if (not s) or (not s.get("schema_valid")) or s.get("error"):
            err = (s or {}).get("error") if s else "no summary"
            print(f"  RUN FAILED/placeholder: {err} (kept previous entry)")
            failures += 1
            continue
        replace_entry(s)
        print(
            f"  OK: matched={s['matched_count']}/{s['expected_count']} "
            f"final_findings={s['final_finding_count']} FP={s['false_positive_count']} "
            f"iter={s['review_iterations']} cap_hit={s['iteration_cap_hit']} "
            f"nat={s['natural_completion']} tok={s['total_tokens']} budget={s['budget_state']}"
        )
    print(f"\n=== done (failures={failures}) ===")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
