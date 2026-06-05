"""Synchronous FastAPI entrypoint for MergeWarden."""

from __future__ import annotations

from typing import Any

import json
import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from src import __version__
from src.analyzer.run_summary import RunSummary, summarize_event_log
from src.analyzer.schemas import DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.config import get_settings
from src.integrations.github_pr_review import (
    GitHubPullRequestReviewTrigger,
    run_github_pull_request_review,
)
from src.integrations.github_webhook import (
    claim_webhook_work,
    decide_github_webhook,
    verify_github_webhook_signature,
)
from src.orchestrator.agent_loop import AgentOrchestrator

app = FastAPI(title="MergeWarden API", version=__version__)
logger = logging.getLogger(__name__)


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


@app.post("/github/webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Receive GitHub App webhooks and enqueue pull-request reviews."""
    settings = get_settings()
    body = await request.body()
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    event_name = request.headers.get("X-GitHub-Event", "")
    signature = request.headers.get("X-Hub-Signature-256")

    if not verify_github_webhook_signature(
        body=body,
        signature_header=signature,
        secret=settings.github_webhook_secret,
    ):
        logger.warning(
            "signature invalid",
            extra={"delivery_id": delivery_id, "event_name": event_name},
        )
        raise HTTPException(status_code=401, detail={"message": "invalid signature", "run_id": ""})

    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail={"message": "invalid json", "run_id": ""}) from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"message": "invalid payload", "run_id": ""})

    logger.info(
        "webhook received",
        extra={
            "delivery_id": delivery_id,
            "event_name": event_name,
            "action": str(payload.get("action", "") or ""),
        },
    )
    decision = decide_github_webhook(
        event_name=event_name,
        delivery_id=delivery_id,
        payload=payload,
        settings=settings,
    )
    decision = claim_webhook_work(
        decision=decision,
        delivery_id=delivery_id,
        allow_rerun=settings.github_webhook_allow_rerun,
    )
    if decision.status != "accepted" or decision.trigger is None:
        logger.info(
            "event ignored",
            extra={
                "delivery_id": delivery_id,
                "event_name": event_name,
                "status": decision.status,
                "reason": decision.reason,
            },
        )
        return {
            "status": decision.status,
            "reason": decision.reason,
            "delivery_id": delivery_id,
        }

    logger.info(
        "pull_request review accepted",
        extra={
            "delivery_id": delivery_id,
            "owner_repo": decision.trigger.owner_repo,
            "pull_number": decision.trigger.pull_number,
            "head_sha": decision.trigger.head_sha,
            "installation_id": decision.trigger.installation_id,
        },
    )
    background_tasks.add_task(process_github_pull_request_review, decision.trigger)
    return {
        "status": "accepted",
        "delivery_id": delivery_id,
        "owner_repo": decision.trigger.owner_repo,
        "pull_number": decision.trigger.pull_number,
        "head_sha": decision.trigger.head_sha,
    }


async def process_github_pull_request_review(
    trigger: GitHubPullRequestReviewTrigger,
) -> None:
    """Background webhook worker wrapper with stable logging."""
    try:
        await run_github_pull_request_review(trigger)
    except Exception:
        logger.exception(
            "review failed",
            extra={
                "delivery_id": trigger.delivery_id,
                "owner_repo": trigger.owner_repo,
                "pull_number": trigger.pull_number,
                "installation_id": trigger.installation_id,
            },
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
