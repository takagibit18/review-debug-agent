"""Single-run verification for #15077 A after the enable_thinking compat fix.
Goal: confirm the NEW model's submit path (enable_thinking=True + forced
tool_choice=submit_review) actually completes a real review (schema_valid,
no placeholder) instead of 400-ing at finalize.

This is a verification step ONLY — it replaces the #15077 A placeholder in the
report if successful, otherwise leaves the report untouched.
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
    assert drv.EFFECTIVE_CAP == 4
    print(f"model under test: {drv.SETTINGS.model_name}")
    print("--- golden_fastapi_fastapi_pr15077_reverse / A-agent-search (VERIFY) ---")
    s = await run_one("golden_fastapi_fastapi_pr15077_reverse", "A-agent-search")
    if (not s) or (not s.get("schema_valid")) or s.get("error"):
        err = (s or {}).get("error") if s else "no summary"
        print(f"  STILL FAILED/placeholder: {err}")
        print("  Report UNCHANGED. Fix did not resolve the submit path.")
        return 2
    replace_entry(s)
    print(
        f"  VERIFY OK: matched={s['matched_count']}/{s['expected_count']} "
        f"final_findings={s['final_finding_count']} FP={s['false_positive_count']} "
        f"iter={s['review_iterations']} cap_hit={s['iteration_cap_hit']} "
        f"nat={s['natural_completion']} tok={s['total_tokens']} budget={s['budget_state']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
