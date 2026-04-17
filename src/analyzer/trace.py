"""Trace helpers for compact and safe event payloads."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.analyzer.event_log import EventType
from src.config import TraceDetailMode

_SENSITIVE_KEYWORDS = (
    "api_key",
    "token",
    "password",
    "secret",
    "authorization",
    "cookie",
    "session",
    "credential",
)


class TraceRecorder:
    """Prepare trace payloads with truncation and redaction."""

    def __init__(
        self,
        *,
        detail_mode: TraceDetailMode = "off",
        max_chars: int = 1200,
        log_tool_body: bool = False,
    ) -> None:
        self._detail_mode = detail_mode
        self._max_chars = max(64, max_chars)
        self._log_tool_body = log_tool_body

    @property
    def detail_mode(self) -> TraceDetailMode:
        return self._detail_mode

    def allows_detail(self) -> bool:
        return self._detail_mode != "off"

    def build_text_preview(self, text: str | None) -> str:
        text = (text or "").strip()
        if not text:
            return ""
        if len(text) <= self._max_chars:
            return text
        return f"{text[: self._max_chars]}...(truncated {len(text) - self._max_chars} chars)"

    def build_tool_call_summaries(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for raw in tool_calls:
            function_block = raw.get("function") if isinstance(raw, dict) else {}
            if not isinstance(function_block, dict):
                continue
            name = str(function_block.get("name", "")).strip()
            arguments = function_block.get("arguments", {})
            payload = self._parse_arguments(arguments)
            summary: dict[str, Any] = {"name": name or "unknown", "args_digest": self._digest(payload)}
            if self._detail_mode == "full":
                summary["args_preview"] = self._sanitize(payload)
            summaries.append(summary)
        return summaries

    def build_tool_result_preview(self, result: Any) -> dict[str, Any]:
        if result is None:
            return {"type": "none"}
        payload = result
        if hasattr(result, "model_dump"):
            payload = result.model_dump()
        digest = self._digest(payload)
        if self._detail_mode == "full" and self._log_tool_body:
            return {"digest": digest, "preview": self._sanitize(payload)}
        return {"digest": digest}

    def record(
        self,
        event_writer: Any,
        event_type: EventType,
        phase: str,
        payload: dict[str, Any],
    ) -> None:
        if not self.allows_detail():
            return
        event_writer(event_type, phase, payload)

    def _parse_arguments(self, arguments: Any) -> Any:
        if isinstance(arguments, str):
            try:
                return json.loads(arguments)
            except Exception:  # noqa: BLE001
                return {"raw": self.build_text_preview(arguments)}
        return arguments

    def _digest(self, payload: Any) -> dict[str, Any]:
        serialized = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
        return {
            "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "length": len(serialized),
        }

    def _sanitize(self, value: Any, *, key: str = "") -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for item_key, item_value in value.items():
                lower_key = str(item_key).lower()
                if self._is_sensitive(lower_key):
                    sanitized[item_key] = "[REDACTED]"
                    continue
                sanitized[item_key] = self._sanitize(item_value, key=lower_key)
            return sanitized
        if isinstance(value, list):
            return [self._sanitize(item, key=key) for item in value]
        if isinstance(value, str):
            if self._is_sensitive(key):
                return "[REDACTED]"
            return self.build_text_preview(value)
        return value

    @staticmethod
    def _is_sensitive(key: str) -> bool:
        return any(keyword in key for keyword in _SENSITIVE_KEYWORDS)
