"""Probe qwen3.7-max auth against the .env endpoint. Minimal cost (max_tokens=1).

NEVER prints the API key — only api_key_present and HTTP status / finish_reason.
"""
from __future__ import annotations
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from src.config import get_settings  # noqa: E402
from openai import OpenAI  # noqa: E402


def probe(model: str, s) -> dict:
    out = {"model": model}
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
        out["status"] = "OK"
        out["finish_reason"] = resp.choices[0].finish_reason
        out["content"] = resp.choices[0].message.content
        print(f"  status = OK (200)")
        print(f"  finish_reason = {resp.choices[0].finish_reason!r}")
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status_code", None) or getattr(e, "code", None)
        msg = getattr(e, "message", None) or str(e)
        out["status"] = f"FAIL({status})"
        out["exception"] = type(e).__name__
        out["message"] = str(msg)[:300]
        print(f"  status = FAIL ({status})")
        print(f"  exception = {type(e).__name__}")
        print(f"  message = {str(msg)[:300]}")
    return out


def main() -> int:
    s = get_settings()
    print(f"settings.model_name (from .env) = {s.model_name!r}")
    models = ["qwen3.7-max", "qwen3.7-max-2026-05-20"]
    results = []
    for m in models:
        results.append(probe(m, s))
    print("\n=== SUMMARY ===")
    for r in results:
        print(f"  {r['model']}: {r['status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
