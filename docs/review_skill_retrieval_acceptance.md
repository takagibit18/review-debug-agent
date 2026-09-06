# Review Skill Retrieval acceptance runbook

## Offline retrieval gate

Run from the repository root:

```powershell
python -m eval.skill_retrieval --skill-bank eval/skill_banks/retrieval-v1 --fixtures-dir eval/fixtures --output-json eval/reports/review-skill-retrieval-v1.json
```

The checked report must have `recall_at_k = 1.0`, zero candidate/deprecated selections, zero hard-budget violations, and zero budget loss. It must also record the effective `top_k`, `char_budget`, and `legacy_fallback_limit` contract. Run the command twice and compare the JSON files byte-for-byte; the report deliberately contains no timestamp.

The fixture bank is isolated from `review_skills/learned.jsonl`. Its README documents every human annotation, including the clean control.

The staged holdout in `eval/holdout/retrieval-v1.json` is denominator-neutral while
`annotation_status` is `pending`. Do not promote an entry until an independent
reviewer has adjudicated its expected skills. The holdout must remain disjoint from
the five currently annotated retrieval-v1 fixtures and should include a transfer
case, a same-language hard negative, and a clean control.

For a malformed metadata migration check, verify that deterministic retrieval
reports `malformed_active_records` and skips the affected record with
`malformed_metadata`; missing metadata must remain eligible for the bounded legacy
fallback. A no-scoped-match case must never select more than
`legacy_fallback_limit` legacy records.

## Regression gate

For each stacked branch, run its targeted commands from `docs/review_skill_retrieval_implementation_plan.md`. On the final branch also run:

```powershell
python -m pytest -q
python -m ruff check src eval tests --exclude eval/outputs
git diff --check codex/review-skill-retrieval-runtime...HEAD
```

Verify that production `review_skills/learned.jsonl` and `review_experience/feedback.jsonl` remain empty and that no dependency manifest changed.

## Provider-backed A/B (not part of the offline gate)

With provider credentials and an approved budget, run the same reviewed fixture subset, model, temperature, sample count, context mode, and graph cache mode twice. Use `skill_retrieval_mode=sequential` for the baseline and `skill_retrieval_mode=deterministic` for the candidate; point both variants at `eval/skill_banks/retrieval-v1` and require identical bank digests.

Do not switch the production default unless candidate finding recall, false-positive rate, schema/workflow validity, tool calls, prompt cost, and end-to-end p95 satisfy the quality gate in the implementation plan. The immediate runtime rollback is `REVIEW_SKILL_RETRIEVAL_MODE=sequential`.
