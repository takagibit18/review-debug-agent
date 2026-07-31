"""SQLite persistence and safe incremental updates for code relation graphs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field

from src.analyzer.code_graph import (
    CodeRelationGraph,
    EdgeKind,
    StaticRelationGraphBuilder,
)

INDEX_SCHEMA_VERSION = 3
INDEX_BUILD_VERSION = "v025.2"


class IndexSnapshot(BaseModel):
    repository_id: str
    revision: str = ""
    file_hashes: dict[str, str] = Field(default_factory=dict)
    graph: CodeRelationGraph = Field(default_factory=CodeRelationGraph)
    schema_version: int = INDEX_SCHEMA_VERSION
    build_version: str = INDEX_BUILD_VERSION
    created_at: str = ""
    updated_at: str = ""


class IndexBuildResult(BaseModel):
    graph: CodeRelationGraph
    repository_id: str
    revision: str
    status: str
    cache_hit: bool = False
    cache_hit_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    file_count: int = Field(default=0, ge=0)
    changed_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    affected_files: list[str] = Field(default_factory=list)
    parsed_file_count: int = Field(default=0, ge=0)
    build_latency_seconds: float = Field(default=0.0, ge=0.0)
    incremental_update_latency_seconds: float = Field(default=0.0, ge=0.0)
    persistence_enabled: bool = True
    fallback: str = ""
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)


class PersistentIndexStore:
    """Versioned local graph store; corrupted caches are preserved then rebuilt."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.last_recovery = ""
        self._initialize_with_recovery()

    def _initialize_with_recovery(self) -> None:
        try:
            self._initialize()
        except _SchemaMismatch as exc:
            self._preserve_invalid_cache("incompatible")
            self.last_recovery = f"schema_rebuild:{exc}"
            self._initialize()
        except sqlite3.DatabaseError as exc:
            self._preserve_invalid_cache("corrupt")
            self.last_recovery = f"corruption_rebuild:{exc.__class__.__name__}"
            self._initialize()

    def _initialize(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            raw_version = connection.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if raw_version is not None and int(raw_version[0]) != INDEX_SCHEMA_VERSION:
                raise _SchemaMismatch(str(raw_version[0]))
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS repositories (
                    repository_id TEXT PRIMARY KEY,
                    root_path TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    build_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    graph_metadata TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    repository_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    language TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (repository_id, path)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    repository_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (repository_id, node_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS edges (
                    repository_id TEXT NOT NULL,
                    edge_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (repository_id, edge_id)
                )
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(INDEX_SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('build_version', ?)",
                (INDEX_BUILD_VERSION,),
            )
            connection.commit()
        finally:
            connection.close()

    def ensure_compatible(self) -> None:
        try:
            with _sqlite_connection(self.path) as connection:
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
            if row is None or int(row[0]) != INDEX_SCHEMA_VERSION:
                raise _SchemaMismatch(str(row[0]) if row else "missing")
        except (_SchemaMismatch, sqlite3.DatabaseError) as exc:
            self._preserve_invalid_cache("incompatible")
            self.last_recovery = f"schema_rebuild:{exc}"
            self._initialize()

    def load(self, repository_id: str) -> IndexSnapshot | None:
        self.ensure_compatible()
        try:
            with _sqlite_connection(self.path) as connection:
                repository = connection.execute(
                    "SELECT revision, build_version, created_at, updated_at, graph_metadata "
                    "FROM repositories WHERE repository_id = ?",
                    (repository_id,),
                ).fetchone()
                if repository is None:
                    return None
                file_rows = connection.execute(
                    "SELECT path, file_hash FROM files WHERE repository_id = ?",
                    (repository_id,),
                ).fetchall()
                node_rows = connection.execute(
                    "SELECT payload FROM nodes WHERE repository_id = ?",
                    (repository_id,),
                ).fetchall()
                edge_rows = connection.execute(
                    "SELECT payload FROM edges WHERE repository_id = ?",
                    (repository_id,),
                ).fetchall()
            graph = CodeRelationGraph(
                nodes={
                    payload["node_id"]: payload
                    for row in node_rows
                    for payload in [json.loads(row[0])]
                },
                edges=[json.loads(row[0]) for row in edge_rows],
                metadata=json.loads(repository[4] or "{}"),
            )
            return IndexSnapshot(
                repository_id=repository_id,
                revision=repository[0],
                file_hashes={row[0]: row[1] for row in file_rows},
                graph=graph,
                build_version=repository[1],
                created_at=repository[2],
                updated_at=repository[3],
            )
        except (sqlite3.DatabaseError, ValueError, json.JSONDecodeError) as exc:
            self._preserve_invalid_cache("corrupt")
            self.last_recovery = f"load_rebuild:{exc.__class__.__name__}"
            self._initialize()
            return None

    def save(
        self,
        *,
        repository_id: str,
        root_path: str,
        revision: str,
        file_hashes: dict[str, str],
        graph: CodeRelationGraph,
        updated_paths: set[str] | None = None,
        deleted_paths: set[str] | None = None,
    ) -> None:
        """Persist a full snapshot or replace only affected path partitions."""

        self.ensure_compatible()
        now = datetime.now(UTC).isoformat()
        with _sqlite_connection(self.path) as connection:
            previous = connection.execute(
                "SELECT created_at FROM repositories WHERE repository_id = ?",
                (repository_id,),
            ).fetchone()
            created = previous[0] if previous else now
            connection.execute(
                """
                INSERT OR REPLACE INTO repositories(
                    repository_id, root_path, revision, build_version,
                    created_at, updated_at, graph_metadata
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_id,
                    root_path,
                    revision,
                    INDEX_BUILD_VERSION,
                    created,
                    now,
                    json.dumps(graph.metadata, ensure_ascii=True, default=str),
                ),
            )
            full_replace = updated_paths is None
            normalized_updated = {
                str(path).replace("\\", "/") for path in (updated_paths or set())
            }
            normalized_deleted = {
                str(path).replace("\\", "/") for path in (deleted_paths or set())
            }
            scoped_paths = normalized_updated | normalized_deleted
            if full_replace:
                connection.execute(
                    "DELETE FROM files WHERE repository_id = ?", (repository_id,)
                )
                connection.execute(
                    "DELETE FROM nodes WHERE repository_id = ?", (repository_id,)
                )
                connection.execute(
                    "DELETE FROM edges WHERE repository_id = ?", (repository_id,)
                )
                selected_hashes = file_hashes
                selected_nodes = list(graph.nodes.values())
                selected_edges = list(graph.edges)
            else:
                stale_node_ids = [
                    row[0]
                    for path in sorted(scoped_paths)
                    for row in connection.execute(
                        "SELECT node_id FROM nodes WHERE repository_id = ? AND path = ?",
                        (repository_id, path),
                    ).fetchall()
                ]
                connection.executemany(
                    "DELETE FROM files WHERE repository_id = ? AND path = ?",
                    [(repository_id, path) for path in sorted(scoped_paths)],
                )
                connection.executemany(
                    "DELETE FROM edges WHERE repository_id = ? AND path = ?",
                    [(repository_id, path) for path in sorted(scoped_paths)],
                )
                connection.executemany(
                    "DELETE FROM edges WHERE repository_id = ? "
                    "AND (source_id = ? OR target_id = ?)",
                    [(repository_id, node_id, node_id) for node_id in stale_node_ids],
                )
                connection.executemany(
                    "DELETE FROM nodes WHERE repository_id = ? AND path = ?",
                    [(repository_id, path) for path in sorted(scoped_paths)],
                )
                selected_hashes = {
                    path: digest
                    for path, digest in file_hashes.items()
                    if path in normalized_updated
                }
                selected_nodes = [
                    node
                    for node in graph.nodes.values()
                    if node.path in normalized_updated
                ]
                node_paths = {node.node_id: node.path for node in graph.nodes.values()}
                selected_edges = [
                    edge
                    for edge in graph.edges
                    if edge.path in normalized_updated
                    or node_paths.get(edge.source, "") in normalized_updated
                    or node_paths.get(edge.target, "") in normalized_updated
                ]
            connection.executemany(
                "INSERT OR REPLACE INTO files(repository_id, path, file_hash, language, revision, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (
                        repository_id,
                        path,
                        digest,
                        _language_for_path(path),
                        revision,
                        now,
                    )
                    for path, digest in sorted(selected_hashes.items())
                ],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO nodes(repository_id, node_id, path, payload) "
                "VALUES(?, ?, ?, ?)",
                [
                    (
                        repository_id,
                        node.node_id,
                        node.path,
                        node.model_dump_json(),
                    )
                    for node in selected_nodes
                ],
            )
            connection.executemany(
                "INSERT OR REPLACE INTO edges(repository_id, edge_id, source_id, target_id, path, payload) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (
                        repository_id,
                        edge.edge_id,
                        edge.source,
                        edge.target,
                        edge.path,
                        edge.model_dump_json(),
                    )
                    for edge in selected_edges
                ],
            )
            connection.commit()

    def _preserve_invalid_cache(self, reason: str) -> None:
        if not self.path.exists():
            return
        timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        backup = self.path.with_name(f"{self.path.name}.{reason}-{timestamp}")
        self.path.replace(backup)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.path) + suffix)
            if sidecar.exists():
                sidecar.replace(Path(str(backup) + suffix))


class _SchemaMismatch(sqlite3.DatabaseError):
    pass


class RelationGraphIndex:
    """Build, reuse, or incrementally update a persistent relation graph."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        persistence_enabled: bool = True,
        index_path: Path | str | None = None,
        resolver_mode: str = "ast",
        language_resolver: Any | None = None,
        max_files: int = 5_000,
        max_ambiguous_targets: int = 4,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.persistence_enabled = persistence_enabled
        self.index_path = (
            Path(index_path)
            if index_path
            else self.repo_root / ".mergewarden" / "relation-index.sqlite3"
        )
        self.builder = StaticRelationGraphBuilder(
            self.repo_root,
            resolver_mode=resolver_mode,
            language_resolver=language_resolver,
            max_files=max_files,
            max_ambiguous_targets=max_ambiguous_targets,
        )

    def build(self) -> IndexBuildResult:
        started = perf_counter()
        repository_id = repository_identity(self.repo_root)
        revision = revision_identity(self.repo_root)
        files = self.builder.discover_files()
        hashes = file_hashes(self.repo_root, files)
        if not self.persistence_enabled:
            graph = self.builder.build(files=files)
            return IndexBuildResult(
                graph=graph,
                repository_id=repository_id,
                revision=revision,
                status="build",
                file_count=len(files),
                changed_files=sorted(hashes),
                affected_files=sorted(hashes),
                parsed_file_count=len(files),
                build_latency_seconds=perf_counter() - started,
                persistence_enabled=False,
            )

        try:
            store = PersistentIndexStore(self.index_path)
            snapshot = store.load(repository_id)
        except (OSError, sqlite3.DatabaseError) as exc:
            graph = self.builder.build(files=files)
            return IndexBuildResult(
                graph=graph,
                repository_id=repository_id,
                revision=revision,
                status="fallback_build",
                file_count=len(files),
                changed_files=sorted(hashes),
                affected_files=sorted(hashes),
                parsed_file_count=len(files),
                build_latency_seconds=perf_counter() - started,
                persistence_enabled=False,
                fallback=f"index_unavailable:{exc.__class__.__name__}",
            )

        diagnostics: list[dict[str, Any]] = []
        if store.last_recovery:
            diagnostics.append(
                {"stage": "persistent_index", "fallback": store.last_recovery}
            )
        rebuild_reason = ""
        if snapshot is not None and snapshot.build_version != INDEX_BUILD_VERSION:
            rebuild_reason = (
                "build_version_changed:"
                f"{snapshot.build_version or 'missing'}->{INDEX_BUILD_VERSION}"
            )
        elif (
            snapshot is not None
            and snapshot.graph.metadata.get("build_profile")
            != self.builder.build_profile()
        ):
            rebuild_reason = "build_profile_changed"
        if rebuild_reason:
            diagnostics.append(
                {
                    "stage": "persistent_index",
                    "fallback": "repository_graph_rebuild",
                    "reason": rebuild_reason,
                }
            )
        if snapshot is None or rebuild_reason:
            graph = self.builder.build(files=files)
            store.save(
                repository_id=repository_id,
                root_path=str(self.repo_root),
                revision=revision,
                file_hashes=hashes,
                graph=graph,
            )
            return IndexBuildResult(
                graph=graph,
                repository_id=repository_id,
                revision=revision,
                status=(
                    "rebuild" if store.last_recovery or rebuild_reason else "build"
                ),
                file_count=len(files),
                changed_files=sorted(hashes),
                affected_files=sorted(hashes),
                parsed_file_count=len(files),
                build_latency_seconds=perf_counter() - started,
                persistence_enabled=True,
                fallback=rebuild_reason or store.last_recovery,
                diagnostics=diagnostics,
            )

        changed = {
            path
            for path, digest in hashes.items()
            if snapshot.file_hashes.get(path) != digest
        }
        deleted = set(snapshot.file_hashes) - set(hashes)
        unchanged = set(hashes) - changed
        if not changed and not deleted:
            snapshot.graph.metadata["cache_hit"] = True
            return IndexBuildResult(
                graph=snapshot.graph,
                repository_id=repository_id,
                revision=revision,
                status="reuse",
                cache_hit=True,
                cache_hit_rate=1.0,
                file_count=len(files),
                parsed_file_count=0,
                build_latency_seconds=perf_counter() - started,
                persistence_enabled=True,
                diagnostics=diagnostics,
            )

        update_started = perf_counter()
        affected = _affected_paths(snapshot.graph, changed | deleted)
        affected.update(changed)
        existing_by_rel = {
            path.resolve().relative_to(self.repo_root).as_posix(): path
            for path in files
        }
        parse_paths = [
            existing_by_rel[path]
            for path in sorted(affected)
            if path in existing_by_rel
        ]
        graph = snapshot.graph.model_copy(deep=True)
        graph.remove_files(deleted)
        try:
            graph = self.builder.build(files=parse_paths, base_graph=graph)
            status = "incremental_update"
            fallback = ""
        except Exception as exc:  # noqa: BLE001
            graph = self.builder.build(files=files)
            status = "rebuild"
            fallback = f"incremental_failed:{exc.__class__.__name__}"
            diagnostics.append(
                {
                    "stage": "incremental_index",
                    "fallback": "full_rebuild",
                    "error": str(exc)[:300],
                }
            )
            parse_paths = files
            affected = set(hashes)
        store.save(
            repository_id=repository_id,
            root_path=str(self.repo_root),
            revision=revision,
            file_hashes=hashes,
            graph=graph,
            updated_paths=None if status == "rebuild" else set(affected),
            deleted_paths=deleted,
        )
        return IndexBuildResult(
            graph=graph,
            repository_id=repository_id,
            revision=revision,
            status=status,
            cache_hit=False,
            cache_hit_rate=len(unchanged) / len(hashes) if hashes else 0.0,
            file_count=len(files),
            changed_files=sorted(changed),
            deleted_files=sorted(deleted),
            affected_files=sorted(affected),
            parsed_file_count=len(parse_paths),
            build_latency_seconds=perf_counter() - started,
            incremental_update_latency_seconds=perf_counter() - update_started,
            persistence_enabled=True,
            fallback=fallback or store.last_recovery,
            diagnostics=diagnostics,
        )


def repository_identity(repo_root: Path) -> str:
    remote = _git_output(repo_root, ["config", "--get", "remote.origin.url"])
    remote = remote.strip()
    raw = f"remote:{remote}" if remote else f"path:{repo_root.resolve()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def revision_identity(repo_root: Path) -> str:
    revision = _git_output(repo_root, ["rev-parse", "HEAD"]).strip()
    return revision or "working-tree"


def file_hashes(repo_root: Path, files: Iterable[Path]) -> dict[str, str]:
    output: dict[str, str] = {}
    for path in files:
        try:
            rel = path.resolve().relative_to(repo_root).as_posix()
            output[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, ValueError):
            continue
    return output


def _affected_paths(graph: CodeRelationGraph, changed_or_deleted: set[str]) -> set[str]:
    affected = set(changed_or_deleted)
    for edge in graph.edges:
        source = graph.nodes.get(edge.source)
        target = graph.nodes.get(edge.target)
        source_path = source.path if source else edge.path
        target_path = target.path if target else ""
        if edge.kind == EdgeKind.IMPORTS and (
            source_path in changed_or_deleted or target_path in changed_or_deleted
        ):
            affected.update(path for path in (source_path, target_path) if path)
        if target_path in changed_or_deleted and source_path:
            affected.add(source_path)
        if source_path in changed_or_deleted and target_path:
            affected.add(target_path)
    return affected


def _git_output(repo_root: Path, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def _language_for_path(path: str) -> str:
    return {".py": "python", ".rs": "rust", ".cs": "csharp"}.get(
        Path(path).suffix.lower(), "unknown"
    )


@contextmanager
def _sqlite_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path)
    try:
        yield connection
    finally:
        connection.close()
