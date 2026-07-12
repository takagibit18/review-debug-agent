"""SQLite database setup for the platform MVP."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS installations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    github_installation_id INTEGER NOT NULL UNIQUE,
    account_login TEXT NOT NULL DEFAULT '',
    account_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    installation_id INTEGER NOT NULL REFERENCES installations(id) ON DELETE CASCADE,
    full_name TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    default_branch TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(installation_id, full_name)
);

CREATE TABLE IF NOT EXISTS review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    installation_id INTEGER NOT NULL REFERENCES installations(id) ON DELETE RESTRICT,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE RESTRICT,
    repo_full_name TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    head_sha TEXT NOT NULL,
    base_sha TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    trigger_event TEXT NOT NULL DEFAULT '',
    trigger_action TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    review_response_path TEXT NOT NULL DEFAULT '',
    run_summary_path TEXT NOT NULL DEFAULT '',
    publish_result_path TEXT NOT NULL DEFAULT '',
    total_tokens INTEGER,
    publish_status TEXT NOT NULL DEFAULT '',
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    resume_from_step TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_review_runs_status_created
ON review_runs(status, created_at, id);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL UNIQUE,
    installation_id INTEGER REFERENCES installations(id) ON DELETE SET NULL,
    event TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    repo_full_name TEXT NOT NULL DEFAULT '',
    pr_number INTEGER,
    head_sha TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK(status IN ('received', 'duplicate', 'queued', 'ignored', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status_received
ON webhook_deliveries(status, received_at, id);

CREATE TABLE IF NOT EXISTS run_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    input_digest TEXT NOT NULL DEFAULT '',
    output_artifact_path TEXT NOT NULL DEFAULT '',
    error_type TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(run_id, step_id, attempt),
    CHECK(status IN ('pending', 'running', 'completed', 'failed', 'skipped'))
);

CREATE INDEX IF NOT EXISTS idx_run_checkpoints_run_step
ON run_checkpoints(run_id, step_id, attempt);

CREATE TABLE IF NOT EXISTS tenant_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    installation_id INTEGER NOT NULL REFERENCES installations(id) ON DELETE CASCADE,
    repository_id INTEGER REFERENCES repositories(id) ON DELETE CASCADE,
    review_enabled INTEGER NOT NULL DEFAULT 1,
    review_draft_prs INTEGER NOT NULL DEFAULT 0,
    publish_comments INTEGER NOT NULL DEFAULT 1,
    model_name TEXT,
    token_budget INTEGER,
    prompt_input_token_budget INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_configs_repo
ON tenant_configs(installation_id, repository_id)
WHERE repository_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_configs_installation
ON tenant_configs(installation_id)
WHERE repository_id IS NULL;

CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES review_runs(run_id) ON DELETE CASCADE,
    installation_id INTEGER NOT NULL REFERENCES installations(id) ON DELETE RESTRICT,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE RESTRICT,
    model_name TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

POST_SCHEMA_SQL = """
DROP INDEX IF EXISTS idx_review_runs_active_head;

CREATE UNIQUE INDEX IF NOT EXISTS idx_review_runs_active_head_tenant
ON review_runs(installation_id, repo_full_name, pr_number, head_sha)
WHERE status IN ('queued', 'running', 'succeeded');

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_installation_received
ON webhook_deliveries(installation_id, received_at, id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_records_run_attempt
ON usage_records(run_id, attempt)
WHERE attempt > 0;
"""


def connect(database_url: str) -> sqlite3.Connection:
    """Open a SQLite connection from a sqlite:/// URL."""
    path = sqlite_path(database_url)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all platform tables and indexes if they do not exist."""
    conn.executescript(SCHEMA_SQL)
    _ensure_column(
        conn,
        "webhook_deliveries",
        "installation_id",
        "INTEGER REFERENCES installations(id) ON DELETE SET NULL",
    )
    _ensure_column(conn, "review_runs", "lease_owner", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "review_runs", "lease_expires_at", "TEXT")
    _ensure_column(conn, "review_runs", "heartbeat_at", "TEXT")
    _ensure_column(conn, "review_runs", "resume_from_step", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "review_runs", "attempt", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "usage_records", "attempt", "INTEGER NOT NULL DEFAULT 0")
    conn.executescript(POST_SCHEMA_SQL)
    conn.commit()


def sqlite_path(database_url: str) -> str:
    """Return the filesystem path for the configured SQLite URL."""
    value = (database_url or "").strip()
    if value in {":memory:", "sqlite:///:memory:"}:
        return ":memory:"
    if value.startswith("sqlite:///"):
        return value.removeprefix("sqlite:///")
    if value.startswith("sqlite://"):
        return value.removeprefix("sqlite://")
    if value:
        return value
    return ".mergewarden/platform.db"


def _ensure_column(
    conn: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_sql: str,
) -> None:
    existing = {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name not in existing:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
