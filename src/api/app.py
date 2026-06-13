"""Synchronous FastAPI entrypoint for MergeWarden."""

from __future__ import annotations

from typing import Any, Literal

import json
import logging

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Query, Request

from src import __version__
from src.analyzer.run_summary import RunSummary, summarize_event_log
from src.analyzer.schemas import DebugRequest, DebugResponse, ReviewRequest, ReviewResponse
from src.config import get_settings
from src.integrations.github_pr_review import (
    GitHubPullRequestReviewTrigger,
    run_github_pull_request_review,
)
from src.integrations.github_webhook import (
    verify_github_webhook_signature,
)
from src.orchestrator.agent_loop import AgentOrchestrator
from src.platform.artifacts import ArtifactStore
from src.platform.db import connect, init_db
from src.platform.repositories import PlatformRepository
from src.platform.schemas import (
    InstallationResponse,
    PlatformHealthResponse,
    RepositoryResponse,
    RetryRunResponse,
    TenantContextResponse,
    TenantSummaryResponse,
    ReviewRunDetailResponse,
    ReviewRunResponse,
    UsageRecordResponse,
    WebhookDeliveryResponse,
)
from src.platform.services import WebhookIngestionService
from src.platform.tenancy import (
    TENANT_HEADER,
    TenantContext,
    TenantResolutionError,
    parse_tenant_id,
    tenant_context_from_installation,
)

app = FastAPI(title="MergeWarden API", version=__version__)
logger = logging.getLogger(__name__)
_initialized_platform_databases: set[str] = set()


@app.on_event("startup")
def platform_startup() -> None:
    """Initialize platform storage for local MVP deployments."""
    settings = get_settings()
    if not settings.platform_init_db_on_startup:
        return
    conn = connect(settings.platform_database_url)
    try:
        init_db(conn)
        _initialized_platform_databases.add(settings.platform_database_url)
    finally:
        conn.close()
    logger.warning(
        "platform API stores webhook reviews as queued database jobs; start "
        "`python cli.py platform worker` to process queued runs",
        extra={
            "database_url": settings.platform_database_url,
            "single_worker": settings.platform_worker_single_worker,
        },
    )


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
) -> dict[str, Any]:
    """Receive GitHub App webhooks and durably enqueue pull-request reviews."""
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
    with _platform_repo(settings) as repo:
        result = WebhookIngestionService(repo, settings).ingest(
            event_name=event_name,
            delivery_id=delivery_id,
            payload=payload,
        )
    logger.info(
        "webhook handled",
        extra={
            "delivery_id": delivery_id,
            "event_name": event_name,
            "status": result.status,
            "reason": result.reason,
            "run_id": result.run_id,
        },
    )
    return result.model_dump(exclude_none=True, exclude_defaults=True)


@app.get("/platform/health", response_model=PlatformHealthResponse)
def platform_health() -> PlatformHealthResponse:
    settings = get_settings()
    connected = False
    with _platform_repo(settings) as repo:
        repo.conn.execute("SELECT 1").fetchone()
        connected = True
    return PlatformHealthResponse(
        status="ok",
        database_url=settings.platform_database_url,
        database_connected=connected,
        worker={
            "mode": "db_polling",
            "single_worker": settings.platform_worker_single_worker,
            "poll_interval_seconds": settings.platform_worker_poll_interval_seconds,
            "required": True,
            "start_command": "python cli.py platform worker",
            "api_processes_reviews_inline": False,
        },
        artifact_root=settings.platform_artifact_root,
    )


@app.get("/platform/tenant", response_model=TenantContextResponse)
def platform_tenant(request: Request) -> TenantContextResponse:
    settings = get_settings()
    with _platform_repo(settings) as repo:
        tenant = _resolve_tenant_context(request, repo, required=False, settings=settings)
    if tenant is None:
        return TenantContextResponse(
            resolved=False,
            source="missing",
            header_name=TENANT_HEADER,
            tenant=None,
        )
    return _tenant_context_response(tenant)


@app.get("/platform/installations", response_model=list[InstallationResponse])
def platform_installations() -> list[InstallationResponse]:
    settings = get_settings()
    with _platform_repo(settings) as repo:
        return [
            InstallationResponse.model_validate(item.model_dump())
            for item in repo.list_installations()
        ]


@app.get("/platform/repositories", response_model=list[RepositoryResponse])
def platform_repositories(
    request: Request,
    installation_id: int | None = Query(default=None),
) -> list[RepositoryResponse]:
    settings = get_settings()
    with _platform_repo(settings) as repo:
        tenant = _require_tenant_context(request, repo, settings=settings)
        if installation_id is not None and installation_id != tenant.id:
            raise HTTPException(
                status_code=403,
                detail={"message": "tenant mismatch", "run_id": ""},
            )
        return [
            RepositoryResponse.model_validate(item.model_dump())
            for item in repo.list_repositories(installation_id=tenant.id)
        ]


