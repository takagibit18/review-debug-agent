"""Run the COMPLETE A/B for fixtures #15077 and #7374 under the new model
qwen3.7-max-2026-05-17 (4-iter relaxed contract, no param change besides the
already-approved model swap).

Targets (4 measured runs):
  #15077 reverse  A-agent-search  x1
  #15077 reverse  B2-graph-warm   x1
  #7374 clean     A-agent-search  x1
  #7374 clean     B2-graph-warm   x1

#6205 entries in the report are left untouched (already valid on the prior
model; out of scope for this request).

EARLY QUOTA/AUTH PROBE: before any paid run, a 1-token ModelClient call is
retried 3x. If the new model keeps returning auth/quota errors, we STOP and
change nothing in the report (exit 2). Only on probe PASS do we run the 4 runs.

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

from src.models.client import ModelClient  # noqa: E402
from src.models.exceptions import (  # noqa: E402
    AuthenticationError,
    ModelClientError,
    ModelTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from src.models.schemas import Message, ModelConfig  # noqa: E402

TARGETS = [
    ("golden_fastapi_fastapi_pr15077_reverse", "A-agent-search"),
    ("golden_fastapi_fastapi_pr15077_reverse", "B2-graph-hybrid-warm"),
    ("golden_pydantic_pydantic-ai_pr7374", "A-agent-search"),
    ("golden_pydantic_pydantic-ai_pr7374", "B2-graph-hybrid-warm"),
]
REPORT = drv.REPORT_PATH
PROBE_ATTEMPTS = 3
PROBE_MSG = "Reply with the single word OK."
_INSUFFICIENT = (
    AuthenticationError,
    RateLimitError,
    ServiceUnavailableError,
    ModelTimeoutError,
    ModelClientError,
)


async def probe_quota(settings) -> tuple[bool, str | None]:
    last = None
    for i in range(1, PROBE_ATTEMPTS + 1):
        try:
            client = ModelClient(settings=settings)
            cfg = ModelConfig(model=settings.model_name, max_tokens=2, timeout=20)
            await client.chat([Message(role="user", content=PROBE_MSG)], config=cfg)
            print(f"[PROBE] attempt {i}/{PROBE_ATTEMPTS}: PASS")
            return True, None
        except _INSUFFICIENT as e:
            last = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"[PROBE] attempt {i}/{PROBE_ATTEMPTS}: FAIL -> {last}")
            await asyncio.sleep(2)
    return False, last


async def run_one(fid: str, vid: str) -> dict | None:
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
    print("=== EARLY QUOTA/AUTH PROBE (new model) ===")
    ok, detail = await probe_quota(drv.SETTINGS)
    if not ok:
        banner = (
            "\n[STOP] PROVIDER QUOTA / AUTH INSUFFICIENT for new model — "
            "aborting BEFORE any paid run.\n"
            f"  last error: {detail}\n  No report entry changed.\n"
        )
        sys.stderr.write(banner)
        print(banner)
        return 2

    print("\n=== PROBE PASSED — running #15077 + #7374 full A/B (4 runs) ===")
    for fid, vid in TARGETS:
        print(f"\n--- {fid} / {vid} ---")
        s = await run_one(fid, vid)
        if (not s) or (not s.get("schema_valid")) or s.get("error"):
            err = (s or {}).get("error") if s else "no summary"
            print(f"  RUN FAILED/placeholder: {err} (kept previous entry)")
            continue
        replace_entry(s)
        print(
            f"  OK: matched={s['matched_count']}/{s['expected_count']} "
            f"iter={s['review_iterations']} cap_hit={s['iteration_cap_hit']} "
            f"nat={s['natural_completion']} tok={s['total_tokens']}"
        )
    print("\n=== done ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
