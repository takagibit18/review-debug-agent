"""Full credential-independent runtime regression across API, agent, and worker."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from src.analyzer.event_log import EventType
from src.analyzer.run_summary import summarize_run_artifacts
from src.analyzer.schemas import ReviewRequest
from src.api.app import app
from src.config import get_settings
from src.integrations.github_publisher import (
    GitHubPublishRequest,
    GitHubPublisher,
)
from src.orchestrator.agent_loop import AgentOrchestrator
from src.platform.db import connect, init_db
from src.platform.repositories import PlatformRepository
from src.platform.worker import PlatformWorker, ReviewPipelineResult

from .test_agent_openai_compatible import (
    _build_git_fixture,
    _fake_provider,
    _tool_names,
)


class _OfflinePublisherClient:
    def __init__(self) -> None:
        self.check_payloads: list[dict[str, Any]] = []
        self.comment_payloads: list[dict[str, Any]] = []

    async def list_review_comments(
        self,
        owner_repo: str,
        pr_number: int,
    ) -> list[dict[str, Any]]:
        assert owner_repo == "offline/runtime-fixture"
        assert pr_number == 17
        return []

    async def list_check_runs(
        self,
        owner_repo: str,
        head_sha: str,
        check_name: str,
    ) -> list[dict[str, Any]]:
        assert owner_repo == "offline/runtime-fixture"
        assert head_sha
        assert check_name == "MergeWarden advisory"
        return []

    async def create_check_run(
        self,
        owner_repo: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        assert owner_repo == "offline/runtime-fixture"
        self.check_payloads.append(payload)
        return {"id": 7001, **payload}

    async def update_check_run(
        self,
        owner_repo: str,
        check_run_id: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise AssertionError("offline regression must create its first check")

    async def create_review_comment(
        self,
        owner_repo: str,
        pr_number: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        assert owner_repo == "offline/runtime-fixture"
        assert pr_number == 17
        self.comment_payloads.append(payload)
        return {
            "id": 8001,
            "html_url": "https://example.invalid/offline-comment/8001",
        }

    async def update_review_comment(
        self,
        owner_repo: str,
        comment_id: int,
        body: str,
    ) -> dict[str, Any]:
        raise AssertionError("offline regression must not update existing comments")


def test_full_signed_webhook_agent_publish_artifact_runtime(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    local_repo = tmp_path / "local-repo"
    local_repo.mkdir()
    base_sha, head_sha, diff_text = _build_git_fixture(local_repo)
    database_path = tmp_path / "platform.db"
    artifact_root = tmp_path / "artifacts"
    event_log_root = tmp_path / "agent-event-logs"
    secret = "offline-runtime-secret"
    _configure_runtime(
        monkeypatch,
        database_path=database_path,
        artifact_root=artifact_root,
        event_log_root=event_log_root,
        secret=secret,
    )

    publisher_client = _OfflinePublisherClient()
    with _fake_provider() as provider:
        host, port = provider.server_address
        monkeypatch.setenv("OPENAI_BASE_URL", f"http://{host}:{port}/v1")

        payload = _pull_request_payload(base_sha=base_sha, head_sha=head_sha)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with TestClient(app) as client:
            webhook = client.post(
                "/github/webhook",
                content=body,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "offline-full-regression-17",
                    "X-Hub-Signature-256": f"sha256={signature}",
                },
            )

        assert webhook.status_code == 200
        assert webhook.json()["status"] == "queued"
        platform_run_id = webhook.json()["run_id"]

        async def review_and_publish(run, config):  # type: ignore[no-untyped-def]
            assert run.base_sha == base_sha
            assert run.head_sha == head_sha
            orchestrator = AgentOrchestrator(
                review_max_iterations=3,
                review_min_tool_iterations=2,
                review_workflow_enforcement="off",
                review_diff_first_changed_files=False,
            )
            try:
                response = await orchestrator.run_review(
                    ReviewRequest(
                        repo_path=str(local_repo),
                        diff_mode=True,
                        diff_text=diff_text,
                        model_name=config.model_name,
                    )
                )
            finally:
                if orchestrator._model_client is not None:  # noqa: SLF001
                    await orchestrator._model_client.close()  # noqa: SLF001

            publish_result = await GitHubPublisher(publisher_client).publish(
                GitHubPublishRequest(
                    owner_repo=run.repo_full_name,
                    pr_number=run.pr_number,
                    head_sha=run.head_sha,
                    response=response,
                    changed_lines={"pkg/service.py": [2]},
                    dry_run=False,
                    publish_comments=config.publish_comments,
                )
            )
            event_log_path = event_log_root / f"{response.run_id}.jsonl"
            run_summary = summarize_run_artifacts(
                run_id=response.run_id,
                event_log_path=event_log_path,
                publish_status="published",
            )
            return ReviewPipelineResult(
                review_response=response,
                publish_result=publish_result,
                run_summary=run_summary,
                diff_text=diff_text,
                changed_lines={"pkg/service.py": [2]},
                event_log_paths=[str(event_log_path)],
                prompt_tokens=30,
                completion_tokens=15,
                total_tokens=run_summary.event_log.total_tokens,
                model_name=config.model_name,
            )

        settings = get_settings()
        with connect(settings.platform_database_url) as conn:
            init_db(conn)
            repository = PlatformRepository(conn)
            worker = PlatformWorker(
                repository,
                settings=settings,
                pipeline=review_and_publish,
                worker_id="offline-full-worker",
            )
            assert worker.run_once() is True
            run = repository.get_run(platform_run_id)
            checkpoints = repository.list_checkpoints(platform_run_id)
            usage = repository.list_usage_records(run_id=platform_run_id)

    assert len(provider.requests) == 3
    assert _tool_names(provider.requests[0]) >= {"get_changed_context", "read_file"}
    assert any(
        message.get("role") == "tool"
        and message.get("tool_call_id") == "call-context"
        for message in provider.requests[1]["messages"]
    )
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call-read"
        for message in provider.requests[2]["messages"]
    )

    assert run is not None
    assert run.status == "succeeded"
    assert run.head_sha == head_sha
    assert run.publish_status == "published"
    assert [(item.step_id, item.status) for item in checkpoints] == [
        ("review_pipeline", "completed"),
        ("persist_artifacts", "completed"),
    ]
    assert len(usage) == 1
    assert usage[0].prompt_tokens == 30
    assert usage[0].completion_tokens == 15
    assert usage[0].total_tokens == 45

    review_artifact = json.loads(
        (artifact_root / run.review_response_path).read_text(encoding="utf-8")
    )
    publish_artifact = json.loads(
        (artifact_root / run.publish_result_path).read_text(encoding="utf-8")
    )
    assert review_artifact["report"]["issues"][0]["location"] == "pkg/service.py:2"
    assert publish_artifact["status"] == "published"
    assert publisher_client.check_payloads[0]["head_sha"] == head_sha
    assert publisher_client.check_payloads[0]["external_id"] == (
        f"mergewarden:offline/runtime-fixture:17:{head_sha}"
    )
    assert len(publisher_client.comment_payloads) == 1
    comment_payload = publisher_client.comment_payloads[0]
    assert comment_payload["commit_id"] == head_sha
    assert comment_payload["path"] == "pkg/service.py"
    assert comment_payload["line"] == 2
    assert comment_payload["side"] == "RIGHT"
    assert "<!-- mergewarden:comment -->" in comment_payload["body"]

    persisted_event_logs = list((artifact_root / platform_run_id / "event_logs").glob("*.jsonl"))
    assert len(persisted_event_logs) == 1
    events = [
        json.loads(line)
        for line in persisted_event_logs[0].read_text(encoding="utf-8").splitlines()
    ]
    integrity_event = next(
        event
        for event in events
        if event["event_type"] == EventType.FINDING_VERIFICATION_COMPLETED.value
    )
    assert integrity_event["payload"]["verifier_kind"] == "integrity_guard"
    assert integrity_event["payload"]["accepted_count"] == 1


def _configure_runtime(
    monkeypatch,
    *,
    database_path: Path,
    artifact_root: Path,
    event_log_root: Path,
    secret: str,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    monkeypatch.setenv("PLATFORM_DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("PLATFORM_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("PLATFORM_REVIEW_ENABLED", "true")
    monkeypatch.setenv("PLATFORM_PUBLISH_COMMENTS", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder-not-a-secret")
    monkeypatch.setenv("MODEL_NAME", "offline-reviewer")
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("MODEL_MAX_RETRIES", "1")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("REVIEW_CONTEXT_MODE", "agent_search")
    monkeypatch.setenv("ROOT_CAUSE_CONSOLIDATION_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_DIR", str(event_log_root))


def _pull_request_payload(*, base_sha: str, head_sha: str) -> dict[str, Any]:
    return {
        "action": "opened",
        "number": 17,
        "repository": {
            "full_name": "offline/runtime-fixture",
            "name": "runtime-fixture",
            "owner": {"login": "offline"},
            "default_branch": "main",
        },
        "pull_request": {
            "number": 17,
            "draft": False,
            "head": {"sha": head_sha},
            "base": {"sha": base_sha},
        },
        "installation": {
            "id": 9017,
            "account": {"login": "offline", "type": "Organization"},
        },
    }
