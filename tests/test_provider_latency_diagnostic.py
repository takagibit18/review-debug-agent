"""Targeted tests for the provider latency diagnostic."""

from eval.provider_latency_diagnostic import (
    ProviderCaseResult,
    ProviderLatencyReport,
    RequestShape,
    classify_termination,
    render_markdown,
    sanitize_exception_message,
)
from src.models.client import ModelClient
from src.models.exceptions import ModelClientError, ModelTimeoutError


def _shape(*, tools: int = 0, thinking: str = "off") -> RequestShape:
    return RequestShape(
        model="deepseek-v4-pro",
        provider="deepseek",
        endpoint_type="https://provider.example",
        api="openai-completions",
        message_count=2,
        system_message_count=1,
        user_message_count=1,
        assistant_message_count=0,
        tool_message_count=0,
        tool_schema_count=tools,
        tool_choice="none",
        thinking=thinking,
        reasoning_effort="high" if thinking == "high" else "none",
        reasoning_replay_required=True,
        assistant_content_required=True,
        provider_compat={"thinking_format": "deepseek"},
        provider_specific_transforms=["extra_body:thinking"],
        max_output_tokens=4096,
        estimated_input_tokens=20,
        prompt_context_chars=80,
        tool_schema_chars=100 if tools else 0,
        total_serialized_request_chars=200,
    )


def test_result_serialization_and_matrix_rendering() -> None:
    report = ProviderLatencyReport(
        runtime_base_commit="a" * 40,
        runtime_contract={
            "model": "deepseek-v4-pro",
            "provider": "deepseek",
            "endpoint_type": "https://provider.example",
            "timeout_seconds": 90,
            "default_max_output_tokens": 4096,
            "model_profile": {"provider": "deepseek"},
        },
        cases=[
            ProviderCaseResult(
                case="plain",
                request_shape=_shape(),
                response_latency_ms=1200,
                completed_within_90s=True,
                termination_reason="completed",
                passed=True,
                provider_attempts=1,
            ),
            ProviderCaseResult(
                case="minimal_tool",
                request_shape=_shape(tools=1, thinking="high"),
                response_latency_ms=2500,
                completed_within_90s=True,
                termination_reason="completed",
                passed=True,
                provider_attempts=1,
            ),
            ProviderCaseResult(
                case="reviewer_pytest9350",
                request_shape=_shape(tools=12, thinking="high"),
                response_latency_ms=90050,
                termination_reason="application_request_timeout",
                provider_attempts=1,
            ),
        ],
        failure_stage="provider_request",
        diagnosis="C. Reviewer request shape",
        ready_to_rerun_reviewer_smoke=False,
        next_step="inspect request shape",
    )

    restored = ProviderLatencyReport.model_validate_json(report.model_dump_json())
    markdown = render_markdown(restored)

    assert restored.cases[2].termination_reason == "application_request_timeout"
    assert "| Reviewer pytest#9350 | 20 | 12 | high | 90050 ms" in markdown
    assert "`Failure Stage = provider_request`" in markdown
    assert "## Observed Evidence" in markdown
    assert "existing behavior and does not change it" in markdown
    assert "**NO**" in markdown


def test_timeout_termination_classification() -> None:
    assert (
        classify_termination(
            ModelTimeoutError("timed out", code="application_request_timeout")
        )
        == "application_request_timeout"
    )
    assert (
        classify_termination(ModelTimeoutError("timed out", code="sdk_read_timeout"))
        == "sdk_read_timeout"
    )
    assert (
        classify_termination(
            ModelClientError("provider body", status_code=400, code="api_status_error")
        )
        == "provider_returned_error_body"
    )
    assert (
        classify_termination(ModelClientError("run timed out", code="run_timeout"))
        == "run_timeout"
    )


def test_sdk_timeout_source_and_secret_redaction() -> None:
    read_timeout = type("ReadTimeout", (Exception,), {})("read")
    sdk_timeout = TimeoutError("sdk")
    sdk_timeout.__cause__ = read_timeout

    assert ModelClient._sdk_timeout_code(sdk_timeout) == "sdk_read_timeout"  # noqa: SLF001
    sanitized = sanitize_exception_message(
        "Authorization: Bearer top-secret api_key=abc https://private.example/v1"
    )
    assert "top-secret" not in sanitized
    assert "abc" not in sanitized
    assert "private.example" not in sanitized
    assert "[REDACTED]" in sanitized
