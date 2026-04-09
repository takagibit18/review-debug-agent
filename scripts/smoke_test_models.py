"""Minimal smoke test for the model client data flow.

Run from project root:
    python scripts/smoke_test_models.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


async def run_smoke_test() -> None:
    """Send one request and print normalized response fields."""
    from src.models import Message, ModelClient, ModelClientError

    # import local modules inside the function:instead of import at the top of the file
    client = ModelClient()
    try:
        response = await client.chat(
            [Message(role="user", content="Say hi in one word.")]
        )
        print("content:", repr(response.content))
        print("usage:", response.usage.model_dump())
        print("model:", response.model)
        print("finish_reason:", response.finish_reason)
    except ModelClientError as exc:
        print("model client error:", str(exc))
        raise
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
