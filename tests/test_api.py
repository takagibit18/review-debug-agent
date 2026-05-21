"""Tests for the synchronous FastAPI entrypoint."""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from src import __version__
from src.analyzer.context_state import ContextState
from src.analyzer.output_formatter import ReviewReport
from src.analyzer.schemas import DebugResponse, ReviewResponse
from src.config import get_settings


def test_health_returns_status_version_and_model_name() -> None:
    from src.api.app import app

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "version": __version__,
        "model_name": get_settings().model_name,
    }


def test_review_endpoint_returns_review_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app

    async def _run_review(self, request):  # type: ignore[no-untyped-def]
        assert request.repo_path == "."
        assert request.diff_mode is True
        return ReviewResponse(
            run_id="api-review-run",
            report=ReviewReport(summary="review ok"),
            context=ContextState(current_files=[request.repo_path]),
        )

    monkeypatch.setattr(api_app.AgentOrchestrator, "run_review", _run_review)

    response = TestClient(api_app.app).post(
        "/review",
        json={"repo_path": ".", "diff_mode": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "api-review-run"
    assert payload["report"]["summary"] == "review ok"
    assert payload["context"]["current_files"] == ["."]


def test_debug_endpoint_returns_debug_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app

    async def _run_debug(self, request):  # type: ignore[no-untyped-def]
        assert request.repo_path == "."
        assert request.error_log_text == "boom"
        return DebugResponse(
            run_id="api-debug-run",
            summary="debug ok",
            hypotheses=["dependency failure"],
            steps=[],
            context=ContextState(current_files=[request.repo_path]),
        )

    monkeypatch.setattr(api_app.AgentOrchestrator, "run_debug", _run_debug)

    response = TestClient(api_app.app).post(
        "/debug",
        json={"repo_path": ".", "error_log_text": "boom"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "api-debug-run"
    assert payload["summary"] == "debug ok"
    assert payload["hypotheses"] == ["dependency failure"]


def test_review_endpoint_returns_stable_error_without_raw_exception(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.api import app as api_app

    async def _broken_run_review(self, request):  # type: ignore[no-untyped-def]
        raise RuntimeError("secret-token-leaked")

    monkeypatch.setattr(api_app.AgentOrchestrator, "run_review", _broken_run_review)

    response = TestClient(api_app.app).post(
        "/review",
        json={"repo_path": "."},
    )

    assert response.status_code == 500
    assert response.json() == {"message": "review failed", "run_id": ""}
    assert "secret-token-leaked" not in response.text


def test_run_summary_endpoint_returns_stable_missing_summary() -> None:
    from src.api import app as api_app

    response = TestClient(api_app.app).get("/runs/missing-run/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "missing-run"
    assert payload["event_log_status"] == "missing"
    assert payload["publish_status"] == "not_requested"
