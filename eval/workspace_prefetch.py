"""Targeted workspace-cache prefetch and offline materialization checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from eval.runner import (
    _apply_fixture_diff,
    _checkout_git_workspace,
    _ensure_git_workspace_cache,
    _validate_diff_added_lines_against_workspace,
    _validate_expected_locations_against_diff,
)
from eval.schemas import Fixture, FixtureManifest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "eval" / "fixtures" / "manifest.json"
DEFAULT_CACHE_DIR = ROOT / "eval" / "outputs" / "workspace_cache"


def _fixture_paths(manifest_path: Path, explicit: list[Path]) -> list[Path]:
    if explicit:
        return [path.resolve() for path in explicit]
    manifest = FixtureManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    paths: list[Path] = []
    for entry in manifest.entries:
        path = Path(entry.path)
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        paths.append(path)
    return paths


def _cache_size_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _snapshot_identity(fixture: Fixture) -> str:
    workspace = fixture.input.workspace
    if workspace is None:
        return ""
    overlay = fixture.input.diff_text if workspace.apply_fixture_diff else ""
    return f"{workspace.checkout_sha}+{hashlib.sha256(overlay.encode('utf-8')).hexdigest()}"


def prefetch_fixtures(
    fixture_paths: list[Path],
    *,
    cache_dir: Path,
) -> dict[str, Any]:
    """Prefetch selected snapshots and prove each can restore fully offline."""
    records: list[dict[str, Any]] = []
    for fixture_path in fixture_paths:
        fixture_id = fixture_path.stem
        try:
            fixture = Fixture.model_validate_json(
                fixture_path.read_text(encoding="utf-8")
            )
            fixture_id = fixture.id
            tags = {tag.lower().replace("_", "-") for tag in fixture.metadata.tags}
            if fixture.metadata.suite.lower().replace("_", "-") == "held-out" or (
                "held-out" in tags
            ):
                raise ValueError(f"Held-out fixture is forbidden: {fixture.id}")
            workspace = fixture.input.workspace
            if workspace is None:
                raise ValueError(f"Fixture {fixture.id} has no git workspace")
            cache_root = _ensure_git_workspace_cache(
                workspace,
                cache_dir,
                pr_number=fixture.source.pr_number,
                offline=False,
            )
            with tempfile.TemporaryDirectory(prefix="eval-offline-restore-") as tmp:
                repo_root = _checkout_git_workspace(
                    workspace,
                    Path(tmp) / "repo",
                    pr_number=fixture.source.pr_number,
                    workspace_cache_dir=cache_dir,
                    offline=True,
                )
                if workspace.apply_fixture_diff:
                    _apply_fixture_diff(fixture, repo_root)
                errors = _validate_diff_added_lines_against_workspace(
                    fixture, repo_root
                ) + _validate_expected_locations_against_diff(fixture, repo_root)
                if errors:
                    raise ValueError("; ".join(errors))
            records.append(
                {
                    "fixture_id": fixture.id,
                    "repo_url": workspace.repo_url,
                    "checkout_sha": workspace.checkout_sha,
                    "repository_snapshot": _snapshot_identity(fixture),
                    "cache_path": str(cache_root.resolve()),
                    "cache_size_bytes": _cache_size_bytes(cache_root),
                    "offline_checkout_verified": True,
                    "success": True,
                }
            )
        except Exception as exc:  # noqa: BLE001
            records.append(
                {
                    "fixture_id": fixture_id,
                    "success": False,
                    "error": str(exc),
                }
            )
    return {
        "success": all(record["success"] for record in records),
        "fixture_count": len(records),
        "success_count": sum(bool(record["success"]) for record in records),
        "failure_count": sum(not bool(record["success"]) for record in records),
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", nargs="*", type=Path, default=[])
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    fixture_paths = _fixture_paths(args.manifest, args.fixtures)
    result = prefetch_fixtures(fixture_paths, cache_dir=args.cache_dir.resolve())
    payload = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    if not result["success"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
