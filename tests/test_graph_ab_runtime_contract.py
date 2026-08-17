"""Regression: development runtime contract must propagate to real runtime settings.

History: `get_settings()` builds a fresh Settings instance from the environment
on every call, so mutating one instance (the old `_apply_runtime_contract`
behaviour) was silently discarded and eval runs used default budgets
(30k/36k, prompt 32k, timeout 90s) instead of the declared development
contract (60k/80k, prompt 12k, timeout 180s).  The harness now writes the
contract into the environment; these tests pin that behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.config import get_settings
from eval.graph_ab_pilot import _apply_runtime_contract

ROOT = Path(__file__).resolve().parents[1]

CONTRACT_CONFIG = {
    "runtime_contract_source": "current",
    "shared": {
        "model": "deepseek-v4-flash",
        "max_output_tokens": 4096,
        "prompt_input_token_budget": 12000,
        "token_budget": 60000,
        "token_hard_budget": 80000,
        "final_submit_reserve_tokens": 12000,
        "final_submit_prompt_token_budget": 4000,
        "final_submit_feedback_token_budget": 1200,
        "max_iterations": 3,
        "tool_budget": 64,
        "model_request_timeout_seconds": 180.0,
        "tool_timeout_seconds": 30.0,
        "run_timeout_seconds": 600.0,
    },
}

CONTRACT_ENV = {
    "MODEL_NAME": "deepseek-v4-flash",
    "MODEL_MAX_TOKENS": "4096",
    "PROMPT_INPUT_TOKEN_BUDGET": "12000",
    "TOKEN_BUDGET": "60000",
    "TOKEN_HARD_BUDGET": "80000",
    "FINAL_SUBMIT_RESERVE_TOKENS": "12000",
    "FINAL_SUBMIT_PROMPT_TOKEN_BUDGET": "4000",
    "FINAL_SUBMIT_FEEDBACK_TOKEN_BUDGET": "1200",
    "AGENT_MAX_TOOL_CALLS": "64",
    "MODEL_REQUEST_TIMEOUT_SECONDS": "180.0",
    "AGENT_TOOL_TIMEOUT_SECONDS": "30.0",
    "AGENT_RUN_TIMEOUT_SECONDS": "600.0",
}


def test_apply_runtime_contract_sets_environment() -> None:
    _apply_runtime_contract(CONTRACT_CONFIG)
    for env_key, value in CONTRACT_ENV.items():
        assert os.environ.get(env_key) == value, f"{env_key} not propagated"


def test_fresh_settings_reflect_development_contract() -> None:
    _apply_runtime_contract(CONTRACT_CONFIG)
    # get_settings() must return the contract values on a brand-new instance.
    s = get_settings()
    assert s.model_name == "deepseek-v4-flash"
    assert s.model_max_tokens == 4096
    assert s.prompt_input_token_budget == 12000
    assert s.token_budget == 60000
    assert s.token_hard_budget == 80000
    assert s.final_submit_reserve_tokens == 12000
    assert s.final_submit_prompt_token_budget == 4000
    assert s.final_submit_feedback_token_budget == 1200
    assert s.agent_max_tool_calls == 64
    assert s.model_request_timeout_seconds == 180.0
    assert s.agent_tool_timeout_seconds == 30.0
    assert s.agent_run_timeout_seconds == 600.0


def test_defaults_differ_from_development_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity guard: without propagation the defaults are the env defaults."""
    for env_key in CONTRACT_ENV:
        monkeypatch.delenv(env_key, raising=False)
    s = get_settings()
    assert s.token_budget == 30000
    assert s.token_hard_budget == 36000
    assert s.prompt_input_token_budget == 32000
    assert s.model_max_tokens == 2048
    assert s.model_request_timeout_seconds == 90.0
