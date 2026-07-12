"""Helpers for deciding whether a review response is trusted."""

from __future__ import annotations

from src.analyzer.schemas import ReviewResponse


def find_blocking_review_error(response: ReviewResponse) -> str | None:
    """Return a model/runtime error that means the review result is not trusted."""
    for error in response.context.errors:
        message = error.message
        if error.category == "runtime" and (
            "Model analysis failed:" in message
            or "Authentication failed" in message
            or "auth_failed" in message
            or "Model response incomplete" in message
            or "Token budget exhausted" in message
        ):
            return message
    return None
