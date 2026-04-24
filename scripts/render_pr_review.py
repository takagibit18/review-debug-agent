"""Render a structured review JSON file into a GitHub PR comment."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.analyzer.schemas import ReviewResponse
from src.reporting.pr_review_comment import render_pr_review_comment


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a review JSON payload into Markdown for PR comments."
    )
    parser.add_argument("--input", required=True, help="Path to the review JSON file.")
    parser.add_argument("--output", required=True, help="Path to the output Markdown file.")
    parser.add_argument("--repo", default="", help="GitHub repository name, e.g. owner/name.")
    parser.add_argument("--pr-number", type=int, default=None, help="Pull request number.")
    parser.add_argument("--commit-sha", default="", help="Head commit SHA.")
    parser.add_argument(
        "--max-issues-per-section",
        type=int,
        default=5,
        help="Maximum number of findings rendered per triage section.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    response = ReviewResponse.model_validate_json(
        Path(args.input).read_text(encoding="utf-8")
    )
    markdown = render_pr_review_comment(
        response,
        repository=args.repo,
        pr_number=args.pr_number,
        commit_sha=args.commit_sha,
        max_issues_per_section=max(1, args.max_issues_per_section),
    )
    Path(args.output).write_text(markdown, encoding="utf-8")


if __name__ == "__main__":
    main()
