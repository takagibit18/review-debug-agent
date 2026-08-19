"""Minimal Bailian auth probe — isolate the 403 cause.

Tests the .env API key against BOTH model names:
  - qwen3.6-flash            (from experiment YAML)
  - qwen3.7-max-2026-05-20   (from .env MODEL_NAME default)

Prints ONLY: model name, HTTP status, finish_reason / error message.
NEVER prints the API key.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from src.config import get_settings  # noqa: E402
from openai import OpenAI  # noqa: E402


def probe(model: str, s) -> None:
    print(f"\n--- probe model={model!r} ---")
    print(f"  base_url = {s.openai_base_url}")
    print(f"  api_key_present = {bool(s.openai_api_key)}")
    client = OpenAI(api_key=s.openai_api_key, base_url=s.openai_base_url)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
            temperature=0.0,
        )
        print(f"  status = OK (200)")
        print(f"  finish_reason = {resp.choices[0].finish_reason!r}")
        print(f"  content = {resp.choices[0].message.content!r}")
    except Exception as e:  # noqa: BLE001
        ename = type(e).__name__
        # OpenAI SDK exceptions carry .status_code / .code / .message
        status = getattr(e, "status_code", None) or getattr(e, "code", None)
        msg = getattr(e, "message", None) or str(e)
        # Truncate message; never include the key (it won't be in the error body anyway).
        print(f"  status = FAIL ({status})")
        print(f"  exception = {ename}")
        print(f"  message = {str(msg)[:300]}")


def main() -> int:
    s = get_settings()
    models = ["qwen3.6-flash", "qwen3.7-max-2026-05-20"]
    for m in models:
        probe(m, s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
