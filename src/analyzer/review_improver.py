"""Thin ModelClient completion adapter for review-skill proposals."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from src.models.client import ModelClient
from src.models.schemas import Message


def parse_proposal_json(content: str) -> dict[str, Any]:
    """Parse a plain or JSON-fenced model response without repairing it."""

    raw = content.strip()
    if raw.startswith("```json") and raw.endswith("```"):
        raw = raw[len("```json") : -len("```")].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("model response is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("model response JSON must be an object")
    return parsed


def complete_with_model(prompt: str) -> dict[str, Any]:
    """Complete one distillation prompt with the configured ModelClient."""

    async def _complete() -> dict[str, Any]:
        client = ModelClient()
        try:
            response = await client.chat([Message(role="user", content=prompt)])
            return parse_proposal_json(response.content)
        finally:
            await client.close()

    return asyncio.run(_complete())
