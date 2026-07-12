"""Static contract checks for the GitHub advisory workflow."""

from pathlib import Path


def test_github_advisory_workflow_wires_phase2_publish_loop() -> None:
    workflow = Path(".github/workflows/github-advisory.yml").read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "contents: read" in workflow
    assert "checks: write" in workflow
    assert "pull-requests: write" in workflow
    assert "scripts/github_changed_lines.py" in workflow
    assert "python cli.py review . --diff" in workflow
    assert "github-advisory publish" in workflow
    assert "--dry-run" in workflow
    assert "--publish" in workflow
    assert "--summary-json \"$RUN_SUMMARY_JSON\"" in workflow
    assert "Model provider key prefix" not in workflow
    assert "TOKEN_BUDGET: ${{ vars.TOKEN_BUDGET || '260000' }}" in workflow
    assert "TOKEN_HARD_BUDGET: ${{ vars.TOKEN_HARD_BUDGET || '340000' }}" in workflow
    assert "PROMPT_INPUT_TOKEN_BUDGET: ${{ vars.PROMPT_INPUT_TOKEN_BUDGET || '32000' }}" in workflow
    assert "MODEL_MAX_TOKENS: ${{ vars.MODEL_MAX_TOKENS || '8192' }}" in workflow
    assert "FILE_CONTEXT_MAX_FILES: ${{ vars.FILE_CONTEXT_MAX_FILES || '8' }}" in workflow
    assert "FILE_CONTEXT_MAX_CHARS_PER_FILE: ${{ vars.FILE_CONTEXT_MAX_CHARS_PER_FILE || '6000' }}" in workflow
    assert "FILE_CONTEXT_MAX_CHARS_TOTAL: ${{ vars.FILE_CONTEXT_MAX_CHARS_TOTAL || '32000' }}" in workflow
    assert "AGENT_TRACE_DETAIL: ${{ vars.AGENT_TRACE_DETAIL || 'compact' }}" in workflow
    assert "actions/checkout@v6" in workflow
    assert "actions/setup-python@v6" in workflow
    assert "actions/upload-artifact@v7" in workflow
