"""Minimal DB-polling worker for platform review runs."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from src.analyzer.run_summary import summarize_run_artifacts
from src.config import Settings
from src.integrations.github_pr_review import (
    GitHubPullRequestReviewTrigger,
    execute_github_pull_request_review,
)
from src.platform.artifacts import ArtifactStore
from src.platform.config import EffectiveTenantConfig, resolve_effective_config
from src.platform.models import ReviewRunRecord
from src.platform.repositories import PlatformRepository

logger = logging.getLogger(__name__)


class ReviewPipelineResult(BaseModel):
    review_response: Any = None
    publish_result: Any = None
    run_summary: Any = None
    diff_text: str = ""
    changed_lines: dict[str, list[int]] = Field(default_factory=dict)
    event_log_paths: list[str] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    model_name: str = ""


ReviewPipeline = Callable[[ReviewRunRecord, EffectiveTenantConfig], Any]


class PlatformWorker:
    """Poll queued review runs and process them one at a time."""

    def __init__(
        self,
        repo: PlatformRepository,
        *,
        settings: Settings,
        pipeline: ReviewPipeline | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.pipeline = pipeline or self._default_pipeline
        self.artifact_store = artifact_store or ArtifactStore(settings.platform_artifact_root)

    def run_once(self) -> bool:
        """Process one queued run if available."""
        run = self.repo.claim_next_queued_run()
        if run is None:
            return False
        try:
            config = resolve_effective_config(
                self.repo,
                settings=self.settings,
                installation_id=run.installation_id,
                repository_id=run.repository_id,
            )
            result = self._execute_pipeline(run, config)
            paths = self.artifact_store.save_review_artifacts(run.run_id, result)
            publish_status = _publish_status(result.publish_result)
            total_tokens = result.total_tokens or _summary_total_tokens(result.run_summary)
            self.repo.mark_run_succeeded(
                run.run_id,
                review_response_path=paths.get("review_response_path", ""),
                run_summary_path=paths.get("run_summary_path", ""),
                publish_result_path=paths.get("publish_result_path", ""),
                total_tokens=total_tokens,
                publish_status=publish_status,
            )
            self.repo.create_usage_record(
                run_id=run.run_id,
                installation_id=run.installation_id,
                repository_id=run.repository_id,
                model_name=result.model_name or config.model_name,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                total_tokens=total_tokens or 0,
                duration_ms=result.duration_ms,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.exception("platform worker run failed", extra={"run_id": run.run_id})
            self.repo.mark_run_failed(
                run.run_id,
                error_type=exc.__class__.__name__,
                error_message=str(exc)[:1000],
            )
            return True

    def run_forever(self, *, poll_interval_seconds: float | None = None) -> None:
        """Poll forever. This MVP is intended for one local worker process."""
        interval = poll_interval_seconds or self.settings.platform_worker_poll_interval_seconds
        while True:
            processed = self.run_once()
            if not processed:
                time.sleep(interval)

    def _execute_pipeline(
        self,
        run: ReviewRunRecord,
        config: EffectiveTenantConfig,
    ) -> ReviewPipelineResult:
        result = self.pipeline(run, config)
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        if isinstance(result, ReviewPipelineResult):
            return result
        return ReviewPipelineResult.model_validate(result)

    async def _default_pipeline(
        self,
        run: ReviewRunRecord,
        config: EffectiveTenantConfig,
    ) -> ReviewPipelineResult:
        installation = self.repo.get_installation(run.installation_id)
        github_installation_id = (
            installation.github_installation_id
            if installation is not None and installation.github_installation_id > 0
            else None
        )
        execution = await execute_github_pull_request_review(
            GitHubPullRequestReviewTrigger(
                owner_repo=run.repo_full_name,
                pull_number=run.pr_number,
                head_sha=run.head_sha,
                base_sha=run.base_sha,
                installation_id=github_installation_id,
                trigger_event=run.trigger_event,
                action=run.trigger_action,
            ),
            publish_comments=config.publish_comments,
            model_name=config.model_name,
        )
        response_run_id = execution.review_response.run_id
        event_log_path = _resolve_event_log_path(self.settings.event_log_dir, response_run_id)
        summary = summarize_run_artifacts(
            run_id=response_run_id,
            event_log_path=event_log_path,
            response_json_path=None,
            publish_result_json_path=None,
            publish_status=_publish_status(execution.publish_result),
        )
        return ReviewPipelineResult(
            review_response=execution.review_response,
            publish_result=execution.publish_result,
            run_summary=summary,
            diff_text=execution.diff_text,
            changed_lines=execution.changed_lines,
            event_log_paths=[str(event_log_path)] if event_log_path.exists() else [],
            total_tokens=summary.event_log.total_tokens,
            model_name=(summary.event_log.model_names[0] if summary.event_log.model_names else config.model_name),
        )


def _resolve_event_log_path(log_dir: str, run_id: str) -> Path:
    return Path(log_dir) / f"{run_id}.jsonl"


def _publish_status(payload: Any) -> str:
    if payload is None:
        return ""
    if hasattr(payload, "status"):
        return str(payload.status)
    if isinstance(payload, dict):
        return str(payload.get("status", "") or "")
    return ""


def _summary_total_tokens(payload: Any) -> int:
    if payload is None:
        return 0
    if hasattr(payload, "event_log") and hasattr(payload.event_log, "total_tokens"):
        return int(payload.event_log.total_tokens or 0)
    if hasattr(payload, "total_tokens"):
        return int(payload.total_tokens or 0)
    if isinstance(payload, dict):
        return int(payload.get("total_tokens", 0) or 0)
    return 0
