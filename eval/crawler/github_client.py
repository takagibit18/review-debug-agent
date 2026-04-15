"""GitHub API client for golden-set data collection."""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values, load_dotenv

BUG_HINT_PATTERN = re.compile(r"(fix|bug|vulnerability|security|regression)", re.IGNORECASE)
FEATURE_TITLE_PATTERN = re.compile(
    r"(\[feat\]|^feat[:(]|^feature\b|^refactor[:(]|^chore[:(]|^docs[:(]|^ci[:(]|^style[:(]|^perf[:(])",
    re.IGNORECASE,
)
DEP_BUMP_TITLE_PATTERN = re.compile(
    r"(^bump\s|^chore\(deps\)|^build\(deps\)|dependabot|renovate|"
    r"^\[security\]\s*bump|^update\s+\S+\s+from\s+\S+\s+to\s)",
    re.IGNORECASE,
)


def _repo_root() -> Path:
    """eval/crawler/github_client.py -> repository root."""
    return Path(__file__).resolve().parent.parent.parent


def _resolve_github_token(explicit: str | None) -> str:
    """Resolve PAT: explicit arg > repo .env file > os.environ (after load_dotenv override).

    Windows often defines an empty or stale ``GITHUB_TOKEN`` in user env; ``load_dotenv``
    default does not override it, so we read ``.env`` directly first, then
    ``load_dotenv(..., override=True)``.
    """
    if explicit is not None and explicit.strip():
        return explicit.strip().strip('"').strip("'")

    env_path = _repo_root() / ".env"
    if env_path.is_file():
        file_values = dotenv_values(env_path)
        for key in ("GITHUB_TOKEN", "GH_TOKEN", "github_token"):
            val = file_values.get(key)
            if val and str(val).strip():
                return str(val).strip().strip('"').strip("'")

    load_dotenv(env_path, override=True)
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "github_token"):
        raw = os.getenv(key)
        if raw and raw.strip():
            return raw.strip().strip('"').strip("'")
    return ""


@dataclass(slots=True)
class PullRequestCandidate:
    """Candidate PR with required metadata for fixture generation."""

    repo_full_name: str
    pr_number: int
    title: str
    html_url: str
    merged_at: str
    merge_commit_sha: str
    head_sha: str
    base_sha: str


