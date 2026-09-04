"""Shared tokenizer and deterministic serialization helpers for input telemetry."""

from __future__ import annotations

import hashlib
import json
from typing import Any

try:
    import tiktoken as _tiktoken
except Exception:  # noqa: BLE001
    _tiktoken = None


TOKENIZER_NAME = "cl100k_base"


def estimate_tokens(text: str) -> int:
    """Estimate tokens with the one tokenizer used by prompt and request telemetry."""

    if not text:
        return 0
    if _tiktoken is None:
        return max(1, len(text) // 4)
    return len(_tiktoken.get_encoding(TOKENIZER_NAME).encode(text))


def serialize_json(value: Any) -> str:
    """Serialize a telemetry component deterministically without exposing secrets."""

    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def component_hash(text: str) -> str:
    """Return a stable digest for a serialized component."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def common_prefix_tokens(left: str, right: str) -> int:
    """Count the token prefix shared by two serialized provider requests."""

    if not left or not right:
        return 0
    if _tiktoken is not None:
        encoding = _tiktoken.get_encoding(TOKENIZER_NAME)
        left_tokens = encoding.encode(left)
        right_tokens = encoding.encode(right)
        limit = min(len(left_tokens), len(right_tokens))
        index = 0
        while index < limit and left_tokens[index] == right_tokens[index]:
            index += 1
        return index
    common_chars = 0
    for left_char, right_char in zip(left, right, strict=False):
        if left_char != right_char:
            break
        common_chars += 1
    return common_chars // 4


def token_component(name: str, text: str) -> dict[str, Any]:
    """Return the safe size/hash record for one prompt component."""

    return {
        "component": name,
        "chars": len(text),
        "estimated_tokens": estimate_tokens(text),
        "component_hash": component_hash(text),
    }
