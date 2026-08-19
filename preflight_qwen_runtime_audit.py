"""Preflight runtime audit for the Qwen joint-analysis experiment.

Goal: prove  DECLARED CONTRACT == EFFECTIVE RUNTIME CONTRACT  before any
paid Qwen API request is made.  Mirrors the contract propagation that
`eval/graph_ab_pilot.py::run_pilot` performs at experiment launch.

NEVER prints API key contents — only `api_key_present=true|false` plus
a structural sanity flag (looks_like_url) that catches the
copy-paste-error where OPENAI_API_KEY was set to the base URL.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from src.config import get_settings  # noqa: E402
from src.models.compat import resolve_model_profile  # noqa: E402
from eval.graph_ab_pilot import _apply_runtime_contract, _load_config  # noqa: E402
import eval.graph_ab_pilot as pilot_mod  # noqa: E402
from src.analyzer import inference_engine as ie_mod  # noqa: E402


def main() -> int:
    config_path = REPO / "eval" / "variants" / "graph-ab-qwen-joint-analysis.yaml"
    config = _load_config(config_path)
    shared = config["shared"]

    print("=" * 72)
    print("PREFLIGHT RUNTIME AUDIT — graph-ab-qwen-joint-analysis")
    print("=" * 72)
    print(f"repo root:              {REPO}")
    print(f"experiment_id:          {config['experiment_id']}")
    print(f"runtime_contract_source:{config['runtime_contract_source']}")
    print(f"formal_graph_ab:        {config['formal_graph_ab']}")
    print(f"held_out_executed:      {config['held_out_executed']}")

    # ----- A. .env provider connection params (sanitized) -------------
    print("\n--- A. .env provider connection params (sanitized) ---")
    for key in ("MODEL_PROVIDER", "MODEL_NAME", "OPENAI_BASE_URL"):
        print(f"  {key}={os.getenv(key, '')!r}")
    ak = os.getenv("OPENAI_API_KEY", "")
    print(f"  OPENAI_API_KEY={'present' if ak else 'MISSING'}")

    # ----- B. .env conflicting overrides (task H) -------------------
    print("\n--- B. .env conflicting runtime-contract overrides (task H) ---")
    conflict_keys = [
        "REVIEW_MAX_ITERATIONS",
        "TOKEN_BUDGET",
        "TOKEN_HARD_BUDGET",
        "PROMPT_INPUT_TOKEN_BUDGET",
        "MODEL_MAX_TOKENS",
        "MODEL_REQUEST_TIMEOUT_SECONDS",
        "AGENT_RUN_TIMEOUT_SECONDS",
        "AGENT_TOOL_TIMEOUT_SECONDS",
        "AGENT_MAX_TOOL_CALLS",
        "FINAL_SUBMIT_RESERVE_TOKENS",
        "FINAL_SUBMIT_PROMPT_TOKEN_BUDGET",
        "FINAL_SUBMIT_FEEDBACK_TOKEN_BUDGET",
    ]
    found_conflicts = []
    for key in conflict_keys:
        val = os.getenv(key)
        if val is not None:
            found_conflicts.append((key, val))
            print(f"  CONFLICT present: {key}={val}")
    if not found_conflicts:
        print("  (no conflicting overrides present in .env;")
        print("   _apply_runtime_contract will set them cleanly at launch)")

    # ----- C. Apply runtime contract (as run_pilot does at line 905) -
    print("\n--- C. Applying runtime contract via _apply_runtime_contract(config) ---")
    _apply_runtime_contract(config)
    print("  _apply_runtime_contract() returned without raising.")

    # ----- D. os.environ after _apply_runtime_contract ---------------
    print("\n--- D. os.environ after _apply_runtime_contract ---")
    expected_env = {
        "MODEL_NAME": "qwen3.6-flash",
        "MODEL_MAX_TOKENS": "4096",
        "PROMPT_INPUT_TOKEN_BUDGET": "12000",
        "TOKEN_BUDGET": "60000",
        "TOKEN_HARD_BUDGET": "80000",
        "FINAL_SUBMIT_RESERVE_TOKENS": "12000",
        "FINAL_SUBMIT_PROMPT_TOKEN_BUDGET": "4000",
        "FINAL_SUBMIT_FEEDBACK_TOKEN_BUDGET": "1200",
        "REVIEW_MAX_ITERATIONS": "3",
        "AGENT_MAX_TOOL_CALLS": "64",
        "MODEL_REQUEST_TIMEOUT_SECONDS": "180.0",
        "AGENT_TOOL_TIMEOUT_SECONDS": "30.0",
        "AGENT_RUN_TIMEOUT_SECONDS": "600.0",
    }
    env_failures = []
    for key, expected in expected_env.items():
        actual = os.environ.get(key)
        status = "OK" if actual == expected else "FAIL"
        print(f"  {key}: expected={expected!r} actual={actual!r} [{status}]")
        if actual != expected:
            env_failures.append((key, expected, actual))

    # ----- E. Explicit REVIEW_MAX_ITERATIONS check (task G) ---------
    print("\n--- E. Explicit REVIEW_MAX_ITERATIONS check (task G) ---")
    rmi = os.environ.get("REVIEW_MAX_ITERATIONS")
    print(f"  os.environ['REVIEW_MAX_ITERATIONS'] = {rmi!r}")
    print(f"  == '3' ? {'YES' if rmi == '3' else 'NO'}")

    # ----- F. Fresh get_settings() effective values (task F) --------
    print("\n--- F. Fresh get_settings() effective values (task F) ---")
    s = get_settings()
    declared_fields = [
        ("model_name", shared["model"]),
        ("model_max_tokens", shared["max_output_tokens"]),
        ("prompt_input_token_budget", shared["prompt_input_token_budget"]),
        ("token_budget", shared["token_budget"]),
        ("token_hard_budget", shared["token_hard_budget"]),
        ("final_submit_reserve_tokens", shared["final_submit_reserve_tokens"]),
        ("final_submit_prompt_token_budget", shared["final_submit_prompt_token_budget"]),
        ("final_submit_feedback_token_budget", shared["final_submit_feedback_token_budget"]),
        ("review_max_iterations", shared["max_iterations"]),
        ("agent_max_tool_calls", shared["tool_budget"]),
        ("model_request_timeout_seconds", shared["model_request_timeout_seconds"]),
        ("agent_tool_timeout_seconds", shared["tool_timeout_seconds"]),
        ("agent_run_timeout_seconds", shared["run_timeout_seconds"]),
    ]
    settings_failures = []
    for field, expected in declared_fields:
        actual = getattr(s, field)
        status = "OK" if actual == expected else "FAIL"
        print(f"  settings.{field}: expected={expected!r} actual={actual!r} [{status}]")
        if actual != expected:
            settings_failures.append((field, expected, actual))

    # ----- G. Provider resolution (task C) ---------------------------
    print("\n--- G. Provider resolution via resolve_model_profile ---")
    profile = resolve_model_profile(s, s.model_name)
    print(f"  settings.model_provider = {s.model_provider!r}")
    print(f"  resolved provider        = {profile.provider!r}")
    print(f"  resolved model           = {profile.model!r}")
    print(f"  thinking_format          = {profile.compat.thinking_format!r}")
    base_url = s.openai_base_url
    base_url_is_dashscope = bool(base_url) and (
        "dashscope" in base_url.lower()
        or "aliyuncs" in base_url.lower()
        or "bailian" in base_url.lower()
    )
    base_url_is_deepseek = bool(base_url) and "deepseek" in base_url.lower()
    print(f"  openai_base_url          = {base_url!r}")
    print(f"  base_url points to DashScope/Bailian endpoint: {base_url_is_dashscope}")
    print(f"  base_url points to deepseek endpoint:          {base_url_is_deepseek}")
    api_key_present = bool(s.openai_api_key)
    # Structural sanity: an API key should not look like the base URL.
    ak_lower = (s.openai_api_key or "").strip().lower()
    looks_like_url = ak_lower.startswith("http://") or ak_lower.startswith("https://")
    looks_like_base_url_copy = bool(base_url) and ak_lower == base_url.strip().lower()
    print(f"  api_key_present          = {api_key_present}")
    print(f"  api_key looks_like_url   = {looks_like_url}")
    print(f"  api_key == base_url      = {looks_like_base_url_copy}")

    provider_failures = []
    if s.model_provider != "dashscope":
        provider_failures.append((
            "model_provider",
            "dashscope",
            s.model_provider,
            "MODEL_PROVIDER not set explicitly in .env; only legacy fallback resolved it",
        ))
    if s.model_name != "qwen3.6-flash":
        provider_failures.append((
            "model_name",
            "qwen3.6-flash",
            s.model_name,
            "runtime contract did not propagate MODEL_NAME",
        ))
    if not base_url_is_dashscope:
        provider_failures.append((
            "openai_base_url",
            "<DashScope OpenAI-compatible endpoint>",
            base_url,
            ".env OPENAI_BASE_URL is not the Bailian/DashScope endpoint",
        ))
    if not api_key_present:
        provider_failures.append((
            "openai_api_key",
            "<non-empty>",
            "<empty>",
            "OPENAI_API_KEY missing/empty in .env",
        ))
    if looks_like_url or looks_like_base_url_copy:
        provider_failures.append((
            "openai_api_key",
            "<real API key (sk-… or Bailian token)>",
            "<looks like a URL / identical to base_url>",
            "OPENAI_API_KEY appears to be a copy of OPENAI_BASE_URL, not a real key",
        ))

    # ----- H. Launcher entry point (task I) --------------------------
    print("\n--- H. Launcher entry point (task I) ---")
    run_pilot_src = inspect.getsource(pilot_mod.run_pilot)
    applies_contract = "_apply_runtime_contract(config)" in run_pilot_src
    print(f"  eval/graph_ab_pilot.run_pilot calls _apply_runtime_contract(config): {'YES' if applies_contract else 'NO'}")
    print(f"  (line 905 of eval/graph_ab_pilot.py, before orchestrator construction)")
    if not applies_contract:
        provider_failures.append((
            "launcher",
            "passes through _apply_runtime_contract",
            "does not",
            "launcher bypasses the harness",
        ))

    # ----- I. Output-token behavior in InferenceEngine (task K) ------
    print("\n--- I. Output-token behavior in InferenceEngine (task K, read-only) ---")
    print(f"  _SUBMIT_MAX_TOKENS       = {ie_mod._SUBMIT_MAX_TOKENS}")
    print(f"  _EXPLORATION_MAX_TOKENS  = {ie_mod._EXPLORATION_MAX_TOKENS}")
    print(f"  settings.model_max_tokens (default ModelConfig) = {s.model_max_tokens}")
    print("  NOTE: exploration call uses _EXPLORATION_MAX_TOKENS=12288 (3x larger than")
    print("        MODEL_MAX_TOKENS=4096); final submit call uses _SUBMIT_MAX_TOKENS=4096.")
    print("        This is the historical design — per task K, NOT modifying in this preflight.")
    print("        No override performed; only recording effective values.")

    # ===== Final sanitized audit (task J) ===========================
    print("\n" + "=" * 72)
    print("SANITIZED PREFLIGHT RUNTIME AUDIT")
    print("=" * 72)

    print("\nProvider:")
    print(f"  provider:          {profile.provider}")
    print(f"  model:             {s.model_name}")
    print(f"  base_url_present:  {bool(base_url)}")
    print(f"  api_key_present:   {api_key_present}")
    print(f"  api_key_looks_like_url: {looks_like_url}")

    print("\nDeclared experiment contract (YAML shared:):")
    print(f"  prompt_input_token_budget:        {shared['prompt_input_token_budget']}")
    print(f"  token_budget:                     {shared['token_budget']}")
    print(f"  token_hard_budget:                {shared['token_hard_budget']}")
    print(f"  model_max_tokens:                 {shared['max_output_tokens']}")
    print(f"  final_submit_reserve_tokens:      {shared['final_submit_reserve_tokens']}")
    print(f"  final_submit_prompt_token_budget: {shared['final_submit_prompt_token_budget']}")
    print(f"  final_submit_feedback_token_budget:{shared['final_submit_feedback_token_budget']}")
    print(f"  review_max_iterations:            {shared['max_iterations']}")
    print(f"  agent_max_tool_calls:             {shared['tool_budget']}")
    print(f"  model_request_timeout_seconds:    {shared['model_request_timeout_seconds']}")
    print(f"  agent_tool_timeout_seconds:       {shared['tool_timeout_seconds']}")
    print(f"  agent_run_timeout_seconds:        {shared['run_timeout_seconds']}")

    print("\nEffective fresh Settings (after _apply_runtime_contract + get_settings):")
    for field, _ in declared_fields:
        print(f"  {field}: {getattr(s, field)}")

    print("\nOutput-token behavior (informational, not gating):")
    print(f"  exploration max_tokens (hardcoded): {ie_mod._EXPLORATION_MAX_TOKENS}")
    print(f"  final submit max_tokens (hardcoded): {ie_mod._SUBMIT_MAX_TOKENS}")
    print(f"  default config model_max_tokens:    {s.model_max_tokens}")

    print("\nRuntime-contract propagation (env + fresh Settings):")
    print(f"  env propagation failures:  {len(env_failures)}")
    print(f"  fresh settings failures:    {len(settings_failures)}")

    print("\nProvider connectivity (gating):")
    print(f"  model_provider == 'dashscope' (explicit):       {'YES' if s.model_provider == 'dashscope' else 'NO (legacy fallback used)'}")
    print(f"  OPENAI_BASE_URL is DashScope/Bailian endpoint:  {'YES' if base_url_is_dashscope else 'NO'}")
    print(f"  OPENAI_BASE_URL still DeepSeek endpoint:        {'YES' if base_url_is_deepseek else 'NO'}")
    print(f"  api_key_present:                                {api_key_present}")
    print(f"  api_key structurally a real key (not a URL):    {'YES' if api_key_present and not looks_like_url else 'NO'}")

    print("\nContract status:")
    total_failures = len(env_failures) + len(settings_failures) + len(provider_failures)
    if total_failures == 0:
        print("  PASS")
        print("\nEXPERIMENT_RUNTIME_PREFLIGHT=PASS")
        return 0

    print("  FAIL")
    print(f"\nEXPERIMENT_RUNTIME_PREFLIGHT=FAIL")
    print("\nFailures (declared / effective / source / suspected cause):")
    for f in env_failures:
        print(f"  [env]     {f[0]}: declared={f[1]!r} effective={f[2]!r}"
              f" source=os.environ after _apply_runtime_contract"
              f" cause=propagation broken")
    for f in settings_failures:
        print(f"  [settings]{f[0]}: declared={f[1]!r} effective={f[2]!r}"
              f" source=fresh get_settings()"
              f" cause=contract not reflected in Settings")
    for f in provider_failures:
        print(f"  [provider]{f[0]}: declared={f[1]!r} effective={f[2]!r}"
              f" source=.env/legacy-detection"
              f" cause={f[3]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
