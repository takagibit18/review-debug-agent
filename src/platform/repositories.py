"""Repository layer for platform persistence."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

from src.platform.models import (
    InstallationRecord,
    RepositoryRecord,
    ReviewRunRecord,
    TenantConfigRecord,
    UsageRecord,
    WebhookDeliveryRecord,
)

ACTIVE_RUN_STATUSES = ("queued", "running", "succeeded")


class PlatformRepository:
    """Small SQLite repository wrapper with explicit persistence methods."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert_installation(
        self,
        *,
        github_installation_id: int,
        account_login: str,
        account_type: str,
        status: str = "active",
    ) -> InstallationRecord:
        existing = self._fetchone(
            "SELECT * FROM installations WHERE github_installation_id = ?",
            (github_installation_id,),
        )
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO installations (
                    github_installation_id, account_login, account_type, status
                )
                VALUES (?, ?, ?, ?)
                """,
                (github_installation_id, account_login, account_type, status),
            )
        else:
            self.conn.execute(
                """
                UPDATE installations
                SET account_login = ?, account_type = ?, status = ?, updated_at = datetime('now')
                WHERE github_installation_id = ?
                """,
                (account_login, account_type, status, github_installation_id),
            )
        self.conn.commit()
        row = self._fetchone(
            "SELECT * FROM installations WHERE github_installation_id = ?",
            (github_installation_id,),
        )
        assert row is not None
        return _installation(row)

    def list_installations(self) -> list[InstallationRecord]:
        return [
            _installation(row)
            for row in self.conn.execute(
                "SELECT * FROM installations ORDER BY id"
            ).fetchall()
        ]

    def get_installation(self, installation_id: int) -> InstallationRecord | None:
        row = self._fetchone(
            "SELECT * FROM installations WHERE id = ?",
            (installation_id,),
        )
        return _installation(row) if row is not None else None

    def upsert_repository(
        self,
        *,
        installation_id: int,
        full_name: str,
        owner: str,
        name: str,
        default_branch: str,
        enabled: bool = True,
    ) -> RepositoryRecord:
        existing = self._fetchone(
            "SELECT * FROM repositories WHERE installation_id = ? AND full_name = ?",
            (installation_id, full_name),
        )
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO repositories (
                    installation_id, full_name, owner, name, default_branch, enabled
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (installation_id, full_name, owner, name, default_branch, int(enabled)),
            )
        else:
            self.conn.execute(
                """
                UPDATE repositories
                SET owner = ?, name = ?, default_branch = ?, enabled = ?, updated_at = datetime('now')
                WHERE installation_id = ? AND full_name = ?
                """,
                (owner, name, default_branch, int(enabled), installation_id, full_name),
            )
        self.conn.commit()
        row = self._fetchone(
            "SELECT * FROM repositories WHERE installation_id = ? AND full_name = ?",
            (installation_id, full_name),
        )
        assert row is not None
        return _repository(row)

    def list_repositories(
        self,
        *,
        installation_id: int | None = None,
    ) -> list[RepositoryRecord]:
        if installation_id is None:
            rows = self.conn.execute("SELECT * FROM repositories ORDER BY id").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM repositories WHERE installation_id = ? ORDER BY id",
                (installation_id,),
            ).fetchall()
        return [_repository(row) for row in rows]

    def insert_webhook_delivery(
        self,
        *,
        delivery_id: str,
        event: str,
        action: str,
        repo_full_name: str,
        pr_number: int | None,
        head_sha: str,
    ) -> tuple[WebhookDeliveryRecord, bool]:
        try:
            self.conn.execute(
                """
                INSERT INTO webhook_deliveries (
                    delivery_id, event, action, repo_full_name, pr_number, head_sha, status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'received')
                """,
                (delivery_id, event, action, repo_full_name, pr_number, head_sha),
            )
            self.conn.commit()
            row = self._delivery_by_id(delivery_id)
            assert row is not None
            return row, False
        except sqlite3.IntegrityError:
            self.conn.rollback()
            self.update_delivery_status(
                delivery_id,
                status="duplicate",
                reason="duplicate_delivery",
            )
            row = self._delivery_by_id(delivery_id)
            assert row is not None
            return row, True

    def update_delivery_status(
        self,
        delivery_id: str,
        *,
        status: str,
        reason: str = "",
        run_id: str = "",
    ) -> WebhookDeliveryRecord:
        self.conn.execute(
            """
            UPDATE webhook_deliveries
            SET status = ?, reason = ?, run_id = COALESCE(NULLIF(?, ''), run_id)
            WHERE delivery_id = ?
            """,
            (status, reason, run_id, delivery_id),
        )
        self.conn.commit()
        row = self._delivery_by_id(delivery_id)
        assert row is not None
        return row

    def list_deliveries(
        self,
        *,
        status: str | None = None,
    ) -> list[WebhookDeliveryRecord]:
        if status:
            rows = self.conn.execute(
                """
                SELECT * FROM webhook_deliveries
                WHERE status = ?
                ORDER BY received_at DESC, id DESC
                """,
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM webhook_deliveries ORDER BY received_at DESC, id DESC"
            ).fetchall()
        return [_delivery(row) for row in rows]

    def create_review_run(
        self,
        *,
        installation_id: int,
        repository_id: int,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
        base_sha: str,
        trigger_event: str,
        trigger_action: str,
        run_id: str | None = None,
    ) -> ReviewRunRecord:
        new_run_id = run_id or f"run-{uuid4().hex}"
        self.conn.execute(
            """
            INSERT INTO review_runs (
                run_id, installation_id, repository_id, repo_full_name, pr_number,
                head_sha, base_sha, status, trigger_event, trigger_action
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                new_run_id,
                installation_id,
                repository_id,
                repo_full_name,
                pr_number,
                head_sha,
                base_sha,
                trigger_event,
                trigger_action,
            ),
        )
        self.conn.commit()
        row = self.get_run(new_run_id)
        assert row is not None
        return row

    def find_active_run(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        head_sha: str,
    ) -> ReviewRunRecord | None:
        placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
        row = self._fetchone(
            f"""
            SELECT * FROM review_runs
            WHERE repo_full_name = ? AND pr_number = ? AND head_sha = ?
              AND status IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            (repo_full_name, pr_number, head_sha, *ACTIVE_RUN_STATUSES),
        )
        return _run(row) if row is not None else None

    def claim_next_queued_run(self) -> ReviewRunRecord | None:
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self._fetchone(
                """
                SELECT * FROM review_runs
                WHERE status = 'queued'
                ORDER BY created_at, id
                LIMIT 1
                """,
                (),
            )
            if row is None:
                self.conn.commit()
                return None
            run_id = str(row["run_id"])
            self.conn.execute(
                """
                UPDATE review_runs
                SET status = 'running', started_at = datetime('now'), updated_at = datetime('now')
                WHERE run_id = ? AND status = 'queued'
                """,
                (run_id,),
            )
            self.conn.commit()
            return self.get_run(run_id)
        except Exception:
            self.conn.rollback()
            raise

    def mark_run_succeeded(
        self,
        run_id: str,
        *,
        review_response_path: str = "",
        run_summary_path: str = "",
        publish_result_path: str = "",
        total_tokens: int | None = None,
        publish_status: str = "",
    ) -> ReviewRunRecord:
        self.conn.execute(
            """
            UPDATE review_runs
            SET status = 'succeeded',
                finished_at = datetime('now'),
                review_response_path = ?,
                run_summary_path = ?,
                publish_result_path = ?,
                total_tokens = ?,
                publish_status = ?,
                error_type = '',
                error_message = '',
                updated_at = datetime('now')
            WHERE run_id = ?
            """,
            (
                review_response_path,
                run_summary_path,
                publish_result_path,
                total_tokens,
                publish_status,
                run_id,
            ),
        )
        self.conn.commit()
        row = self.get_run(run_id)
        assert row is not None
        return row

    def mark_run_failed(
        self,
        run_id: str,
        *,
        error_type: str,
        error_message: str,
    ) -> ReviewRunRecord:
        self.conn.execute(
            """
            UPDATE review_runs
            SET status = 'failed',
                finished_at = datetime('now'),
                error_type = ?,
                error_message = ?,
                updated_at = datetime('now')
            WHERE run_id = ?
            """,
            (error_type, error_message, run_id),
        )
        self.conn.commit()
        row = self.get_run(run_id)
        assert row is not None
        return row

    def get_run(self, run_id: str) -> ReviewRunRecord | None:
        row = self._fetchone("SELECT * FROM review_runs WHERE run_id = ?", (run_id,))
        return _run(row) if row is not None else None

    def list_runs(
        self,
        *,
        repo_full_name: str | None = None,
        pr_number: int | None = None,
        status: str | None = None,
    ) -> list[ReviewRunRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if repo_full_name:
            clauses.append("repo_full_name = ?")
            params.append(repo_full_name)
        if pr_number is not None:
            clauses.append("pr_number = ?")
            params.append(pr_number)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM review_runs{where} ORDER BY created_at DESC, id DESC",
            tuple(params),
        ).fetchall()
        return [_run(row) for row in rows]

    def retry_run(self, run_id: str) -> ReviewRunRecord:
        original = self.get_run(run_id)
        if original is None:
            raise KeyError(run_id)
        if original.status not in {"failed", "cancelled", "skipped"}:
            raise ValueError("only failed, cancelled, or skipped runs can be retried")
        active = self.find_active_run(
            repo_full_name=original.repo_full_name,
            pr_number=original.pr_number,
            head_sha=original.head_sha,
        )
        if active is not None:
            raise ValueError("an active run already exists for this repo, pull request, and head sha")
        return self.create_review_run(
            installation_id=original.installation_id,
            repository_id=original.repository_id,
            repo_full_name=original.repo_full_name,
            pr_number=original.pr_number,
            head_sha=original.head_sha,
            base_sha=original.base_sha,
            trigger_event=original.trigger_event,
            trigger_action=original.trigger_action,
        )

    def upsert_tenant_config(
        self,
        *,
        installation_id: int,
        repository_id: int | None,
        review_enabled: bool,
        review_draft_prs: bool,
        publish_comments: bool,
        model_name: str | None = None,
        token_budget: int | None = None,
        prompt_input_token_budget: int | None = None,
    ) -> TenantConfigRecord:
        existing = self.get_tenant_config(
            installation_id=installation_id,
            repository_id=repository_id,
        )
        values = (
            int(review_enabled),
            int(review_draft_prs),
            int(publish_comments),
            model_name,
            token_budget,
            prompt_input_token_budget,
        )
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO tenant_configs (
                    installation_id, repository_id, review_enabled, review_draft_prs,
                    publish_comments, model_name, token_budget, prompt_input_token_budget
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    installation_id,
                    repository_id,
                    int(review_enabled),
                    int(review_draft_prs),
                    int(publish_comments),
                    model_name,
                    token_budget,
                    prompt_input_token_budget,
                ),
            )
        else:
            self.conn.execute(
                """
                UPDATE tenant_configs
                SET review_enabled = ?,
                    review_draft_prs = ?,
                    publish_comments = ?,
                    model_name = ?,
                    token_budget = ?,
                    prompt_input_token_budget = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (*values, existing.id),
            )
        self.conn.commit()
        record = self.get_tenant_config(
            installation_id=installation_id,
            repository_id=repository_id,
        )
        assert record is not None
        return record

    def get_tenant_config(
        self,
        *,
        installation_id: int,
        repository_id: int | None,
    ) -> TenantConfigRecord | None:
        if repository_id is None:
            row = self._fetchone(
                """
                SELECT * FROM tenant_configs
                WHERE installation_id = ? AND repository_id IS NULL
                """,
                (installation_id,),
            )
        else:
            row = self._fetchone(
                """
                SELECT * FROM tenant_configs
                WHERE installation_id = ? AND repository_id = ?
                """,
                (installation_id, repository_id),
            )
        return _tenant_config(row) if row is not None else None

    def create_usage_record(
        self,
        *,
        run_id: str,
        installation_id: int,
        repository_id: int,
        model_name: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        duration_ms: int = 0,
    ) -> UsageRecord:
        self.conn.execute(
            """
            INSERT INTO usage_records (
                run_id, installation_id, repository_id, model_name,
                prompt_tokens, completion_tokens, total_tokens, duration_ms
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                installation_id,
                repository_id,
                model_name,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                duration_ms,
            ),
        )
        self.conn.commit()
        row = self._fetchone(
            "SELECT * FROM usage_records WHERE id = last_insert_rowid()",
            (),
        )
        assert row is not None
        return _usage(row)

    def list_usage_records(self, *, run_id: str) -> list[UsageRecord]:
        rows = self.conn.execute(
            "SELECT * FROM usage_records WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [_usage(row) for row in rows]

    def _delivery_by_id(self, delivery_id: str) -> WebhookDeliveryRecord | None:
        row = self._fetchone(
            "SELECT * FROM webhook_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        )
        return _delivery(row) if row is not None else None

    def _fetchone(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()


def _installation(row: sqlite3.Row) -> InstallationRecord:
    return InstallationRecord.model_validate(dict(row))


def _repository(row: sqlite3.Row) -> RepositoryRecord:
    payload = dict(row)
    payload["enabled"] = bool(payload.get("enabled"))
    return RepositoryRecord.model_validate(payload)


def _run(row: sqlite3.Row) -> ReviewRunRecord:
    return ReviewRunRecord.model_validate(dict(row))


def _delivery(row: sqlite3.Row) -> WebhookDeliveryRecord:
    return WebhookDeliveryRecord.model_validate(dict(row))


def _tenant_config(row: sqlite3.Row) -> TenantConfigRecord:
    payload = dict(row)
    for key in ("review_enabled", "review_draft_prs", "publish_comments"):
        payload[key] = bool(payload.get(key))
    return TenantConfigRecord.model_validate(payload)


def _usage(row: sqlite3.Row) -> UsageRecord:
    return UsageRecord.model_validate(dict(row))
