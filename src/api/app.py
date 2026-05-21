"""Synchronous FastAPI entrypoint for MergeWarden."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException

from src import __version__
from src.analyzer.run_summary import RunSummary, summarize_event_log
from src.analyzer.schemas import DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.config import get_settings
from src.orchestrator.agent_loop import AgentOrchestrator

app = FastAPI(title="MergeWarden API", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    """Return basic service health and runtime defaults."""
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "model_name": settings.model_name,
    }


@app.post("/review", response_model=ReviewResponse)
async def review(request: ReviewRequest) -> ReviewResponse:
    """Run a synchronous review request through the shared orchestrator."""
    orchestrator = AgentOrchestrator()
    try:
        return await orchestrator.run_review(request)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _stable_error("review failed") from exc


@app.post("/debug", response_model=DebugResponse)
async def debug(request: DebugRequest) -> DebugResponse:
    """Run a synchronous debug request through the shared orchestrator."""
    orchestrator = AgentOrchestrator()
    try:
        return await orchestrator.run_debug(request)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _stable_error("debug failed") from exc


@app.get("/runs/{run_id}/summary", response_model=RunSummary)
def run_summary(run_id: str) -> RunSummary:
    """Return a compact summary for a run event log without changing review/debug contracts."""
    settings = get_settings()
    return summarize_event_log(
        _resolve_event_log_path(settings.event_log_dir, run_id),
        run_id=run_id,
        publish_status="not_requested",
    )


def _stable_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=500,
        detail={"message": message, "run_id": ""},
    )


def _resolve_event_log_path(log_dir: str, run_id: str) -> str:
    from pathlib import Path

    return str(Path(log_dir) / f"{run_id}.jsonl")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Any, exc: HTTPException) -> Any:
    from fastapi.responses import JSONResponse

    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": str(exc.detail), "run_id": ""},
    )
