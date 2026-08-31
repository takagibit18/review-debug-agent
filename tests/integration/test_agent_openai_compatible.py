"""Real Agent loop coverage backed by a deterministic local HTTP provider."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, cast

from src.analyzer.event_log import EventType
from src.analyzer.schemas import ReviewRequest
from src.orchestrator.agent_loop import AgentOrchestrator


class _ProviderServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]


class _OpenAICompatibleHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        server = cast(_ProviderServer, self.server)
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        server.requests.append(request)
        round_number = len(server.requests)

        if self.path != "/v1/chat/completions" or round_number > 3:
            self.send_error(500, "unexpected fake-provider request")
            return

        if round_number == 1:
            tool_call = _tool_call(
                "call-context",
                "get_changed_context",
                {"file_path": "pkg/service.py", "line": 2, "radius": 5},
            )
        elif round_number == 2:
            tool_call = _tool_call(
                "call-read",
                "read_file",
                {"file_path": "pkg/service.py", "offset": 0, "limit": 20},
            )
        else:
            tool_call = _tool_call(
                "call-submit",
                "submit_review",
                {
                    "summary": "The changed default enables an unsafe execution path.",
                    "issues": [
                        {
                            "severity": "warning",
                            "location": "pkg/service.py:2",
                            "evidence": "`return True` enables unsafe mode by default.",
                            "suggestion": "Keep the safe default unless callers opt in.",
                            "confidence": 0.95,
                        }
                    ],
                },
            )

        response = {
            "id": f"chatcmpl-offline-{round_number}",
            "object": "chat.completion",
            "created": 1,
            "model": "offline-reviewer",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [tool_call],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        encoded = json.dumps(response, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, separators=(",", ":")),
        },
    }


@contextmanager
def _fake_provider() -> Iterator[_ProviderServer]:
    server = _ProviderServer(("127.0.0.1", 0), _OpenAICompatibleHandler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_real_agent_uses_http_provider_tools_and_finding_guard(
    monkeypatch,
    tmp_path: Path,
) -> None:
    base_sha, head_sha, diff_text = _build_git_fixture(tmp_path)
    assert base_sha != head_sha
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setenv("MODEL_NAME", "offline-reviewer")
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("MODEL_MAX_RETRIES", "1")
    monkeypatch.setenv("MODEL_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("CONTEXT_SUMMARY_ENABLED", "false")
    monkeypatch.setenv("REVIEW_CONTEXT_MODE", "agent_search")
    monkeypatch.setenv("ROOT_CAUSE_CONSOLIDATION_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_DIR", ".mergewarden/logs")

    with _fake_provider() as provider:
        host, port = provider.server_address
        monkeypatch.setenv("OPENAI_BASE_URL", f"http://{host}:{port}/v1")
        orchestrator = AgentOrchestrator(
            review_max_iterations=3,
            review_min_tool_iterations=2,
            review_workflow_enforcement="off",
            review_diff_first_changed_files=False,
        )

        async def run_and_close():  # type: ignore[no-untyped-def]
            response = await orchestrator.run_review(
                ReviewRequest(
                    repo_path=str(tmp_path),
                    diff_mode=True,
                    diff_text=diff_text,
                )
            )
            assert orchestrator._model_client is not None  # noqa: SLF001
            await orchestrator._model_client.close()  # noqa: SLF001
            return response

        response = asyncio.run(run_and_close())

    assert len(provider.requests) == 3
    assert _tool_names(provider.requests[0]) >= {"get_changed_context", "read_file"}
    assert "submit_review" not in _tool_names(provider.requests[0])
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call-context"
        for message in provider.requests[1]["messages"]
    )
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "call-read"
        for message in provider.requests[2]["messages"]
    )
    assert [issue.location for issue in response.report.issues] == ["pkg/service.py:2"]
    assert response.report.issues[0].confidence == 0.95

    events = [
        json.loads(line)
        for line in (
            tmp_path / ".mergewarden" / "logs" / f"{response.run_id}.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    verification = next(
        event
        for event in events
        if event["event_type"] == EventType.FINDING_VERIFICATION_COMPLETED.value
    )
    assert verification["payload"]["accepted_count"] == 1
    assert verification["payload"]["verifier_kind"] == "integrity_guard"


def _tool_names(request: dict[str, Any]) -> set[str]:
    return {
        str(tool["function"]["name"])
        for tool in request.get("tools", [])
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
    }


def _build_git_fixture(repo: Path) -> tuple[str, str, str]:
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Offline Test")
    _git(repo, "config", "user.email", "offline@example.test")
    target = repo / "pkg" / "service.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def unsafe_mode():\n    return False\n", encoding="utf-8")
    _git(repo, "add", "pkg/service.py")
    _git(repo, "commit", "--quiet", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    target.write_text("def unsafe_mode():\n    return True\n", encoding="utf-8")
    _git(repo, "add", "pkg/service.py")
    _git(repo, "commit", "--quiet", "-m", "head")
    head_sha = _git(repo, "rev-parse", "HEAD")
    diff_text = _git(repo, "diff", "--no-ext-diff", base_sha, head_sha)
    return base_sha, head_sha, diff_text


def _git(repo: Path, *args: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
        }
    )
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    return completed.stdout.strip()
