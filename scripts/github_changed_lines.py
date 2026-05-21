"""Generate changed-lines JSON for GitHub advisory publishing."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analyzer.diff_lines import changed_new_lines_by_file  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a unified PR diff into {path: [new-side changed lines]} JSON."
    )
    parser.add_argument(
        "--diff-file",
        help="Path to a unified diff file. Use '-' to read from stdin.",
    )
    parser.add_argument("--base", help="Base git ref for local diff generation.")
    parser.add_argument("--head", help="Head git ref for local diff generation.")
    parser.add_argument(
        "--repo",
        default=".",
        help="Repository path used with --base/--head. Defaults to current directory.",
    )
    parser.add_argument(
        "--output",
        help="Output JSON path. Defaults to stdout.",
    )
    args = parser.parse_args(argv)

    diff_text = _load_diff_text(parser, args)
    payload = {
        path: sorted(lines)
        for path, lines in sorted(changed_new_lines_by_file(diff_text).items())
        if lines
    }
    output = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


def _load_diff_text(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    if args.diff_file and (args.base or args.head):
        parser.error("--diff-file cannot be combined with --base/--head")
    if args.diff_file:
        if args.diff_file == "-":
            return sys.stdin.read()
        return Path(args.diff_file).read_text(encoding="utf-8")
    if not args.base or not args.head:
        parser.error("provide --diff-file or both --base and --head")
    result = subprocess.run(
        [
            "git",
            "-C",
            args.repo,
            "diff",
            "--no-ext-diff",
            args.base,
            args.head,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "git diff failed")
    return result.stdout


if __name__ == "__main__":
    raise SystemExit(main())
