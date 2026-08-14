"""Three-case provider latency diagnostic using MergeWarden's production model path."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

import eval.runner as base_runner
from eval.core_eval import CoreRuntimeConfig, load_core_config
from eval.schemas import Fixture
from src.analyzer.context_builder import ContextBuilder
from src.analyzer.schemas import ReviewRequest
from src.config import get_settings
from src.models.client import ModelClient
from src.models.compat import ModelCallPolicy, resolve_model_profile
from src.models.exceptions import (
    AuthenticationError,
    ModelClientError,
    ModelTimeoutError,
    RateLimitError,
    ServiceUnavailableError,
)
from src.models.schemas import Message, ModelConfig, ModelResponse
from src.orchestrator.agent_loop import AgentOrchestrator
from src.tools import create_default_registry

ROOT = Path(__file__).resolve().parents[1]
CORE_CONFIG = ROOT / "eval" / "core_eval_v1.yaml"
FIXTURE_PATH = ROOT / "eval" / "fixtures" / "golden_pytest-dev_pytest_pr9350.json"
OUTPUT_JSON = ROOT / "eval" / "outputs" / "provider-latency-diagnostic.json"
OUTPUT_MARKDOWN = ROOT / "eval" / "reports" / "provider-latency-diagnostic.md"
MODEL_NAME = "deepseek-v4-pro"
TIMEOUT_SECONDS = 90.0
CASE_ORDER = ("plain", "minimal_tool", "reviewer_pytest9350")

TerminationReason = Literal[
    "completed",
    "completed_without_required_tool",
    "application_request_timeout",
    "sdk_connect_timeout",
    "sdk_read_timeout",
    "sdk_timeout",
    "provider_http_error",
    "provider_returned_error_body",
    "connection_error",
    "authentication_error",
    "rate_limit",
    "run_timeout",
    "unknown",
    "skipped",
]


class RequestShape(BaseModel):
    """Content-safe request metadata captured immediately before ModelClient.chat."""

    model: str
    provider: str
    endpoint_type: str
    api: str
    message_count: int
    system_message_count: int
    user_message_count: int
    assistant_message_count: int
    tool_message_count: int
    tool_schema_count: int
    tool_choice: str
    thinking: str
    reasoning_effort: str
    reasoning_replay_required: bool
    assistant_content_required: bool
    provider_compat: dict[str, Any]
    provider_specific_transforms: list[str]
    max_output_tokens: int
    estimated_input_tokens: int
    prompt_context_chars: int
    tool_schema_chars: int
    total_serialized_request_chars: int


class ProviderCaseResult(BaseModel):
    """One application-level measured case with one provider attempt."""

    case: str
    executed: bool = True
    skip_reason: str = ""
    request_shape: RequestShape | None = None
    request_started_at: str = ""
    response_latency_ms: int | None = None
    total_elapsed_ms: int | None = None
    timeout_threshold_seconds: float = TIMEOUT_SECONDS
    completed_within_90s: bool = False
    termination_reason: TerminationReason = "unknown"
    passed: bool = False
    provider_attempts: int = 0
    exception_class: str = ""
    exception_message: str = ""
    response_finish_reason: str = ""
    response_content_chars: int = 0
    response_tool_call_count: int = 0
    prompt_tokens_reported: int | None = None
    completion_tokens_reported: int | None = None
    reviewer_run_id: str = ""
    preparation_elapsed_ms: int | None = None


class ProviderLatencyReport(BaseModel):
    """Machine-readable source for the latency and request-shape matrices."""

    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    runtime_base_commit: str
    fixture: str = "golden_pytest-dev_pytest_pr9350"
    runtime_contract: dict[str, Any]
    cases: list[ProviderCaseResult]
    failure_stage: str
    diagnosis: str
    ready_to_rerun_reviewer_smoke: bool
    next_step: str


def _safe_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if not parsed.hostname:
        return "openai-compatible"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme or 'https'}://{parsed.hostname}{port}"


def sanitize_exception_message(value: str) -> str:
    """Remove credentials and URL details from diagnostic exception text."""

    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", value)
    text = re.sub(
        r"(?i)\b(authorization|api[_-]?key|token|secret)\b\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"https?://[^\s)]+", "[ENDPOINT]", text)
    return text[:600]


def classify_termination(exc: BaseException) -> TerminationReason:
    """Map production model exceptions to the diagnostic timeout taxonomy."""

    if isinstance(exc, ModelTimeoutError):
        code = str(exc.code or "")
        if code in {
            "application_request_timeout",
            "sdk_connect_timeout",
            "sdk_read_timeout",
            "sdk_timeout",
        }:
            return code  # type: ignore[return-value]
        return "unknown"
    if isinstance(exc, AuthenticationError):
        return "authentication_error"
    if isinstance(exc, RateLimitError):
        return "rate_limit"
    if isinstance(exc, ServiceUnavailableError):
        return (
            "connection_error"
            if exc.code == "connection_error"
            else "provider_http_error"
        )
    if isinstance(exc, ModelClientError) and exc.status_code is not None:
        if exc.code == "api_status_error":
            return "provider_returned_error_body"
        return "provider_http_error"
    if isinstance(exc, ModelClientError) and exc.code == "run_timeout":
        return "run_timeout"
    return "unknown"


def _shape(
    client: ModelClient,
    messages: list[Message],
    config: ModelConfig | None,
    tools: list[dict[str, Any]] | None,
    policy: ModelCallPolicy | None,
) -> RequestShape:
    source_config = config or client.default_config
    profile = client.profile_for(source_config.model)
    runtime_config, runtime_policy = client._apply_policy(  # noqa: SLF001
        source_config, policy, profile
    )
    serialized_messages = client._serialize_messages(  # noqa: SLF001
        messages, profile
    )
    tool_payload = tools or []
    payload: dict[str, Any] = {
        "model": runtime_config.model,
        "messages": serialized_messages,
        "temperature": runtime_config.temperature,
        "max_tokens": runtime_config.max_tokens,
        "top_p": runtime_config.top_p,
    }
    if tool_payload:
        payload["tools"] = tool_payload
    if runtime_config.tool_choice is not None:
        payload["tool_choice"] = runtime_config.tool_choice
    if runtime_config.extra_body is not None:
        payload["extra_body"] = runtime_config.extra_body
    reasoning_effort = "none"
    transforms: list[str] = []
    if runtime_config.extra_body:
        transforms.append("extra_body:" + ",".join(sorted(runtime_config.extra_body)))
    if runtime_policy.thinking == "high" and profile.compat.supports_reasoning_effort:
        payload["reasoning_effort"] = "high"
        reasoning_effort = "high"
        transforms.append("reasoning_effort=high")
    builder = ContextBuilder()
    message_tokens = sum(builder.estimate_tokens(item.content) for item in messages)
    tool_chars = (
        len(json.dumps(tool_payload, ensure_ascii=True, sort_keys=True))
        if tool_payload
        else 0
    )
    tool_tokens = (
        builder.estimate_tokens(json.dumps(tool_payload, ensure_ascii=True))
        if tool_payload
        else 0
    )
    role_counts = {
        role: sum(item.role == role for item in messages)
        for role in ("system", "user", "assistant", "tool")
    }
    return RequestShape(
        model=runtime_config.model,
        provider=profile.provider,
        endpoint_type=_safe_endpoint(str(client._settings.openai_base_url)),  # noqa: SLF001
        api=profile.api,
        message_count=len(messages),
        system_message_count=role_counts["system"],
        user_message_count=role_counts["user"],
        assistant_message_count=role_counts["assistant"],
        tool_message_count=role_counts["tool"],
        tool_schema_count=len(tool_payload),
        tool_choice=(
            json.dumps(runtime_config.tool_choice, sort_keys=True)
            if runtime_config.tool_choice is not None
            else "none"
        ),
        thinking=runtime_policy.thinking,
        reasoning_effort=reasoning_effort,
        reasoning_replay_required=profile.compat.requires_reasoning_replay_for_tool_calls,
        assistant_content_required=profile.compat.requires_assistant_content_for_tool_calls,
        provider_compat=profile.compat.model_dump(mode="json"),
        provider_specific_transforms=transforms,
        max_output_tokens=runtime_config.max_tokens,
        estimated_input_tokens=message_tokens + tool_tokens,
        prompt_context_chars=sum(len(item.content) for item in messages),
        tool_schema_chars=tool_chars,
        total_serialized_request_chars=len(
            json.dumps(payload, ensure_ascii=True, sort_keys=True)
        ),
    )


async def _measure_call(
    *,
    case: str,
    client: ModelClient,
    messages: list[Message],
    config: ModelConfig | None = None,
    tools: list[dict[str, Any]] | None = None,
    policy: ModelCallPolicy | None = None,
    required_tool: str = "",
) -> ProviderCaseResult:
    result = ProviderCaseResult(
        case=case,
        request_shape=_shape(client, messages, config, tools, policy),
        request_started_at=datetime.now(UTC).isoformat(),
        provider_attempts=1,
    )
    started = perf_counter()
    try:
        response = await client.chat(
            messages=messages,
            config=config,
            tools=tools,
            policy=policy,
        )
        result.response_latency_ms = round((perf_counter() - started) * 1000)
        result.total_elapsed_ms = result.response_latency_ms
        result.completed_within_90s = (
            result.response_latency_ms / 1000
        ) < TIMEOUT_SECONDS
        result.response_finish_reason = response.finish_reason
        result.response_content_chars = len(response.content)
        result.response_tool_call_count = len(response.tool_calls)
        result.prompt_tokens_reported = response.usage.prompt_tokens
        result.completion_tokens_reported = response.usage.completion_tokens
        tool_names = {
            str(call.get("function", {}).get("name", ""))
            for call in response.tool_calls
            if isinstance(call, dict) and isinstance(call.get("function"), dict)
        }
        if required_tool and required_tool not in tool_names:
            result.termination_reason = "completed_without_required_tool"
            result.passed = False
        else:
            result.termination_reason = "completed"
            result.passed = result.completed_within_90s
    except Exception as exc:  # noqa: BLE001
        result.response_latency_ms = round((perf_counter() - started) * 1000)
        result.total_elapsed_ms = result.response_latency_ms
        result.termination_reason = classify_termination(exc)
        result.exception_class = exc.__class__.__name__
        result.exception_message = sanitize_exception_message(str(exc))
    return result


def _diagnostic_client(settings: Any) -> ModelClient:
    """Use the production client while disabling SDK retries for measured cases."""

    client = ModelClient(settings=settings, max_retries=1, temperature=0.0)
    client._client.max_retries = 0  # noqa: SLF001
    return client


def _minimal_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "diagnostic_echo",
            "description": "Return one fixed diagnostic value.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        },
    }


async def run_plain(settings: Any) -> ProviderCaseResult:
    client = _diagnostic_client(settings)
    try:
        return await _measure_call(
            case="plain",
            client=client,
            messages=[
                Message(
                    role="system", content="Return exactly the requested short text."
                ),
                Message(role="user", content="Return exactly: MERGEWARDEN_PROVIDER_OK"),
            ],
            config=client.default_config,
            policy=ModelCallPolicy(thinking="off"),
        )
    finally:
        await client.close()


async def run_minimal_tool(settings: Any) -> ProviderCaseResult:
    client = _diagnostic_client(settings)
    try:
        return await _measure_call(
            case="minimal_tool",
            client=client,
            messages=[
                Message(
                    role="system", content="Use the supplied tool as your only action."
                ),
                Message(
                    role="user", content="Call diagnostic_echo with value provider-ok."
                ),
            ],
            config=client.default_config,
            tools=[_minimal_tool_schema()],
            policy=ModelCallPolicy(thinking="high"),
            required_tool="diagnostic_echo",
        )
    finally:
        await client.close()


class _ReviewerRecordingClient:
    """Recording wrapper that returns the one captured production response."""

    def __init__(self, client: ModelClient) -> None:
        self.client = client
        self.default_config = client.default_config
        self.result: ProviderCaseResult | None = None

    async def chat(self, *args: Any, **kwargs: Any) -> ModelResponse:
        if self.result is not None:
            raise ModelClientError(
                "Unexpected second reviewer provider call",
                code="unexpected_second_call",
            )
        messages = kwargs.get("messages", args[0] if args else [])
        config = kwargs.get("config")
        tools = kwargs.get("tools")
        policy = kwargs.get("policy")
        shape = _shape(self.client, messages, config, tools, policy)
        result = ProviderCaseResult(
            case="reviewer_pytest9350",
            request_shape=shape,
            request_started_at=datetime.now(UTC).isoformat(),
            provider_attempts=1,
        )
        started = perf_counter()
        try:
            response = await self.client.chat(*args, **kwargs)
            result.response_latency_ms = round((perf_counter() - started) * 1000)
            result.total_elapsed_ms = result.response_latency_ms
            result.completed_within_90s = (
                result.response_latency_ms / 1000
            ) < TIMEOUT_SECONDS
            result.termination_reason = "completed"
            result.passed = result.completed_within_90s
            result.response_finish_reason = response.finish_reason
            result.response_content_chars = len(response.content)
            result.response_tool_call_count = len(response.tool_calls)
            result.prompt_tokens_reported = response.usage.prompt_tokens
            result.completion_tokens_reported = response.usage.completion_tokens
            self.result = result
            return response
        except Exception as exc:  # noqa: BLE001
            result.response_latency_ms = round((perf_counter() - started) * 1000)
            result.total_elapsed_ms = result.response_latency_ms
            result.termination_reason = classify_termination(exc)
            result.exception_class = exc.__class__.__name__
            result.exception_message = sanitize_exception_message(str(exc))
            self.result = result
            raise


async def run_reviewer_first_request(
    settings: Any, runtime: CoreRuntimeConfig
) -> ProviderCaseResult:
    fixture = Fixture.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))
    started = perf_counter()
    with tempfile.TemporaryDirectory(prefix="provider-latency-") as temp_dir:
        repo_root = await asyncio.to_thread(
            base_runner._prepare_fixture_workspace,
            fixture,
            Path(temp_dir) / "repo",
            workspace_cache_dir=Path(settings.eval_workspace_cache_dir),
        )
        errors = await asyncio.to_thread(
            base_runner._validate_diff_added_lines_against_workspace, fixture, repo_root
        ) + await asyncio.to_thread(
            base_runner._validate_expected_locations_against_diff, fixture, repo_root
        )
        if errors:
            return ProviderCaseResult(
                case="reviewer_pytest9350",
                termination_reason="unknown",
                exception_class="FixtureValidationError",
                exception_message="; ".join(errors),
                preparation_elapsed_ms=round((perf_counter() - started) * 1000),
            )
        context = base_runner._build_fixture_context(fixture, repo_root)
        diff = base_runner._prepend_context(fixture.input.diff_text or "", context)
        request = ReviewRequest(
            repo_path=str(repo_root),
            diff_mode=True,
            diff_text=diff,
            verbose=False,
        )
        real_client = _diagnostic_client(settings)
        recorder = _ReviewerRecordingClient(real_client)
        orchestrator = AgentOrchestrator(
            permission_mode="default",
            temperature=0.0,
            review_max_iterations=runtime.review_max_iterations,
            review_min_tool_iterations=max(1, settings.eval_review_min_tool_iterations),
            review_diff_first_changed_files=True,
            context_mode="agent_search",
        )
        orchestrator._model_client = recorder  # type: ignore[assignment]  # noqa: SLF001
        orchestrator._reset_run(runtime.review_max_iterations, str(repo_root))  # noqa: SLF001
        review_context = orchestrator._build_review_tool_context(request)  # noqa: SLF001
        orchestrator._registry = create_default_registry(  # noqa: SLF001
            include_execute=False,
            review_context=review_context,
        )
        state = orchestrator.prepare_context(request)
        await orchestrator._prepare_review_context(state, request)  # noqa: SLF001
        await orchestrator._maybe_prefetch_review_changed_files(state, request)  # noqa: SLF001
        preparation_ms = round((perf_counter() - started) * 1000)
        try:
            await orchestrator.analyze(
                state,
                request,
                orchestrator._registry.list_specs(),  # noqa: SLF001
            )
        finally:
            await real_client.close()
            orchestrator._close_event_log()  # noqa: SLF001
        result = recorder.result or ProviderCaseResult(
            case="reviewer_pytest9350",
            termination_reason="unknown",
            exception_class="NoProviderCallObserved",
        )
        result.reviewer_run_id = orchestrator._run_id  # noqa: SLF001
        result.preparation_elapsed_ms = preparation_ms
        return result


def skipped(case: str, reason: str) -> ProviderCaseResult:
    return ProviderCaseResult(
        case=case,
        executed=False,
        skip_reason=reason,
        termination_reason="skipped",
    )


def diagnose(cases: list[ProviderCaseResult]) -> tuple[str, str, bool, str]:
    by_case = {item.case: item for item in cases}
    plain = by_case["plain"]
    tool = by_case["minimal_tool"]
    reviewer = by_case["reviewer_pytest9350"]
    if not plain.passed:
        return (
            "provider_request",
            "A. Provider / endpoint latency: the minimal plain production ModelClient request did not complete successfully within 90 seconds.",
            False,
            "Check deepseek-v4-pro endpoint availability/latency under the unchanged 90s contract before rerunning any Reviewer smoke.",
        )
    if not tool.passed:
        return (
            "provider_request",
            "B. Tool / compatibility path: plain completed, but the minimal high-thinking tool request failed.",
            False,
            "Inspect the DeepSeek thinking + tool-calling compatibility request and provider behavior; do not change Reviewer logic or timeout yet.",
        )
    if not reviewer.passed:
        return (
            "provider_request",
            "C. Reviewer request shape: plain and minimal tool calls completed, but the first production pytest#9350 reviewer request failed.",
            False,
            "Compare the recorded Reviewer input/tool-schema size and 12288-token exploration cap with the passing minimal calls in a follow-up diagnostic.",
        )
    return (
        "none",
        "E. INCONCLUSIVE: all three single attempts completed; the prior provider timeout was not reproduced.",
        True,
        "Rerun the Reviewer/Runtime smoke once under the unchanged runtime contract; do not start formal Graph A/B yet.",
    )


async def run_diagnostic() -> ProviderLatencyReport:
    config = load_core_config(CORE_CONFIG)
    runtime = config.runtime
    env = {
        "MODEL_NAME": MODEL_NAME,
        "MODEL_MAX_TOKENS": str(runtime.model_max_tokens),
        "MODEL_REQUEST_TIMEOUT_SECONDS": str(int(TIMEOUT_SECONDS)),
        "MODEL_MAX_RETRIES": "1",
        "PROMPT_INPUT_TOKEN_BUDGET": str(runtime.prompt_input_token_budget),
        "TOKEN_BUDGET": str(runtime.token_budget),
        "TOKEN_HARD_BUDGET": str(runtime.token_hard_budget),
        "FINAL_SUBMIT_RESERVE_TOKENS": str(runtime.final_submit_reserve_tokens),
        "FINAL_SUBMIT_PROMPT_TOKEN_BUDGET": str(
            runtime.final_submit_prompt_token_budget
        ),
        "FINAL_SUBMIT_FEEDBACK_TOKEN_BUDGET": str(
            runtime.final_submit_feedback_token_budget
        ),
        "EVAL_REVIEW_MAX_ITERATIONS_CAP": str(runtime.review_max_iterations),
    }
    original = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        settings = get_settings()
        retry_probe = ModelClient(settings=settings, max_retries=1)
        production_sdk_retries = retry_probe._client.max_retries  # noqa: SLF001
        await retry_probe.close()
        cases: list[ProviderCaseResult] = []
        plain = await run_plain(settings)
        cases.append(plain)
        if not plain.passed:
            cases.extend(
                [
                    skipped("minimal_tool", "Stopped because Plain did not pass."),
                    skipped(
                        "reviewer_pytest9350", "Stopped because Plain did not pass."
                    ),
                ]
            )
        else:
            tool = await run_minimal_tool(settings)
            cases.append(tool)
            if not tool.passed:
                cases.append(
                    skipped(
                        "reviewer_pytest9350",
                        "Stopped because Minimal Tool did not pass.",
                    )
                )
            else:
                cases.append(await run_reviewer_first_request(settings, runtime))
        failure_stage, diagnosis, ready, next_step = diagnose(cases)
        base_commit = base_runner._run_git(["rev-parse", "HEAD"], cwd=ROOT)
        profile = resolve_model_profile(settings, MODEL_NAME)
        return ProviderLatencyReport(
            runtime_base_commit=base_commit,
            runtime_contract={
                "model": MODEL_NAME,
                "provider": profile.provider,
                "endpoint_type": _safe_endpoint(str(settings.openai_base_url)),
                "timeout_seconds": settings.model_request_timeout_seconds,
                "default_max_output_tokens": settings.model_max_tokens,
                "provider_attempts_per_case": 1,
                "diagnostic_sdk_max_retries": 0,
                "production_sdk_default_max_retries": production_sdk_retries,
                "model_profile": profile.model_dump(mode="json"),
            },
            cases=cases,
            failure_stage=failure_stage,
            diagnosis=diagnosis,
            ready_to_rerun_reviewer_smoke=ready,
            next_step=next_step,
        )
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def render_markdown(report: ProviderLatencyReport) -> str:
    by_case = {item.case: item for item in report.cases}
    ordered = [by_case[item] for item in CASE_ORDER]

    def value(case: ProviderCaseResult, field: str) -> str:
        if not case.executed:
            return "SKIPPED"
        shape = case.request_shape
        if shape is None:
            return "N/A"
        return str(getattr(shape, field))

    lines = [
        "# Provider Latency Diagnostic",
        "",
        "> One measured attempt per case; no automatic retry, fallback model, timeout change, or full Reviewer A/B run.",
        "",
        "## Environment / Runtime Contract",
        "",
        f"- Runtime base commit: `{report.runtime_base_commit}`",
        f"- Model / provider: `{report.runtime_contract['model']}` / `{report.runtime_contract['provider']}`",
        f"- Endpoint type: `{report.runtime_contract['endpoint_type']}` (credentials and URL path omitted)",
        f"- Timeout: `{report.runtime_contract['timeout_seconds']}` seconds; default max output: `{report.runtime_contract['default_max_output_tokens']}`; provider attempts per case: `1`",
        f"- Retry controls: diagnostic SDK retries `0`; production SDK default observed `{report.runtime_contract.get('production_sdk_default_max_retries', 'unknown')}`; ModelClient outer attempts `1`",
        f"- ModelProfile / ProviderCompat: `{json.dumps(report.runtime_contract['model_profile'], sort_keys=True)}`",
        "",
        "## Latency Matrix",
        "",
        "| Case | Input Tokens | Tools | Thinking | Latency | Termination | PASS |",
        "|---|---:|---:|---|---:|---|---|",
    ]
    labels = {
        "plain": "Plain",
        "minimal_tool": "Minimal Tool",
        "reviewer_pytest9350": "Reviewer pytest#9350",
    }
    for case in ordered:
        shape = case.request_shape
        lines.append(
            f"| {labels[case.case]} | {shape.estimated_input_tokens if shape else 'N/A'} | {shape.tool_schema_count if shape else 'N/A'} | {shape.thinking if shape else 'N/A'} | {str(case.response_latency_ms) + ' ms' if case.response_latency_ms is not None else 'SKIPPED'} | {case.termination_reason} | {'YES' if case.passed else 'NO'} |"
        )
    lines.extend(
        [
            "",
            "`response_latency_ms` is total non-streaming response latency, not TTFT.",
            "",
            "## Request Shape Matrix",
            "",
            "| Property | Plain | Minimal Tool | Reviewer |",
            "|---|---:|---:|---:|",
        ]
    )
    for label, field in (
        ("message_count", "message_count"),
        ("system_message_count", "system_message_count"),
        ("user_message_count", "user_message_count"),
        ("assistant_message_count", "assistant_message_count"),
        ("tool_message_count", "tool_message_count"),
        ("input_tokens_est", "estimated_input_tokens"),
        ("prompt_chars", "prompt_context_chars"),
        ("tool_schema_count", "tool_schema_count"),
        ("tool_schema_chars", "tool_schema_chars"),
        ("thinking", "thinking"),
        ("tool_choice", "tool_choice"),
        ("reasoning replay", "reasoning_replay_required"),
        ("assistant content required", "assistant_content_required"),
        ("reasoning_effort", "reasoning_effort"),
        ("provider transforms", "provider_specific_transforms"),
        ("max_output_tokens", "max_output_tokens"),
        ("serialized_request_chars", "total_serialized_request_chars"),
    ):
        lines.append(
            f"| {label} | {value(ordered[0], field)} | {value(ordered[1], field)} | {value(ordered[2], field)} |"
        )
    reviewer_shape = ordered[2].request_shape
    plain_shape = ordered[0].request_shape
    if reviewer_shape is not None and plain_shape is not None:
        input_ratio = reviewer_shape.estimated_input_tokens / max(
            plain_shape.estimated_input_tokens, 1
        )
        lines.extend(
            [
                "",
                "## Observed Evidence",
                "",
                f"- The Reviewer request was `{input_ratio:.1f}x` the estimated input tokens of Plain, with `{reviewer_shape.tool_schema_count}` tool schemas / `{reviewer_shape.tool_schema_chars}` schema characters, and took `{ordered[2].response_latency_ms} ms`.",
                f"- Although the Core runtime default is `{report.runtime_contract['default_max_output_tokens']}`, the exact first Reviewer exploration request resolved to `{reviewer_shape.max_output_tokens}` max output tokens. This diagnostic records that existing behavior and does not change it.",
                "- All three single attempts completed. This sample therefore cannot distinguish transient provider availability from Reviewer request-shape latency, and does not support diagnosing the 90-second policy as too strict.",
            ]
        )
    lines.extend(["", "## Case Details", ""])
    for case in ordered:
        detail = (
            f"Skipped: {case.skip_reason}"
            if not case.executed
            else f"Started `{case.request_started_at}`; response latency / total request elapsed `{case.response_latency_ms} / {case.total_elapsed_ms} ms`; termination `{case.termination_reason}`; exception `{case.exception_class or 'none'}` / `{case.exception_message or 'none'}`; provider attempts `{case.provider_attempts}`. Finish reason `{case.response_finish_reason or 'none'}`; response content / tool calls `{case.response_content_chars} chars / {case.response_tool_call_count}`; provider-reported prompt / completion tokens `{case.prompt_tokens_reported} / {case.completion_tokens_reported}`."
        )
        if case.reviewer_run_id:
            detail += (
                f" Reviewer run_id `{case.reviewer_run_id}`; request preparation "
                f"`{case.preparation_elapsed_ms} ms`."
            )
        lines.extend(
            [
                f"### {labels[case.case]}",
                "",
                detail,
                "",
            ]
        )
    lines.extend(
        [
            "## Failure Attribution",
            "",
            f"`Failure Stage = {report.failure_stage}`",
            "",
            report.diagnosis,
            "",
            "## Ready to rerun Reviewer/Runtime Smoke?",
            "",
            f"**{'YES' if report.ready_to_rerun_reviewer_smoke else 'NO'}**",
            "",
            report.next_step,
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-json", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--output-markdown", type=Path, default=OUTPUT_MARKDOWN)
    args = parser.parse_args()
    report = asyncio.run(run_diagnostic())
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    args.output_markdown.write_text(render_markdown(report), encoding="utf-8")
    print(f"JSON {args.output_json}")
    print(f"MARKDOWN {args.output_markdown}")
    for case in report.cases:
        print(
            f"{case.case}: termination={case.termination_reason} latency_ms={case.response_latency_ms} pass={case.passed}"
        )
    print(report.diagnosis)


if __name__ == "__main__":
    main()
