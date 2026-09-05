# Retrieval holdout v1

This directory is the staging area for a real-fixture holdout. It is deliberately
separate from `eval/skill_banks/retrieval-v1`: entries listed here are not part of
the current retrieval denominator and do not carry `expected_skill_ids` yet.

Promotion rules:

1. Keep the fixture IDs disjoint from the annotated retrieval-v1 set.
2. A reviewer must inspect the diff and expected finding before adding an
   `expected_skill_ids` annotation.
3. Record whether the fixture is a positive transfer case, a same-language hard
   negative, a graph-only/lexical-miss case, or a clean control.
4. Only an independently reviewed annotation may be copied into the retrieval
   evaluator; pending entries must remain denominator-neutral.
5. Run the retrieval evaluator twice after promotion and compare the reports
   byte-for-byte (excluding no fields—the report is intentionally timestamp-free).

The initial manifest uses real fixtures already present in `eval/fixtures/` and
contains hypotheses only. It intentionally does not assert a target skill or
claim holdout accuracy.

| Fixture | Role | Why it is useful |
| --- | --- | --- |
| `golden_pydantic_pydantic-ai_pr6205_reverse` | positive transfer candidate | Behavior-bearing `FileUrl` state is lost across parallel adapter round-trips; tests derived-state and cross-file invariant routing. |
| `golden_pydantic_pydantic-ai_pr7374` | clean control | Same repository family and Python surface, but no annotated review issue. |
| `golden_real_requests_netrc_pr7205` | same-language hard negative | Python fallback/default handling without the offline-discovery mechanism used by the annotated SpeechRecognition case. |
| `golden_vybestack_llxprt-code_pr3012_reverse` | provider/retry hard negative | A retry-policy invariant with provider errors; useful for checking that unrelated fallback skills do not leak in. |
| `golden_pytest-dev_pytest_pr8513` | same-framework hard negative | Pytest precision behavior, distinct from wrapper equality/hashing. |

Before promotion, add a reviewed annotation record to the manifest or a future
annotation file and include the reviewer/date in the change log. Do not turn the
hypotheses above into labels automatically.
