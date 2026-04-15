"""Golden fixture generation pipeline."""

from __future__ import annotations

import json
from pathlib import Path
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
        *,
        github_client: GithubCrawlerClient | None = None,
        annotator: LLMAnnotator | None = None,
        min_expected_issues: int = 0,
    ) -> None:
        self._fixtures_dir = Path(fixtures_dir)
        self._fixtures_dir.mkdir(parents=True, exist_ok=True)
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
    ) -> list[Path]:
        """Discover PRs and create fixture files."""
        existing_keys = self._load_existing_keys()
        written_paths: list[Path] = []

        candidates = await self._github_client.discover_pull_request_candidates(
            max_repos=max_repos,
            max_prs_per_repo=max_prs_per_repo,
        )

        for candidate in candidates:
            key = (candidate.repo_full_name, candidate.pr_number)
            if key in existing_keys:
                continue
            fixture = await self._create_fixture(candidate, suite=suite)
            expected_count = len(fixture.expected.issues)
            if fixture.expected.is_empty_annotation:
                print(
                    "Skipping fixture "
                    f"{fixture.id}: expected_issues={expected_count}, reason=empty_annotation"
                )
                continue
            if expected_count < self._min_expected_issues:
                reason = "empty_annotation" if fixture.expected.is_empty_annotation else "below_threshold"
                print(
                    "Skipping fixture "
                    f"{fixture.id}: expected_issues={expected_count}, "
                    f"min_required={self._min_expected_issues}, reason={reason}"
                )
                continue
            fixture_path = self._fixtures_dir / f"{fixture.id}.json"
            fixture_path.write_text(fixture.model_dump_json(indent=2), encoding="utf-8")
            written_paths.append(fixture_path)
            existing_keys.add(key)

        self._write_manifest(self._iter_fixture_files())
        self._write_review_checklist(self._iter_fixture_files())
        return written_paths

    async def _create_fixture(self, candidate: PullRequestCandidate, *, suite: str) -> Fixture:
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
        annotation = await self._annotator.annotate(
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
        return Fixture(
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

