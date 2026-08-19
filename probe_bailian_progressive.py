"""Progressive Bailian probe — isolate which request element triggers 403.

The eval runner's exploration call sends (vs the simple probe):
  - top_p
  - tools (function definitions)
  - extra_body={"enable_thinking": True}   (dashscope thinking_format, thinking="high")

Tests each element in isolation to find the 403 trigger.
NEVER prints the API key.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from src.config import get_settings  # noqa: E402
from openai import OpenAI  # noqa: E402

SIMPLE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "say_hi",
            "description": "Reply with a greeting.",
            "parameters": {
                "type": "object",
                "properties": {"who": {"type": "string"}},
                "required": [],
            },
        },
    }
]


def call(label: str, s, **extra) -> None:
    base = {
        "model": "qwen3.7-max-2026-05-20",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 4,
        "temperature": 0.0,
    }
    base.update(extra)
    client = OpenAI(api_key=s.openai_api_key, base_url=s.openai_base_url)
    print(f"\n--- {label} ---")
    print(f"  extra keys: {sorted(extra.keys())}")
    try:
        resp = client.chat.completions.create(**base)
        print(f"  status = OK (200)")
        print(f"  finish_reason = {resp.choices[0].finish_reason!r}")
        print(f"  content = {resp.choices[0].message.content!r}")
    except Exception as e:  # noqa: BLE001
        ename = type(e).__name__
        status = getattr(e, "status_code", None) or getattr(e, "code", None)
        # Try to extract the raw Bailian body for the real reason.
        body = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            try:
                body = resp.content.decode("utf-8", errors="replace")[:400]
            except Exception:  # noqa: BLE001
                body = str(getattr(resp, "text", ""))[:400]
        print(f"  status = FAIL ({status})")
        print(f"  exception = {ename}")
        if body:
            print(f"  raw_body = {body}")


def main() -> int:
    s = get_settings()
    print(f"base_url = {s.openai_base_url}")
    print(f"api_key_present = {bool(s.openai_api_key)}")
    call("T1 baseline (model+msg+temp+max_tokens)", s)
    call("T2 + top_p=1.0", s, top_p=1.0)
    call("T3 + tools (function calling)", s, tools=SIMPLE_TOOL)
    call("T4 + extra_body enable_thinking=True", s, extra_body={"enable_thinking": True})
    call(
        "T5 full exploration shape (tools + enable_thinking + top_p)",
        s,
        tools=SIMPLE_TOOL,
        top_p=1.0,
        extra_body={"enable_thinking": True},
    )
    call("T6 tools + enable_thinking=False (submit shape)", s, tools=SIMPLE_TOOL, extra_body={"enable_thinking": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
