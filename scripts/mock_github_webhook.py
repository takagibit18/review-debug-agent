"""Send a signed mock GitHub webhook to a local MergeWarden server."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a signed mock GitHub webhook to /github/webhook."
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8000/github/webhook",
        help="Webhook URL. Defaults to http://localhost:8000/github/webhook.",
    )
    parser.add_argument(
        "--secret",
        default=os.getenv("GITHUB_WEBHOOK_SECRET", ""),
        help="Webhook secret. Defaults to GITHUB_WEBHOOK_SECRET.",
    )
    parser.add_argument(
        "--event",
        default="pull_request",
        help="GitHub event name. Defaults to pull_request.",
    )
    parser.add_argument(
        "--delivery-id",
        default="",
        help="Delivery id. Defaults to a generated UUID.",
    )
    parser.add_argument(
        "--action",
        default="opened",
        help="Pull request action used by the generated payload.",
    )
    parser.add_argument(
        "--payload-json",
        default="",
        help="Optional path to a JSON payload file. If omitted, a sample PR payload is generated.",
    )
    args = parser.parse_args(argv)

    if not args.secret:
        parser.error("--secret or GITHUB_WEBHOOK_SECRET is required")

    payload = _load_payload(args.payload_json, action=args.action)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    delivery_id = args.delivery_id or str(uuid.uuid4())
    request = Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": args.event,
            "X-GitHub-Delivery": delivery_id,
            "X-Hub-Signature-256": _signature(body, args.secret),
        },
    )

    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            text = response.read().decode("utf-8", errors="replace")
            print(f"status={response.status}")
            print(text)
            return 0
    except HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        print(f"status={exc.status}", file=sys.stderr)
        print(text, file=sys.stderr)
        return 1


def _load_payload(path: str, *, action: str) -> dict[str, Any]:
    if path:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise SystemExit("payload JSON must be an object")
        return raw
    return {
        "action": action,
        "number": 1,
        "repository": {"full_name": "owner/repo"},
        "pull_request": {
            "number": 1,
            "draft": False,
            "head": {"sha": "head-sha"},
            "base": {"sha": "base-sha"},
        },
        "installation": {"id": 123456},
        "sender": {"login": "octocat", "type": "User"},
    }


def _signature(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


if __name__ == "__main__":
    raise SystemExit(main())