class GithubCrawlerClient:
    """Async GitHub client wrapping repo search and PR retrieval."""

    def __init__(
        self,
        token: str | None = None,
        *,
        timeout: float = 30.0,
        base_url: str = "https://api.github.com",
    ) -> None:
        resolved = _resolve_github_token(token)
        common_headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._auth_token = resolved.strip()
        auth_headers = {**common_headers}
        if self._auth_token:
            # GitHub accepts ``Bearer`` (OAuth/PAT) and ``token`` (classic PAT); try Bearer first.
            auth_headers["Authorization"] = f"Bearer {self._auth_token}"
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=auth_headers,
            timeout=timeout,
            follow_redirects=True,
        )
        # Unauthenticated client for public-repo fallback when PAT is rejected (401).
        self._anon_client = httpx.AsyncClient(
            base_url=base_url,
            headers=common_headers,
            timeout=timeout,
            follow_redirects=True,
        )

    async def close(self) -> None:
        """Close underlying HTTP resources."""
        await self._client.aclose()
        await self._anon_client.aclose()

    @staticmethod
    def _is_rate_limit_response(resp: httpx.Response) -> bool:
        """True when GitHub is throttling (REST: often 403 + rate limit JSON)."""
        if resp.status_code == 429:
            return True
        if resp.status_code != 403:
            return False
        if resp.headers.get("X-RateLimit-Remaining") == "0":
            return True
        text = (resp.text or "").lower()
        return "rate limit" in text or "secondary rate limit" in text

    @staticmethod
    async def _sleep_for_rate_limit(resp: httpx.Response) -> None:
        """Wait until Retry-After or X-RateLimit-Reset (capped)."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = 60.0
        else:
            reset = resp.headers.get("X-RateLimit-Reset")
            if reset:
                try:
                    wait = max(0.0, float(int(reset)) - time.time()) + 1.0
                except ValueError:
                    wait = 60.0
            else:
                wait = 60.0
        wait = min(max(wait, 1.0), 3600.0)
        await asyncio.sleep(wait)

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET with Bearer -> token prefix -> anonymous fallback on 401; retry on rate limit."""
        merged = {**(extra_headers or {})}
        send_headers = merged if merged else None
        max_rate_retries = 8
        rate_attempt = 0
        while True:
            resp = await self._client.get(path, params=params, headers=send_headers)
            if resp.status_code == 401 and self._auth_token:
                resp = await self._client.get(
                    path,
                    params=params,
                    headers={**merged, "Authorization": f"token {self._auth_token}"},
                )
            if resp.status_code == 401:
                resp = await self._anon_client.get(path, params=params, headers=send_headers)
            if self._is_rate_limit_response(resp):
                if rate_attempt >= max_rate_retries:
                    return resp
                await self._sleep_for_rate_limit(resp)
                rate_attempt += 1
                continue
            return resp

    @staticmethod
    def _raise_api_error(resp: httpx.Response, path: str) -> None:
        """Raise with status, path, and short body (GitHub error JSON or snippet)."""
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            preview = (resp.text or "")[:400].replace("\n", " ")
            raise RuntimeError(
                f"GitHub API {resp.status_code} for {path}. Body preview: {preview!r}"
            ) from exc

    async def search_repositories(
        self,
        *,
        language: str = "python",
        stars: str = "10..500",
        pushed_since: str = "2025-01-01",
        sort: str = "updated",
        order: str = "desc",
        per_page: int = 20,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Search candidate repositories for fixture mining."""
        query = f"language:{language} stars:{stars} pushed:>{pushed_since}"
        params = {
            "q": query,
            "sort": sort,
            "order": order,
            "per_page": max(1, min(100, per_page)),
            "page": max(1, page),
        }
        path = "/search/repositories"
        resp = await self._get(path, params=params)
        self._raise_api_error(resp, path)
        payload = resp.json()
        items = payload.get("items", [])
        return items if isinstance(items, list) else []

    async def list_closed_pull_requests(
        self,
        repo_full_name: str,
        *,
        per_page: int = 30,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """List closed PRs for one repository.

        Returns an empty list if the repo is gone, renamed, or not visible (404/403),
        so discovery can skip it without aborting the crawl.
        """
        path = f"/repos/{repo_full_name}/pulls"
        resp = await self._get(
            path,
            params={
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": max(1, min(100, per_page)),
                "page": max(1, page),
            },
        )
        if resp.status_code == 404:
            return []
        if resp.status_code == 403 and not self._is_rate_limit_response(resp):
            return []
        self._raise_api_error(resp, path)
        payload = resp.json()
        return payload if isinstance(payload, list) else []

    async def get_pull_request(self, repo_full_name: str, pr_number: int) -> dict[str, Any]:
        """Fetch full PR detail."""
        path = f"/repos/{repo_full_name}/pulls/{pr_number}"
        resp = await self._get(path)
        self._raise_api_error(resp, path)
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}

    async def get_pull_request_diff(self, repo_full_name: str, pr_number: int) -> str:
        """Fetch unified diff for one PR."""
        path = f"/repos/{repo_full_name}/pulls/{pr_number}"
        resp = await self._get(
            path,
            extra_headers={"Accept": "application/vnd.github.v3.diff"},
        )
        self._raise_api_error(resp, path)
        return resp.text or ""

    async def get_pull_request_files(
        self,
        repo_full_name: str,
        pr_number: int,
        *,
        per_page: int = 100,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """List file changes for a PR."""
        path = f"/repos/{repo_full_name}/pulls/{pr_number}/files"
        resp = await self._get(
            path,
            params={
                "per_page": max(1, min(100, per_page)),
                "page": max(1, page),
            },
        )
        self._raise_api_error(resp, path)
        payload = resp.json()
        return payload if isinstance(payload, list) else []

    async def get_file_content(self, repo_full_name: str, path: str, ref: str) -> str:
        """Fetch raw file content from repository ref."""
        api_path = f"/repos/{repo_full_name}/contents/{path}"
        resp = await self._get(
            api_path,
            params={"ref": ref},
            extra_headers={"Accept": "application/vnd.github.raw+json"},
        )
        if resp.status_code == 404:
            return ""
        self._raise_api_error(resp, api_path)
        return resp.text or ""

    async def discover_pull_request_candidates(
        self,
        *,
        max_repos: int = 10,
        max_prs_per_repo: int = 5,
        min_changed_lines: int = 10,
        max_changed_lines: int = 500,
    ) -> list[PullRequestCandidate]:
        """Automatically discover merged bug-fix PR candidates."""
        repos = await self.search_repositories(per_page=max_repos)
        candidates: list[PullRequestCandidate] = []
        for repo in repos[:max_repos]:
            full_name = str(repo.get("full_name", "")).strip()
            if not full_name:
                continue
            prs = await self.list_closed_pull_requests(full_name, per_page=max_prs_per_repo * 3)
            picked = 0
            for pr in prs:
                if picked >= max_prs_per_repo:
                    break
                if not self._is_candidate(pr):
                    continue
                # List pulls response often omits additions/deletions; only enforce size when present.
                if "additions" in pr or "deletions" in pr:
                    changed = int(pr.get("additions", 0) or 0) + int(
                        pr.get("deletions", 0) or 0
                    )
                    if changed < min_changed_lines or changed > max_changed_lines:
                        continue
                pr_number = int(pr.get("number", 0) or 0)
                if pr_number <= 0:
                    continue
                files = await self.get_pull_request_files(full_name, pr_number)
                title = str(pr.get("title", "") or "")
                if self._is_dependency_only_change(title, files):
                    continue
                candidates.append(
                    PullRequestCandidate(
                        repo_full_name=full_name,
                        pr_number=pr_number,
                        title=str(pr.get("title", "") or ""),
                        html_url=str(pr.get("html_url", "") or ""),
                        merged_at=str(pr.get("merged_at", "") or ""),
                        merge_commit_sha=str(pr.get("merge_commit_sha", "") or ""),
                        head_sha=str((pr.get("head") or {}).get("sha", "") or ""),
                        base_sha=str((pr.get("base") or {}).get("sha", "") or ""),
                    )
                )
                picked += 1
        return candidates

    @staticmethod
    def _is_candidate(pr_payload: dict[str, Any]) -> bool:
        merged_at = str(pr_payload.get("merged_at", "") or "").strip()
        if not merged_at:
            return False

        title = str(pr_payload.get("title", "") or "")
        if FEATURE_TITLE_PATTERN.search(title):
            return False
        if DEP_BUMP_TITLE_PATTERN.search(title):
            return False
        if BUG_HINT_PATTERN.search(title):
            return True

        labels = pr_payload.get("labels", [])
        if not isinstance(labels, list):
            return False
        for label in labels:
            name = ""
            if isinstance(label, dict):
                name = str(label.get("name", "") or "")
            if BUG_HINT_PATTERN.search(name):
                return True
        return False

    @staticmethod
    def _is_dependency_only_change(title: str, files: list[dict[str, Any]]) -> bool:
        """Filter out dependency/CI-only PRs from the golden set."""
        if DEP_BUMP_TITLE_PATTERN.search(title or ""):
            return True
        if not files:
            return False
        low_signal_count = 0
        for file_item in files:
            path = str(file_item.get("filename", "") or "").strip().lower()
            if not path:
                continue
            if GithubCrawlerClient._is_low_signal_path(path):
                low_signal_count += 1
        return low_signal_count == len(files)

    @staticmethod
    def _is_low_signal_path(path: str) -> bool:
        if path.endswith(".lock"):
            return True
        if path in {"requirements.txt", "requirements-dev.txt"}:
            return True
        if path.endswith("/requirements.txt") or path.endswith("/requirements-dev.txt"):
            return True
        if path in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pipfile.lock"}:
            return True
        if path.startswith(".github/workflows/") and (path.endswith(".yml") or path.endswith(".yaml")):
            return True
        return False

