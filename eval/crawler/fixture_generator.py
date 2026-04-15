"""Golden fixture generation pipeline."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Iterable

from eval.crawler.annotator import LLMAnnotator
from eval.crawler.github_client import GithubCrawlerClient, PullRequestCandidate
from eval.crawler.pr_parser import build_fixture_input, build_fixture_source, parse_unified_diff
from eval.schemas import Fixture, FixtureManifest, FixtureManifestEntry


class FixtureGenerator:
    """Generate fixture files and manifest from discovered PRs."""

    def __init__(
        self,
        fixtures_dir: str | Path = Path("eval") / "fixtures",
        outputs_dir: str | Path = Path("eval") / "outputs",
        *,
        github_client: GithubCrawlerClient | None = None,
        annotator: LLMAnnotator | None = None,
        min_expected_issues: int = 0,
    ) -> None:
        self._fixtures_dir = Path(fixtures_dir)
        self._outputs_dir = Path(outputs_dir)
        self._fixtures_dir.mkdir(parents=True, exist_ok=True)
        self._outputs_dir.mkdir(parents=True, exist_ok=True)
        self._github_client = github_client or GithubCrawlerClient()
        self._annotator = annotator or LLMAnnotator()
        self._min_expected_issues = max(0, min_expected_issues)

    async def close(self) -> None:
        """Close internal clients."""
        await self._github_client.close()
        await self._annotator.close()

    async def generate(
        self,
        *,
        suite: str = "golden",
        max_repos: int = 10,
        max_prs_per_repo: int = 5,
        curated_repos: list[str] | None = None,
        concurrency: int = 3,
    ) -> list[Path]:
        """Discover PRs and create fixture files."""
        existing_keys = self._load_existing_keys()
        written_paths: list[Path] = []
        started_at = datetime.now(UTC)
        crawl_entries: list[dict[str, object]] = []
        requested_repos = (
            min(len(curated_repos), max_repos) if curated_repos else max_repos
        )

        candidates = await self._github_client.discover_pull_request_candidates(
            max_repos=max_repos,
            max_prs_per_repo=max_prs_per_repo,
            curated_repos=curated_repos,
        )

        pending_candidates: list[PullRequestCandidate] = []
        for candidate in candidates:
            key = (candidate.repo_full_name, candidate.pr_number)
            if key in existing_keys:
                crawl_entries.append(
                    {
                        "repo": candidate.repo_full_name,
                        "pr_number": candidate.pr_number,
                        "stage": "dedupe",
                        "outcome": "skipped",
                        "reason": "already_exists",
                        "duration_ms": 0,
                    }
                )
                continue
            pending_candidates.append(candidate)

        semaphore = asyncio.Semaphore(max(1, concurrency))

        async def process_candidate(candidate: PullRequestCandidate) -> dict[str, object]:
            started = perf_counter()
            entry: dict[str, object] = {
                "repo": candidate.repo_full_name,
                "pr_number": candidate.pr_number,
                "stage": "annotate",
                "outcome": "error",
                "reason": "",
                "issues_draft": 0,
                "issues_after_critique": 0,
                "fixture_written": False,
                "duration_ms": 0,
            }
            try:
                async with semaphore:
                    fixture, diagnostics = await self._create_fixture(candidate, suite=suite)
            except Exception as exc:  # noqa: BLE001
                entry["reason"] = str(exc)
                entry["duration_ms"] = int((perf_counter() - started) * 1000)
                return entry

            entry["issues_draft"] = int(diagnostics.get("issues_draft", 0))
            entry["issues_after_critique"] = int(diagnostics.get("issues_after_critique", 0))
            expected_count = len(fixture.expected.issues)
            if fixture.expected.is_empty_annotation:
                entry["outcome"] = "skipped"
                entry["reason"] = "empty_annotation"
                entry["duration_ms"] = int((perf_counter() - started) * 1000)
                return entry
            if expected_count < self._min_expected_issues:
                entry["outcome"] = "skipped"
                entry["reason"] = "below_threshold"
                entry["duration_ms"] = int((perf_counter() - started) * 1000)
                return entry

            entry["outcome"] = "ready"
            entry["reason"] = ""
            entry["duration_ms"] = int((perf_counter() - started) * 1000)
            entry["fixture"] = fixture
            return entry

        processed_entries = await asyncio.gather(
            *(process_candidate(candidate) for candidate in pending_candidates),
            return_exceptions=False,
        )

        for entry in processed_entries:
            fixture_obj = entry.pop("fixture", None)
            if fixture_obj is None:
                crawl_entries.append(entry)
                continue
            assert isinstance(fixture_obj, Fixture)
            fixture_path = self._fixtures_dir / f"{fixture_obj.id}.json"
            fixture_path.write_text(fixture_obj.model_dump_json(indent=2), encoding="utf-8")
            written_paths.append(fixture_path)
            existing_keys.add((fixture_obj.source.repo_full_name, fixture_obj.source.pr_number))
            entry["fixture_written"] = True
            crawl_entries.append(entry)

        self._write_manifest(self._iter_fixture_files())
        self._write_review_checklist(self._iter_fixture_files())
        self._write_crawl_log(
            started_at=started_at,
            requested_repos=requested_repos,
            candidates_found=len(candidates),
            candidates_processed=len(pending_candidates),
            fixtures_written=len(written_paths),
            entries=crawl_entries,
        )
        return written_paths

    async def _create_fixture(
        self,
        candidate: PullRequestCandidate,
        *,
        suite: str,
    ) -> tuple[Fixture, dict[str, int]]:
        pr_detail = await self._github_client.get_pull_request(
            candidate.repo_full_name, candidate.pr_number
        )
        diff_text = await self._github_client.get_pull_request_diff(
            candidate.repo_full_name, candidate.pr_number
        )
        files_payload = await self._github_client.get_pull_request_files(
            candidate.repo_full_name, candidate.pr_number
        )

        ref = candidate.merge_commit_sha or candidate.head_sha or candidate.base_sha
        file_contents: dict[str, str] = {}
        for file_item in files_payload:
            path = str(file_item.get("filename", "") or "")
            if not path:
                continue
            file_contents[path] = await self._github_client.get_file_content(
                candidate.repo_full_name, path, ref
            )

        fixture_input = build_fixture_input(diff_text, file_contents)
        annotation, annotation_diagnostics = await self._annotator.annotate_with_diagnostics(
            repo_full_name=candidate.repo_full_name,
            pr_number=candidate.pr_number,
            pr_title=candidate.title,
            pr_body=str(pr_detail.get("body", "") or ""),
            diff_text=diff_text,
        )
        fixture_source = build_fixture_source(
            repo_full_name=candidate.repo_full_name,
            pr_number=candidate.pr_number,
            url=candidate.html_url,
            merge_commit_sha=candidate.merge_commit_sha,
            title=candidate.title,
        )

        parsed_files = parse_unified_diff(diff_text)
        fixture_id = f"{suite}_{candidate.repo_full_name.replace('/', '_')}_pr{candidate.pr_number}"
        fixture = Fixture(
            id=fixture_id.lower(),
            type="review",
            source=fixture_source,
            input=fixture_input,
            expected=annotation,
            metadata={
                "suite": suite,
                "tags": ["github", "auto_discover", "llm_assisted"],
                "annotated_by": "llm_draft",
                "reviewed": False,
                "difficulty": self._estimate_difficulty(parsed_files),
            },
        )
        return fixture, annotation_diagnostics

    def _write_crawl_log(
        self,
        *,
        started_at: datetime,
        requested_repos: int,
        candidates_found: int,
        candidates_processed: int,
        fixtures_written: int,
        entries: list[dict[str, object]],
    ) -> None:
        finished_at = datetime.now(UTC)
        payload = {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "repos_searched": requested_repos,
            "candidates_found": candidates_found,
            "candidates_processed": candidates_processed,
            "fixtures_written": fixtures_written,
            "entries": entries,
        }
        timestamp = finished_at.strftime("%Y%m%d_%H%M%S")
        output_path = self._outputs_dir / f"{timestamp}_crawl_log.json"
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _estimate_difficulty(diff_files: list) -> str:
        file_count = len(diff_files)
        hunk_count = sum(len(item.hunks) for item in diff_files)
        score = file_count + hunk_count
        if score <= 3:
            return "easy"
        if score <= 8:
            return "medium"
        return "hard"

    def _iter_fixture_files(self) -> Iterable[Path]:
        return sorted(self._fixtures_dir.glob("*.json"))

    def _load_existing_keys(self) -> set[tuple[str, int]]:
        keys: set[tuple[str, int]] = set()
        for path in self._iter_fixture_files():
            try:
                fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            keys.add((fixture.source.repo_full_name, fixture.source.pr_number))
        return keys

    def _write_manifest(self, fixture_paths: Iterable[Path]) -> None:
        entries: list[FixtureManifestEntry] = []
        for path in fixture_paths:
            try:
                fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            entries.append(
                FixtureManifestEntry(
                    fixture_id=fixture.id,
                    suite=fixture.metadata.suite,
                    fixture_type=fixture.type,
                    repo_full_name=fixture.source.repo_full_name,
                    pr_number=fixture.source.pr_number,
                    path=str(path.as_posix()),
                    reviewed=fixture.metadata.reviewed,
                )
            )
        manifest = FixtureManifest(entries=entries)
        manifest_path = self._fixtures_dir / "manifest.json"
        manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    def _write_review_checklist(self, fixture_paths: Iterable[Path]) -> None:
        lines = [
            "# Golden Fixture Review Checklist",
            "",
            "| fixture_id | source | reviewed | note |",
            "|---|---|---|---|",
        ]
        for path in fixture_paths:
            if path.name == "manifest.json":
                continue
            try:
                fixture = Fixture.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            source = f"{fixture.source.repo_full_name}#{fixture.source.pr_number}"
            lines.append(f"| {fixture.id} | {source} | {fixture.metadata.reviewed} |  |")
        checklist_path = self._fixtures_dir / "review_checklist.md"
        checklist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

