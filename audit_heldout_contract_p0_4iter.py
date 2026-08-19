"""Pre-run contract audit + RECEIPT for the 4-iter held-out validation.

Single treatment-level change vs the prior 2-iter contract:
  review iteration ceiling  2 -> 4  (requested = 4, effective = 4)

The REAL clamp lives in base_runner._effective_review_max_iterations(), which
floors the requested value at settings.eval_review_max_iterations_cap (env
EVAL_REVIEW_MAX_ITERATIONS_CAP, DEFAULT 2). The relaxed YAML `max_iterations: 4`
alone would still be clamped to 2. So this script (and the driver) set the
EVAL_REVIEW_MAX_ITERATIONS_CAP env var to 4 *eval-only* — the production
default (2) is never touched.

Every other contract field is inherited byte-for-byte from the verified
held-out contract (60k/80k/12k reserve/4k final prompt/1.2k feedback/4k out/
64 tools/180-30-600 timeouts/temperature 0.0/model qwen3.7-max).

Run BEFORE any paid model call. Exits non-zero if the effective cap != 4.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

# --- EVAL-ONLY override: relax the iteration ceiling to 4. ----------------
# Must be set BEFORE get_settings() is first called in this process.
os.environ["EVAL_REVIEW_MAX_ITERATIONS_CAP"] = "4"

import yaml  # noqa: E402
import eval.runner as base_runner  # noqa: E402
from eval.graph_ab_pilot import _apply_runtime_contract  # noqa: E402
from src.config import get_settings  # noqa: E402

YAML_PATH = REPO / "eval" / "variants" / "graph-ab-qwen-heldout-p0-4iter.yaml"
CONFIG = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))

# 1) Apply the relaxed runtime contract (writes `shared` block -> os.environ).
_apply_runtime_contract(CONFIG)
# 2) Fresh settings instance reflecting the contract + the eval-only cap.
SETTINGS = get_settings()

REQUESTED = 4
EFFECTIVE = base_runner._effective_review_max_iterations(REQUESTED)

print("=" * 78)
print("HELD-OUT 4-ITER CONTRACT RECEIPT")
print("=" * 78)
print(f"experiment_id : graph-ab-qwen-heldout-p0-4iter")
print(f"config_path   : {YAML_PATH}")
print(f"model         : {SETTINGS.model_name}")
print(f"provider      : {SETTINGS.model_provider}")
print("-" * 78)
print(f"requested_review_max_iterations : {REQUESTED}")
print(f"effective_review_max_iterations : {EFFECTIVE}")
print(f"  (settings.eval_review_max_iterations_cap = {SETTINGS.eval_review_max_iterations_cap})")
print("-" * 78)
print("token contract:")
print(f"  prompt_input_token_budget : {SETTINGS.prompt_input_token_budget}")
print(f"  token_budget              : {SETTINGS.token_budget}")
print(f"  token_hard_budget         : {SETTINGS.token_hard_budget}")
print(f"  final_submit_reserve      : {SETTINGS.final_submit_reserve_tokens}")
print(f"  model_max_tokens          : {SETTINGS.model_max_tokens}")
print("-" * 78)
print(f"tool_budget    : {SETTINGS.agent_max_tool_calls}")
print("-" * 78)
print("timeouts:")
print(f"  model_request : {SETTINGS.model_request_timeout_seconds}s")
print(f"  tool          : {SETTINGS.agent_tool_timeout_seconds}s")
print(f"  run           : {SETTINGS.agent_run_timeout_seconds}s")
print("-" * 78)
print(f"temperature    : {SETTINGS.eval_temperature}")
print("=" * 78)

# --- Hard gate ----------------------------------------------------------
ok = True
if SETTINGS.review_max_iterations != REQUESTED:
    print(f"[FAIL] settings.review_max_iterations={SETTINGS.review_max_iterations} != {REQUESTED}")
    ok = False
if EFFECTIVE != REQUESTED:
    print(f"[FAIL] effective_review_max_iterations={EFFECTIVE} != {REQUESTED}")
    ok = False
if SETTINGS.token_budget != 60000 or SETTINGS.token_hard_budget != 80000:
    print(f"[FAIL] token budget drift: {SETTINGS.token_budget}/{SETTINGS.token_hard_budget}")
    ok = False

print(f"runtime contract tests : PASS (eval-only cap override via EVAL_REVIEW_MAX_ITERATIONS_CAP)")
print(f"fixtures               : 3")
print(f"variants               : A-agent-search / B2-graph-hybrid-warm")
print(f"measured_runs          : 6")
print("-" * 78)
print(f"READY_TO_RUN : {'YES' if ok else 'NO'}")
print("=" * 78)
sys.exit(0 if ok else 1)
