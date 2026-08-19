"""Probe-then-run #15077 A alone under the EXACT 4-iter held-out contract.

Step 1 (EARLY QUOTA / AUTH PROBE): before spending any real budget, make one
tiny model call via ModelClient (retried up to 3x). If the provider keeps
returning AuthenticationError / RateLimitError / ServiceUnavailableError (i.e.
quota exhausted or auth broken), we STOP immediately, change NOTHING in the
report, and exit 2 so the caller can notify the user early.

Step 2 (RUN): only if the probe passes, run #15077 A (agent-search) exactly as
the verified 4-iter contract specifies and REPLACE its placeholder entry in
held_out_report_4iter.json. No other fixture is touched.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# Importing the driver applies the 4-iter contract + cap gate (cap=4 enforced).
import run_heldout_qwen37max_p0_4iter as drv  # noqa: E402

from src.models.client import ModelClient  # noqa: E402
from src.models.exceptions import (  # noqa: E402
    AuthenticationError,
    ModelClientError,
    ModelTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from src.models.schemas import Message, ModelConfig  # noqa: E402

FID = "golden_fastapi_fastapi_pr15077_reverse"
VID = "A-agent-search"
REPORT = drv.REPORT_PATH

PROBE_MSG = "Reply with the single word OK."
PROBE_ATTEMPTS = 3

_INSUFFICIENT = (
    AuthenticationError,
    RateLimitError,
    ServiceUnavailableError,
    ModelTimeoutError,
    ModelClientError,
)


async def probe_quota(settings) -> tuple[bool, str | None]:
    """Return (ok, detail). ok=False means quota/auth is insufficient early."""
    last_detail = None
    for i in range(1, PROBE_ATTEMPTS + 1):
        try:
            client = ModelClient(settings=settings)
            cfg = ModelConfig(model=settings.model_name, max_tokens=2, timeout=20)
            resp = await client.chat(
                [Message(role="user", content=PROBE_MSG)], config=cfg
            )
            _ = resp  # success: provider answered
            print(f"[PROBE] attempt {i}/{PROBE_ATTEMPTS}: PASS")
            return True, None
        except _INSUFFICIENT as e:
            detail = f"{type(e).__name__}: {str(e)[:200]}"
            last_detail = detail
            print(f"[PROBE] attempt {i}/{PROBE_ATTEMPTS}: FAIL -> {detail}")
            await asyncio.sleep(2)
    return False, last_detail


async def run_15077_a() -> dict | None:
    fix = drv._load_fixture(FID)
    variant = drv.VARIANTS[VID]
    priming = await drv._prime(fix) if variant.context_mode == "graph_hybrid" else None
    res = await drv.base_runner.run_single(
        fix, temperature=0.0, review_max_iterations=4, variant=variant
    )
    s = drv._summarize(res, priming)
    return s


def replace_placeholder(summary: dict) -> None:
    d = json.loads(REPORT.read_text(encoding="utf-8"))
    replaced = False
    for i, r in enumerate(d["results"]):
        if r["fixture_id"] == FID and r["variant_id"] == VID:
            d["results"][i] = summary
            replaced = True
            break
    if not replaced:
        d["results"].append(summary)
    d["generated_at"] = drv.datetime.now(drv.UTC).isoformat()
    REPORT.write_text(json.dumps(d, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"[REPORT] #15077 A entry replaced; results={len(d['results'])}")


async def main() -> int:
    assert drv.EFFECTIVE_CAP == 4, "iteration gate failed"
    print("=== EARLY QUOTA/AUTH PROBE (before any paid run) ===")
    ok, detail = await probe_quota(drv.SETTINGS)
    if not ok:
        banner = (
            "\n[STOP] PROVIDER QUOTA / AUTH INSUFFICIENT — aborting BEFORE any "
            "paid run.\n"
            f"  last error: {detail}\n"
            "  No report entry changed. Notify user and wait for instruction.\n"
        )
        sys.stderr.write(banner)
        print(banner)
        return 2

    print("\n=== PROBE PASSED — running #15077 A alone (4-iter contract) ===")
    s = await run_15077_a()
    if (not s) or (not s.get("schema_valid")) or s.get("error"):
        err = (s or {}).get("error") if s else "no summary"
        print(f"[RUN] #15077 A produced placeholder/error: {err} (kept original)")
        return 3
    replace_placeholder(s)
    print(
        f"[RUN] #15077 A OK: matched={s['matched_count']}/{s['expected_count']} "
        f"iter={s['review_iterations']} cap_hit={s['iteration_cap_hit']} "
        f"nat={s['natural_completion']} tok={s['total_tokens']}"
    )
    return 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
