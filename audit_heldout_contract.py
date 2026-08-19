"""HELD-OUT RUNTIME CONTRACT AUDIT (deterministic, NO paid model calls).

Loads eval/variants/graph-ab-qwen-heldout.yaml, runs the real
_apply_runtime_contract(config) exactly like the pilot would, then builds a
FRESH get_settings() AFTER the apply and prints every contract field.

This is the section-six/seven hard gate: effective settings must equal the
frozen relaxed contract before any paid run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402
from eval.graph_ab_pilot import _apply_runtime_contract  # noqa: E402
from src.config import get_settings  # noqa: E402

YAML_PATH = REPO / "eval" / "variants" / "graph-ab-qwen-heldout.yaml"

# Frozen relaxed contract (from the user's spec, section two/section seven).
FROZEN = {
    "model_name": "qwen3.7-max-2026-05-20",   # doc; DEAD model -> we use qwen3.7-max
    "model_max_tokens": 4096,
    "prompt_input_token_budget": 12000,
    "token_budget": 60000,
    "token_hard_budget": 80000,
    "final_submit_reserve_tokens": 12000,
    "final_submit_prompt_token_budget": 4000,
    "final_submit_feedback_token_budget": 1200,
    "review_max_iterations": 3,       # requested
    "agent_max_tool_calls": 64,
    "model_request_timeout_seconds": 180.0,
    "agent_tool_timeout_seconds": 30.0,
    "agent_run_timeout_seconds": 600.0,
    "model_provider": "dashscope",
}


def main() -> int:
    config = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    print("CONFIG_PATH =", YAML_PATH)
    print("runtime_contract_source =", config.get("runtime_contract_source"))
    assert config.get("runtime_contract_source") == "current", "must be current"

    # PRE-apply baseline (to show what .env/defaults would give WITHOUT contract)
    before = get_settings()

    # THE apply — mirrors the pilot exactly
    _apply_runtime_contract(config)

    # FRESH settings AFTER apply (the gate)
    s = get_settings()

    rows = [
        ("model_name", s.model_name, FROZEN["model_name"]),
        ("model_provider", s.model_provider, FROZEN["model_provider"]),
        ("model_max_tokens", s.model_max_tokens, FROZEN["model_max_tokens"]),
        ("prompt_input_token_budget", s.prompt_input_token_budget, FROZEN["prompt_input_token_budget"]),
        ("token_budget", s.token_budget, FROZEN["token_budget"]),
        ("token_hard_budget", s.token_hard_budget, FROZEN["token_hard_budget"]),
        ("final_submit_reserve_tokens", s.final_submit_reserve_tokens, FROZEN["final_submit_reserve_tokens"]),
        ("final_submit_prompt_token_budget", s.final_submit_prompt_token_budget, FROZEN["final_submit_prompt_token_budget"]),
        ("final_submit_feedback_token_budget", s.final_submit_feedback_token_budget, FROZEN["final_submit_feedback_token_budget"]),
        ("review_max_iterations(requested)", s.review_max_iterations, FROZEN["review_max_iterations"]),
        ("agent_max_tool_calls", s.agent_max_tool_calls, FROZEN["agent_max_tool_calls"]),
        ("model_request_timeout_seconds", s.model_request_timeout_seconds, FROZEN["model_request_timeout_seconds"]),
        ("agent_tool_timeout_seconds", s.agent_tool_timeout_seconds, FROZEN["agent_tool_timeout_seconds"]),
        ("agent_run_timeout_seconds", s.agent_run_timeout_seconds, FROZEN["agent_run_timeout_seconds"]),
    ]

    print("\n" + "=" * 78)
    print("PRE-APPLY BASELINE (what ran WITHOUT contract — caused CASE 1):")
    print(f"  token_budget              = {before.token_budget}")
    print(f"  token_hard_budget         = {before.token_hard_budget}")
    print(f"  prompt_input_token_budget = {before.prompt_input_token_budget}")
    print(f"  model_max_tokens          = {before.model_max_tokens}")
    print("=" * 78)

    print("\nFRESH EFFECTIVE SETTINGS (after _apply_runtime_contract):")
    print(f"  {'field':<34}{'effective':<18}{'frozen':<18}{'match'}")
    print("-" * 78)
    all_ok = True
    for name, eff, frozen in rows:
        try:
            ok = (eff == frozen)
        except Exception:
            ok = False
        all_ok = all_ok and ok
        print(f"  {name:<34}{str(eff):<18}{str(frozen):<18}{'OK' if ok else 'MISMATCH'}")
        if not ok:
            all_ok = False

    # Tool/timeout/iteration caps that clamp requested values
    print("\nEFFECTIVE REVIEW ITERATION CAP CHECK:")
    from eval.runner import _effective_review_max_iterations
    eff_iter = _effective_review_max_iterations(3)
    print(f"  requested_review_max_iterations = 3")
    print(f"  effective_review_max_iterations = {eff_iter}  (cap allowed per spec)")

    print("\nRESULT:", "ALL FIELDS MATCH FROZEN (model aside)" if all_ok else "MISMATCH -> STOP")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