@app.get("/platform/runs", response_model=list[ReviewRunResponse])
def platform_runs(
    request: Request,
    repo_full_name: str | None = Query(default=None),
    pr_number: int | None = Query(default=None),
    status: str | None = Query(default=None),
) -> list[ReviewRunResponse]:
    settings = get_settings()
    artifacts = ArtifactStore(settings.platform_artifact_root)
    with _platform_repo(settings) as repo:
        tenant = _require_tenant_context(request, repo, settings=settings)
        return [
            _run_response(run, artifacts)
            for run in repo.list_runs(
                installation_id=tenant.id,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                status=status,
            )
        ]


@app.get("/platform/runs/{run_id}", response_model=ReviewRunDetailResponse)
def platform_run_detail(request: Request, run_id: str) -> ReviewRunDetailResponse:
    settings = get_settings()
    artifacts = ArtifactStore(settings.platform_artifact_root)
    with _platform_repo(settings) as repo:
        tenant = _require_tenant_context(request, repo, settings=settings)
        run = repo.get_run(run_id, installation_id=tenant.id)
        if run is None:
            raise HTTPException(status_code=404, detail={"message": "run not found", "run_id": run_id})
        base = _run_response(run, artifacts)
        return ReviewRunDetailResponse(
            **base.model_dump(),
            usage_records=[
                UsageRecordResponse.model_validate(item.model_dump())
                for item in repo.list_usage_records(run_id=run_id)
            ],
        )


@app.post("/platform/runs/{run_id}/retry", response_model=RetryRunResponse)
def platform_retry_run(request: Request, run_id: str) -> RetryRunResponse:
    settings = get_settings()
    with _platform_repo(settings) as repo:
        tenant = _require_tenant_context(request, repo, settings=settings)
        try:
            retry = repo.retry_run(run_id, installation_id=tenant.id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"message": "run not found", "run_id": run_id}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc), "run_id": run_id}) from exc
    return RetryRunResponse(status="queued", run_id=retry.run_id, previous_run_id=run_id)


@app.get("/platform/deliveries", response_model=list[WebhookDeliveryResponse])
def platform_deliveries(
    request: Request,
    status: str | None = Query(default=None),
) -> list[WebhookDeliveryResponse]:
    settings = get_settings()
    with _platform_repo(settings) as repo:
        tenant = _require_tenant_context(request, repo, settings=settings)
        return [
            WebhookDeliveryResponse.model_validate(item.model_dump())
            for item in repo.list_deliveries(installation_id=tenant.id, status=status)
        ]


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


@contextmanager
def _platform_repo(settings: Any | None = None) -> Iterator[PlatformRepository]:
    active_settings = settings or get_settings()
    conn = connect(active_settings.platform_database_url)
    try:
        if (
            active_settings.platform_init_db_on_startup
            and active_settings.platform_database_url not in _initialized_platform_databases
        ):
            init_db(conn)
            _initialized_platform_databases.add(active_settings.platform_database_url)
        yield PlatformRepository(conn)
    finally:
        conn.close()


def _resolve_tenant_context(
    request: Request,
    repo: PlatformRepository,
    *,
    required: bool = True,
    settings: Any | None = None,
) -> TenantContext | None:
    active_settings = settings or get_settings()
    raw_header = request.headers.get(TENANT_HEADER)
    source: Literal["header", "default"] = "header"
    try:
        tenant_id = parse_tenant_id(raw_header)
    except TenantResolutionError as exc:
        raise HTTPException(
            status_code=400,
            detail={"message": str(exc), "run_id": ""},
        ) from exc

    if tenant_id is None and active_settings.platform_default_tenant_id is not None:
        tenant_id = active_settings.platform_default_tenant_id
        source = "default"

    if tenant_id is None:
        if required:
            raise HTTPException(
                status_code=400,
                detail={"message": "tenant required", "run_id": ""},
            )
        return None

    installation = repo.get_installation(tenant_id)
    if installation is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "tenant not found", "run_id": ""},
        )
    return tenant_context_from_installation(installation, source=source)


def _require_tenant_context(
    request: Request,
    repo: PlatformRepository,
    *,
    settings: Any | None = None,
) -> TenantContext:
    tenant = _resolve_tenant_context(request, repo, settings=settings)
    assert tenant is not None
    return tenant


def _tenant_context_response(tenant: TenantContext) -> TenantContextResponse:
    return TenantContextResponse(
        resolved=True,
        source=tenant.source,
        header_name=TENANT_HEADER,
        tenant=TenantSummaryResponse(
            id=tenant.id,
            name=tenant.name,
            github_installation_id=tenant.github_installation_id,
            account_login=tenant.account_login,
            account_type=tenant.account_type,
            status=tenant.status,
        ),
    )


def _run_response(run: Any, artifacts: ArtifactStore) -> ReviewRunResponse:
    artifact_paths = artifacts.metadata_for_run(run.run_id)
    for key in ("review_response_path", "run_summary_path", "publish_result_path"):
        value = getattr(run, key, "")
        if value:
            artifact_paths[key] = value
    return ReviewRunResponse.model_validate(
        {
            **run.model_dump(),
            "artifact_paths": artifact_paths,
        }
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Any, exc: HTTPException) -> Any:
    from fastapi.responses import JSONResponse

    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": str(exc.detail), "run_id": ""},
    )
