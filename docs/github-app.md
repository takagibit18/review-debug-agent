# GitHub App PR Review Setup

This guide configures MergeWarden as a GitHub App bot. The API receives PR
webhooks and durably enqueues review runs. A separate worker exchanges
`installation_id` for a short-lived installation token, runs the existing
review pipeline, and publishes advisory checks/comments as the GitHub App.

## 1. Create The GitHub App

1. Open GitHub:
   `Settings -> Developer settings -> GitHub Apps -> New GitHub App`.
2. Set a name such as `MergeWarden Review Bot`.
3. Set Homepage URL to your product or repository URL.
4. Set Webhook URL:
   - Local development: `https://<your-ngrok-domain>/github/webhook`
   - Production: `https://<your-backend-domain>/github/webhook`
5. Generate a strong Webhook secret and save it as `GITHUB_WEBHOOK_SECRET`.

## 2. Permissions

Set Repository permissions:

| Permission | Access |
|---|---|
| Metadata | Read |
| Contents | Read |
| Pull requests | Read and write |
| Checks | Read and write |

Subscribe to events:

| Event | Required |
|---|---|
| Ping | GitHub sends this automatically when saving the webhook |
| Pull request | Yes |
| Installation | Yes |
| Installation repositories | Optional |

## 3. Private Key

In the GitHub App settings, generate a private key and download the `.pem`.

For local `.env`, either paste the multiline PEM directly if your shell supports
it, or convert newlines to literal `\n`:

```text
GITHUB_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----
```

MergeWarden normalizes literal `\n` sequences automatically.

## 4. Environment Variables

Minimal GitHub App mode:

```bash
GITHUB_AUTH_MODE=app
GITHUB_APP_ID=123456
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET="your-webhook-secret"
APP_BASE_URL=https://your-backend.example.com

OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat
```

Existing token mode remains supported:

```bash
GITHUB_AUTH_MODE=token
GITHUB_TOKEN=ghp_or_ghs_token
```

If `GITHUB_AUTH_MODE` is missing, MergeWarden keeps the previous token-mode
behavior. `GITHUB_APP_MODE=true` is also accepted and switches to app mode.

## 5. Start The Service

Create `.env` from `.env.example`, configure the GitHub App and model variables
shown above, then start the hosted runtime:

```bash
docker compose up -d --build
```

Compose starts one FastAPI API service on port `8000` and one continuously
polling Platform Worker. Both services use the same `/data` volume for the
SQLite database, review artifacts, and event logs. The API only validates and
enqueues webhook work; the worker processes each `ReviewRun` asynchronously.
The API service also enables `PLATFORM_PUBLIC_GITHUB_APP_ONLY`, so only
`GET /health` and `POST /github/webhook` are exposed by this public runtime.

Health check:

```bash
curl http://localhost:8000/health
```

## 6. Local Webhook Test With ngrok

Start the hosted runtime:

```bash
docker compose up -d --build
```

Expose it:

```bash
ngrok http 8000
```

Use the HTTPS forwarding URL as the GitHub App webhook URL:

```text
https://<ngrok-domain>/github/webhook
```

Create a PR in a repository where the app is installed. For these actions,
MergeWarden attempts a review:

- `opened`
- `reopened`
- `synchronize`
- `ready_for_review`

Draft PRs are skipped unless:

```bash
GITHUB_REVIEW_DRAFT_PRS=true
```

You can also send a locally signed mock delivery before connecting GitHub:

```bash
GITHUB_WEBHOOK_SECRET=local-secret \
python scripts/mock_github_webhook.py \
  --url http://localhost:8000/github/webhook \
  --secret local-secret \
  --event pull_request \
  --action opened
```

The sample payload uses `installation.id=123456`. In app mode this will reach
the review worker and then fail token exchange unless you provide a real
installation id and GitHub App credentials. For signature and routing tests,
that is enough to verify the webhook boundary.

## 7. Production Deployment

Run the Compose API and worker services on a Docker host. Configure `.env` with:

```bash
APP_BASE_URL=https://mergewarden.example.com
GITHUB_AUTH_MODE=app
GITHUB_APP_ID=...
GITHUB_PRIVATE_KEY=...
GITHUB_WEBHOOK_SECRET=...
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
MODEL_NAME=...
```

Then update the GitHub App webhook URL:

```text
https://mergewarden.example.com/github/webhook
```

Compose exposes HTTP on port `8000`; provide HTTPS, DNS, and ingress in the
deployment environment. The named volume persists `/data/platform.db`,
`/data/platform-artifacts`, and `/data/logs` across container restarts.

Install the GitHub App into the target test repository and open a PR. The
published check/comment should show the GitHub App bot identity.

## 8. Duplicate Handling

Webhook intake uses the shared SQLite database for durable idempotency and
queueing:

- duplicate `X-GitHub-Delivery` is ignored
- duplicate `repo + pull_number + head_sha` is ignored

The API inserts a queued `ReviewRun` and returns without running the model.
The single Platform Worker claims queued runs from the same database, writes
artifacts and event logs to the shared volume, and publishes the GitHub advisory.

Set this only for deliberate rerun testing:

```bash
GITHUB_WEBHOOK_ALLOW_RERUN=true
```

## 9. Troubleshooting

`401 invalid signature`:

- Check the GitHub App webhook secret.
- Confirm the request reaches `/github/webhook` unchanged by proxies.
- Confirm the request includes `X-Hub-Signature-256`; unsigned requests are rejected.

`missing_installation_id`:

- Ensure the webhook comes from a GitHub App installation event or PR event.
- Confirm `GITHUB_AUTH_MODE=app`.

`GITHUB_PRIVATE_KEY is required`:

- Configure the downloaded `.pem`.
- Preserve PEM delimiters and newline characters.

Review runs but no comments appear:

- Confirm the app has `Pull requests: Read and write`.
- Confirm the finding location is on a changed line; otherwise it is summary-only.

`403 GitHub permission denied`:

- Confirm the app is installed on the repository.
- Confirm repository permissions include `Contents: Read`, `Pull requests: Read and write`, and `Checks: Read and write`.
- Reinstall or update the GitHub App installation after changing permissions.

Webhook not delivered:

- Check the GitHub App "Recent Deliveries" tab.
- Confirm the webhook URL is public HTTPS for GitHub-hosted delivery.
- For local testing, confirm ngrok/cloudflared is still running and points to port `8000`.

Comment or check publication failed:

- Inspect backend logs for `delivery_id`, `owner_repo`, and `pull_number`.
- Confirm the installation access token was created for the same repository installation.
- Confirm the PR head SHA in the event still exists.

Token mode still works through the existing CLI and GitHub Actions flow:

```bash
python cli.py github-advisory publish \
  --repo owner/repo \
  --pr-number 123 \
  --head-sha "$HEAD_SHA" \
  --response-json review_response.json \
  --changed-lines-json changed_lines.json \
  --publish
```
