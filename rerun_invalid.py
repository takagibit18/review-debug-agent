"""Rerun ALL auth-failed/invalid runs from held_out_report_4iter.json under the
EXACT 4-iter held-out contract (no parameter change). Each original run that
produced a placeholder review (schema_valid=False / error set) due to provider
AuthenticationError (403 auth_failed) is re-executed once. Successful reruns
REPLACE the placeholder entry in the report; the original INVALID event_log is
quarantined to *.INVALID_AUTH.jsonl (kept for diagnostics, never deleted). If a
rerun still fails (auth), the original placeholder is kept and the failure is
recorded in the report note so we can diagnose the key.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, os.getcwd())
import run_heldout_qwen37max_p0_4iter as drv  # sets cap=4 + gate at import

REPORT = drv.REPORT_PATH


async def rerun_one(fid: str, vid: str):
    fix = drv._load_fixture(fid)
    variant = drv.VARIANTS[vid]
    priming = await drv._prime(fix) if variant.context_mode == "graph_hybrid" else None
    try:
        res = await drv.base_runner.run_single(
            fix, temperature=0.0, review_max_iterations=4, variant=variant
        )
    except Exception as e:  # noqa: BLE001
        return None, f"EXC {type(e).__name__}: {e}"
    s = drv._summarize(res, priming)
    if (not s.get("schema_valid")) or s.get("error"):
        return None, f"PLACEHOLDER: {s.get('error')}"
    return s, None


async def main() -> None:
    assert drv.EFFECTIVE_CAP == 4, "iteration gate failed"
    d = json.loads(REPORT.read_text(encoding="utf-8"))
    invalid = [
        (r["fixture_id"], r["variant_id"], r.get("event_log_path"))
        for r in d["results"]
        if (not r.get("schema_valid")) or r.get("error")
    ]
    print(f"invalid runs to rerun ({len(invalid)}):")
    for f, v, _ in invalid:
        print(f"  {f} {v}")
    outcomes = {}
    for fid, vid, old_log in invalid:
        print(f"\n=== rerun {fid} {vid} ===")
        s, err = await rerun_one(fid, vid)
        if s:
            for i, r in enumerate(d["results"]):
                if r["fixture_id"] == fid and r["variant_id"] == vid:
                    d["results"][i] = s
                    break
            if old_log and os.path.isfile(old_log):
                dst = old_log[: -len(".jsonl")] + ".INVALID_AUTH.jsonl"
                os.rename(old_log, dst)
                print(f"  quarantined original -> {os.path.basename(dst)}")
            print(
                f"  OK schema={s['schema_valid']} matched={s['matched_count']}/"
                f"{s['expected_count']} iter={s['review_iterations']} "
                f"cap_hit={s['iteration_cap_hit']} nat={s['natural_completion']} "
                f"tok={s['total_tokens']}"
            )
            outcomes[f"{fid}/{vid}"] = "OK"
        else:
            print(f"  FAILED: {err}")
            outcomes[f"{fid}/{vid}"] = err
    d["status"] = "VALID_4ITER_MEASURED (auth-failed placeholders rerun attempted)"
    d["note"] += " | rerun outcomes: " + json.dumps(outcomes, ensure_ascii=False)
    d["generated_at"] = drv.datetime.now(drv.UTC).isoformat()
    REPORT.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"\nreport updated: {len(d['results'])} results; outcomes={outcomes}")


if __name__ == "__main__":
    asyncio.run(main())
