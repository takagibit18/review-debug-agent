# MergeWarden Platform MVP

This document describes the minimal backend platform layer for GitHub App mode.
It is a deployable MVP shape, not a complete SaaS product.

## What It Supports

- SQLite-backed records for installations, repositories, webhook deliveries, review runs, tenant configs, and usage records.
- DB-backed webhook idempotency by GitHub delivery id and by `repo/pr/head_sha` for queued, running, and succeeded runs.
- Durable queued review runs processed by a local DB-polling worker.
- A run state machine: `queued`, `running`, `succeeded`, `failed`, `cancelled`, `skipped`.
- Local artifact storage under `.mergewarden/platform-artifacts/<run_id>/` by default.
- Basic management APIs for health, installations, repositories, runs, retries, and deliveries.
- Tenant config resolution with priority: repository config, installation config, global settings.
- Advisory-only GitHub publishing. `publish_comments=false` still allows the neutral check path while suppressing inline comments.

## What It Does Not Support

- No user login, dashboard permissions, or management API authentication.
- No billing, quota enforcement, or customer account system.
- No formal multi-replica worker lock. The SQLite MVP is intended for one local worker process.
- No object storage. Artifacts are local files.
- No Kubernetes, distributed queue, or SaaS control plane.
- Default SQLite is only suitable for local development and small validation deployments.
- Management APIs are local/internal MVP endpoints only. Production deployments must add authentication and network access controls.

## Configuration

Important environment variables:

```text
PLATFORM_DATABASE_URL=sqlite:///.mergewarden/platform.db
PLATFORM_ARTIFACT_ROOT=.mergewarden/platform-artifacts
PLATFORM_INIT_DB_ON_STARTUP=true
PLATFORM_REVIEW_ENABLED=true
PLATFORM_PUBLISH_COMMENTS=true
PLATFORM_WORKER_POLL_INTERVAL_SECONDS=2.0
GITHUB_REVIEW_DRAFT_PRS=false
GITHUB_WEBHOOK_SECRET=<github webhook secret>
GITHUB_AUTH_MODE=app
GITHUB_APP_ID=<github app id>
GITHUB_PRIVATE_KEY=<github app private key>
```

Model and budget defaults still come from existing settings:

```text
MODEL_NAME=gpt-4o
TOKEN_BUDGET=30000
PROMPT_INPUT_TOKEN_BUDGET=32000
```

## Initialize The Database

The API and worker initialize tables automatically when `PLATFORM_INIT_DB_ON_STARTUP=true`.
You can also initialize explicitly:

```bash
python cli.py platform init-db
```

Tests can point `PLATFORM_DATABASE_URL` at a temp SQLite file, for example:

```text
PLATFORM_DATABASE_URL=sqlite:///C:/tmp/mergewarden-platform-test.db
```

## Start The API

```bash
uvicorn src.api.app:app --reload
```

Useful endpoints:

- `GET /platform/health`
- `GET /platform/installations`
- `GET /platform/repositories?installation_id=1`
- `GET /platform/runs?repo_full_name=owner/repo&pr_number=7&status=queued`
- `GET /platform/runs/{run_id}`
- `POST /platform/runs/{run_id}/retry`
- `GET /platform/deliveries?status=ignored`

`POST /platform/runs/{run_id}/retry` creates a new queued run instead of mutating the old run. This keeps the failed run as an immutable audit record.

## Start The Worker

Process one queued run and exit:

```bash
python cli.py platform worker --once
```

Poll forever:

```bash
python cli.py platform worker
```

The MVP worker uses SQLite polling and is documented as single-worker. It claims a run by moving it from `queued` to `running` inside a SQLite `BEGIN IMMEDIATE` transaction. This is enough for the local MVP but is not a production multi-replica queue lock.

## GitHub App Webhook Setup

Configure the GitHub App webhook URL:

```text
https://<your-host>/github/webhook
```

Required webhook secret:

```text
GITHUB_WEBHOOK_SECRET=<same secret configured in GitHub>
```

Subscribe to pull request events. The platform reviews these actions:

- `opened`
- `reopened`
- `synchronize`
- `ready_for_review`

The route verifies `X-Hub-Signature-256`, records `webhook_deliveries`, and returns quickly with `queued`, `duplicate`, `ignored`, or `ok`.

## Local Mock Webhook Test

Use the existing helper script after setting the platform and GitHub App env vars:

```bash
python scripts/mock_github_webhook.py
```

Or post a signed pull request fixture to:

```text
POST http://localhost:8000/github/webhook
```

Then inspect:

```bash
curl http://localhost:8000/platform/runs
curl http://localhost:8000/platform/deliveries
python cli.py platform worker --once
curl http://localhost:8000/platform/runs/<run_id>
```

## Database Tables

- `installations`: GitHub installation/account identity and status.
- `repositories`: repositories attached to an installation and whether they are enabled.
- `review_runs`: durable review queue, status machine, errors, artifact paths, publish status, and token totals.
- `webhook_deliveries`: delivery audit and duplicate/ignored troubleshooting records.
- `tenant_configs`: installation-level and repository-level review/publish/model/budget settings.
- `usage_records`: per-run usage audit extracted from worker results or run summaries.

## Artifacts

Default root:

```text
.mergewarden/platform-artifacts/<run_id>/
```

Possible files:

- `review_response.json`
- `run_summary.json`
- `publish_result.json`
- `pr.diff`
- `changed_lines.json`
- `event_logs/*.jsonl`

`review_runs` stores relative artifact paths such as `<run_id>/review_response.json`. List endpoints return metadata paths only and do not inline large artifact contents.

## Tenant Boundaries

The MVP separates installations, repositories, runs, webhook deliveries, configs, and usage records in the database. Webhook decisions resolve config with this order:

```text
repository config > installation config > global settings
```

This affects:

- `review_enabled`
- `review_draft_prs`
- `publish_comments`
- `model_name`
- `token_budget`
- `prompt_input_token_budget`

The worker passes `model_name` into the review request and uses the resolved budgets for audit/config resolution. The current orchestrator still reads token budget settings globally in some deeper components, so full per-run budget isolation remains a production follow-up.

## Production Gaps

Before treating this as a production SaaS backend, add:

- Authentication and authorization for all `/platform/*` management APIs.
- PostgreSQL migrations and transaction/locking semantics designed for multiple API and worker replicas.
- A real queue or robust DB job leasing with stale-run recovery.
- Object storage for artifacts and lifecycle/retention policies.
- Tenant onboarding, account ownership, and audit-log access controls.
- Quotas, billing, and abuse controls.
- Observability for queue depth, run duration, failures, and GitHub API/model provider usage.
- Secret management and environment separation for staging/production.
