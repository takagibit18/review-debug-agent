"""Tests for GitHub PR changed-line extraction."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.analyzer.diff_lines import changed_new_lines_by_file


PR_DIFF = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,5 @@
 context
-old_call()
+new_call()
+guard()
 context
@@ -10,2 +11,3 @@
 second_context
+tail_call()
diff --git a/src/new.py b/src/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,2 @@
+first()
+second()
diff --git a/src/deleted.py b/src/deleted.py
deleted file mode 100644
index 4444444..0000000
--- a/src/deleted.py
+++ /dev/null
@@ -1,2 +0,0 @@
-old
-lines
"""


def test_changed_new_lines_by_file_parses_pr_diff_new_side_lines() -> None:
    changed = changed_new_lines_by_file(PR_DIFF)

    assert changed == {
        "src/app.py": {2, 3, 12},
        "src/new.py": {1, 2},
    }


def test_github_changed_lines_script_writes_stable_json(tmp_path: Path) -> None:
    diff_path = tmp_path / "pr.diff"
    output_path = tmp_path / "changed_lines.json"
    diff_path.write_text(PR_DIFF, encoding="utf-8")
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [
            sys.executable,
            "scripts/github_changed_lines.py",
            "--diff-file",
            str(diff_path),
            "--output",
            str(output_path),
        ],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output_path.read_text(encoding="utf-8")) == {
        "src/app.py": [2, 3, 12],
        "src/new.py": [1, 2],
    }
