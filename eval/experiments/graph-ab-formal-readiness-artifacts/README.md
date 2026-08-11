# Graph A/B formal readiness diagnostic artifacts

- Experiment: `graph-ab-formal-readiness`
- Source branch: `experiment/graph-ab-formal-readiness`
- Source commit: `585e8f022b3db48f979d28345b0c074a7a7a29d3`
- Original logical output directories: `eval/outputs/graph-ab-formal-readiness/` and `eval/outputs/event_logs/`
- Generated at: `2026-08-06T03:59:34.793229+00:00`
- Manifest records: 27
- Invalid preview runs: 9
- Sanitized checkpoint records: 33

## Purpose

These files preserve the stable metadata needed to explain the formal readiness state, associate run records with repository snapshots and contract hashes, and diagnose the nine invalid preview runs. They can be used to resume investigation after moving to another computer, but they are not a substitute for the raw archive.

The remaining readiness blockers are `preflight_variants_incomplete_or_invalid` and `preflight_schema_invalid`. The repository is not ready for formal paired A/B collection.

## Sanitization

Only allowlisted fields were copied. Secret values, authorization headers, browser session material, environment variables, complete prompts, source code, raw tool arguments, API responses, raw model output, workspace indexes, and absolute local paths were excluded. Error messages were path- and secret-redacted and truncated to a diagnostic maximum.

`artifact-checksums.json` marks the selected raw files that must be included in the external archive. Inclusion must be confirmed against the handoff manifest and `SHA256SUMS.txt` after the export is created.

## Raw data not committed

The raw checkpoint, preflight and preview outputs, the nine invalid-run event logs, workspace caches, Graph SQLite indexes, and full run payloads remain outside Git. `.env`, credentials, browser data, virtual environments, Git object caches, and unrelated experiment outputs are neither committed nor selected for the handoff archive.

## Continue diagnosis from the external backup

Restore the Git bundle, check out `experiment/graph-ab-formal-readiness`, verify the frozen `eval/agent-baseline-v1` tag, and extract `raw-eval/MergeWarden-graph-ab-formal-readiness-raw.zip` outside the repository. Match raw files to these records by SHA-256. Do not commit the raw archive back into Git.

These artifacts were derived from existing outputs. No held-out evaluation or new model A/B run was executed to create them.
