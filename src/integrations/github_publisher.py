"""GitHub advisory publishing and comment lifecycle planning."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field, model_validator

from src.analyzer.schemas import ReviewResponse
from src.integrations.github_adapter import (
    GitHubAdvisoryPayload,
    InlineCommentCandidate,
    build_github_advisory_payload,
)

GITHUB_COMMENT_MARKER = (
    os.getenv("GITHUB_ADVISORY_COMMENT_MARKER", "<!-- mergewarden:comment -->").strip()
    or "<!-- mergewarden:comment -->"
)
_METADATA_PREFIX = "<!-- mergewarden:"
_METADATA_SUFFIX = " -->"


class GitHubPublishRequest(BaseModel):
    """Inputs needed to publish a review advisory to one pull request."""

    owner_repo: str
    pr_number: int = Field(ge=1)
    head_sha: str = Field(min_length=1)
    response: ReviewResponse
    changed_lines: dict[str, list[int]] = Field(default_factory=dict)
    dry_run: bool = True


class CommentMetadata(BaseModel):
    """Hidden metadata embedded in MergeWarden-owned comments."""

    tool: str = "mergewarden"
    run_id: str = ""
    fingerprint: str
    head_sha: str = ""


class PendingReviewComment(BaseModel):
    """Review comment body ready for lifecycle planning or publication."""

    path: str
    line: int
    body: str
    fingerprint: str


class CommentUpdate(BaseModel):
    """Existing comment update operation."""

    comment_id: int
    fingerprint: str
    body: str


class PublishedCommentRecord(BaseModel):
    """Published or updated GitHub review comment metadata."""

    comment_id: int
    fingerprint: str
    path: str = ""
    line: int | None = None
    html_url: str = ""
    action: str


class CommentLifecyclePlan(BaseModel):
    """Create/update/stale operations for MergeWarden-owned comments."""

    create: list[PendingReviewComment] = Field(default_factory=list)
    update: list[CommentUpdate] = Field(default_factory=list)
    stale: list[CommentUpdate] = Field(default_factory=list)
    unchanged: list[PublishedCommentRecord] = Field(default_factory=list)
    summary_only_count: int = 0
    foreign_comment_count: int = 0
    create_count: int = 0
    update_count: int = 0
    stale_count: int = 0

    @model_validator(mode="after")
    def _set_counts(self) -> "CommentLifecyclePlan":
        self.create_count = len(self.create)
        self.update_count = len(self.update)
        self.stale_count = len(self.stale)
        return self


class GitHubPublishResult(BaseModel):
    """Result of a dry-run or real GitHub advisory publication."""

    status: str
    owner_repo: str
    pr_number: int
    head_sha: str
    run_id: str
    advisory_payload: GitHubAdvisoryPayload
    lifecycle_plan: CommentLifecyclePlan
    check_run: dict[str, Any] = Field(default_factory=dict)
    inline_comment_records: list[PublishedCommentRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class GitHubPublisherClient(Protocol):
    """Minimal GitHub client protocol used by the publisher."""

    async def list_review_comments(self, owner_repo: str, pr_number: int) -> list[dict[str, Any]]: ...

    async def create_check_run(self, owner_repo: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def create_review_comment(
        self,
        owner_repo: str,
        pr_number: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def update_review_comment(
        self,
        owner_repo: str,
        comment_id: int,
        body: str,
    ) -> dict[str, Any]: ...


class GitHubApiClient:
    """Small async GitHub REST client for GitHub Actions publishing."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
    ) -> None:
        if not token.strip():
            raise ValueError("GITHUB_TOKEN is required for publishing.")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token.strip()}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def list_review_comments(self, owner_repo: str, pr_number: int) -> list[dict[str, Any]]:
        path = f"/repos/{owner_repo}/pulls/{pr_number}/comments"
        resp = await self._client.get(path, params={"per_page": 100})
        self._raise_api_error(resp, path)
        payload = resp.json()
        return payload if isinstance(payload, list) else []

    async def create_check_run(self, owner_repo: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = f"/repos/{owner_repo}/check-runs"
        resp = await self._client.post(path, json=payload)
        self._raise_api_error(resp, path)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def create_review_comment(
        self,
        owner_repo: str,
        pr_number: int,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        path = f"/repos/{owner_repo}/pulls/{pr_number}/comments"
        resp = await self._client.post(path, json=payload)
        self._raise_api_error(resp, path)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def update_review_comment(
        self,
        owner_repo: str,
        comment_id: int,
        body: str,
    ) -> dict[str, Any]:
        path = f"/repos/{owner_repo}/pulls/comments/{comment_id}"
        resp = await self._client.patch(path, json={"body": body})
        self._raise_api_error(resp, path)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _raise_api_error(resp: httpx.Response, path: str) -> None:
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            preview = (resp.text or "")[:400].replace("\n", " ")
            raise RuntimeError(
                f"GitHub API {resp.status_code} for {path}. Body preview: {preview!r}"
            ) from exc


class GitHubPublisher:
    """Publish MergeWarden advisory checks and comments."""

    def __init__(self, client: GitHubPublisherClient) -> None:
        self._client = client

    def build_publish_plan(
        self,
        request: GitHubPublishRequest,
        *,
        existing_comments: list[dict[str, Any]] | None = None,
    ) -> GitHubPublishResult:
        advisory = build_github_advisory_payload(request.response, request.changed_lines)
        candidates = [
            _build_pending_comment(item, request.response.run_id, request.head_sha)
            for item in advisory.inline_comments
        ]
        lifecycle = build_comment_lifecycle_plan(
            candidates=candidates,
            existing_comments=existing_comments or [],
            run_id=request.response.run_id,
            head_sha=request.head_sha,
            summary_only_count=len(advisory.summary_only_issues),
        )
        return GitHubPublishResult(
            status="dry_run" if request.dry_run else "planned",
            owner_repo=request.owner_repo,
            pr_number=request.pr_number,
            head_sha=request.head_sha,
            run_id=request.response.run_id,
            advisory_payload=advisory,
            lifecycle_plan=lifecycle,
            check_run=_build_check_run_payload(request, advisory),
        )

    async def publish(self, request: GitHubPublishRequest) -> GitHubPublishResult:
        """Publish or dry-run the advisory."""
        if request.dry_run:
            return self.build_publish_plan(request)
        existing_comments = await self._client.list_review_comments(
            request.owner_repo,
            request.pr_number,
        )
        result = self.build_publish_plan(request, existing_comments=existing_comments)
        check_run = await self._client.create_check_run(request.owner_repo, result.check_run)
        records: list[PublishedCommentRecord] = []
        for create_item in result.lifecycle_plan.create:
            created = await self._client.create_review_comment(
                request.owner_repo,
                request.pr_number,
                {
                    "body": create_item.body,
                    "path": create_item.path,
                    "line": create_item.line,
                    "side": "RIGHT",
                },
            )
            records.append(
                PublishedCommentRecord(
                    comment_id=int(created.get("id", 0) or 0),
                    fingerprint=create_item.fingerprint,
                    path=create_item.path,
                    line=create_item.line,
                    html_url=str(created.get("html_url", "") or ""),
                    action="created",
                )
            )
        for update_item in result.lifecycle_plan.update:
            updated = await self._client.update_review_comment(
                request.owner_repo,
                update_item.comment_id,
                update_item.body,
            )
            records.append(
                PublishedCommentRecord(
                    comment_id=update_item.comment_id,
                    fingerprint=update_item.fingerprint,
                    html_url=str(updated.get("html_url", "") or ""),
                    action="updated",
                )
            )
        for stale_item in result.lifecycle_plan.stale:
            updated = await self._client.update_review_comment(
                request.owner_repo,
                stale_item.comment_id,
                stale_item.body,
            )
            records.append(
                PublishedCommentRecord(
                    comment_id=stale_item.comment_id,
                    fingerprint=stale_item.fingerprint,
                    html_url=str(updated.get("html_url", "") or ""),
                    action="stale",
                )
            )
        result.status = "published"
        result.check_run = check_run
        result.inline_comment_records = records
        return result

    def publish_sync(self, request: GitHubPublishRequest) -> GitHubPublishResult:
        """Synchronous wrapper for Click tests and commands."""
        return asyncio.run(self.publish(request))


def build_comment_lifecycle_plan(
    *,
    candidates: list[PendingReviewComment],
    existing_comments: list[dict[str, Any]],
    run_id: str,
    head_sha: str,
    summary_only_count: int = 0,
) -> CommentLifecyclePlan:
    """Build create/update/stale operations from current candidates and comments."""
    existing_by_fingerprint: dict[str, dict[str, Any]] = {}
    foreign_count = 0
    for raw in existing_comments:
        body = str(raw.get("body", "") or "")
        metadata = extract_comment_metadata(body)
        if metadata is None:
            foreign_count += 1
            continue
        existing_by_fingerprint[metadata.fingerprint] = raw

    create: list[PendingReviewComment] = []
    update: list[CommentUpdate] = []
    unchanged: list[PublishedCommentRecord] = []
    active_fingerprints = {candidate.fingerprint for candidate in candidates}
    for candidate in candidates:
        existing = existing_by_fingerprint.get(candidate.fingerprint)
        if existing is None:
            create.append(candidate)
            continue
        comment_id = int(existing.get("id", 0) or 0)
        if str(existing.get("body", "") or "") == candidate.body:
            unchanged.append(
                PublishedCommentRecord(
                    comment_id=comment_id,
                    fingerprint=candidate.fingerprint,
                    path=str(existing.get("path", "") or candidate.path),
                    line=int(existing.get("line", candidate.line) or candidate.line),
                    html_url=str(existing.get("html_url", "") or ""),
                    action="unchanged",
                )
            )
            continue
        update.append(
            CommentUpdate(
                comment_id=comment_id,
                fingerprint=candidate.fingerprint,
                body=candidate.body,
            )
        )

    stale: list[CommentUpdate] = []
    for fingerprint, raw in existing_by_fingerprint.items():
        if fingerprint in active_fingerprints:
            continue
        comment_id = int(raw.get("id", 0) or 0)
        stale.append(
            CommentUpdate(
                comment_id=comment_id,
                fingerprint=fingerprint,
                body=_stale_body(str(raw.get("body", "") or ""), run_id, head_sha),
            )
        )

    return CommentLifecyclePlan(
        create=create,
        update=update,
        stale=stale,
        unchanged=unchanged,
        summary_only_count=summary_only_count,
        foreign_comment_count=foreign_count,
    )


def extract_comment_metadata(body: str) -> CommentMetadata | None:
    """Extract MergeWarden metadata from a review comment body."""
    if GITHUB_COMMENT_MARKER not in body:
        return None
    start = body.rfind(_METADATA_PREFIX)
    if start < 0:
        return None
    start += len(_METADATA_PREFIX)
    end = body.find(_METADATA_SUFFIX, start)
    if end < 0:
        return None
    try:
        raw = json.loads(body[start:end])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or not raw.get("fingerprint"):
        return None
    return CommentMetadata.model_validate(raw)


def resolve_github_token(explicit: str | None = None) -> str:
    """Resolve the GitHub token accepted by GitHub Actions and local runs."""
    if explicit and explicit.strip():
        return explicit.strip()
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "github_token"):
        raw = os.getenv(key)
        if raw and raw.strip():
            return raw.strip()
    return ""


def _build_pending_comment(
    candidate: InlineCommentCandidate,
    run_id: str,
    head_sha: str,
) -> PendingReviewComment:
    metadata = CommentMetadata(
        run_id=run_id,
        fingerprint=candidate.fingerprint,
        head_sha=head_sha,
    )
    metadata_json = json.dumps(metadata.model_dump(), sort_keys=True, separators=(",", ":"))
    body = "\n\n".join(
        [
            candidate.body,
            GITHUB_COMMENT_MARKER,
            f"<!-- mergewarden:{metadata_json} -->",
        ]
    )
    return PendingReviewComment(
        path=candidate.path,
        line=candidate.line,
        body=body,
        fingerprint=candidate.fingerprint,
    )


def _stale_body(existing_body: str, run_id: str, head_sha: str) -> str:
    existing_metadata = extract_comment_metadata(existing_body)
    metadata = CommentMetadata(
        run_id=run_id,
        fingerprint=existing_metadata.fingerprint if existing_metadata else "unknown",
        head_sha=head_sha,
    )
    metadata_json = json.dumps(metadata.model_dump(), sort_keys=True, separators=(",", ":"))
    return "\n\n".join(
        [
            "Stale MergeWarden advisory: this finding was not reproduced in the latest run.",
            GITHUB_COMMENT_MARKER,
            f"<!-- mergewarden:{metadata_json} -->",
        ]
    )


def _build_check_run_payload(
    request: GitHubPublishRequest,
    advisory: GitHubAdvisoryPayload,
) -> dict[str, Any]:
    return {
        "name": "MergeWarden advisory",
        "head_sha": request.head_sha,
        "status": "completed",
        "conclusion": "neutral",
        "output": {
            "title": "MergeWarden advisory",
            "summary": advisory.check_summary,
            "text": _build_check_text(advisory),
        },
    }


def _build_check_text(advisory: GitHubAdvisoryPayload) -> str:
    parts = [advisory.check_summary]
    if advisory.summary_only_issues:
        parts.append("")
        parts.append("Summary-only findings:")
        for item in advisory.summary_only_issues:
            parts.append(
                f"- {item.issue.severity.value} {item.issue.location}: {item.issue.suggestion}"
            )
    return "\n".join(parts)
