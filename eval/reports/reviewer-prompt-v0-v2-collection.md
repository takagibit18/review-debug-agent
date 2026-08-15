# Reviewer Prompt V0-V2 Collection

> Two valid optimized `deepseek-v4-pro` samples, run sequentially with one provider attempt each. The formal contract threshold remained 90 seconds; a diagnostic-only 180-second cap allowed late responses to be observed. No retry, fallback, Graph run, or full Reviewer loop was used.

## Runtime Contract

- Branch / HEAD: `codex/reviewer-prompt-v0-v2` / `ae2b2bf6203fbb9c32ce648b7f26731493d601ed`
- Fixture / path: `golden_pytest-dev_pytest_pr9350` / first real A-agent-search Reviewer request
- Model / provider: `deepseek-v4-pro` / DeepSeek OpenAI-compatible completions
- Temperature / thinking: `0` / `high` with `reasoning_effort=high`
- Default / actual exploration max output: `4096` / `12288`
- Contract threshold / collection cap: `90s` / `180s`
- ModelClient outer attempts / diagnostic SDK retries: `1` / `0`

## Valid Sample Matrix

| Sample | run_id | Prompt Tokens | Completion Tokens | Tool Calls | Latency | Completed <90s | Completed <180s | Termination |
|---|---|---:|---:|---:|---:|---|---|---|
| 1 | `554129a3-acb2-47d7-9ea6-420a2f080383` | 10,536 | 5,260 | 4 | 67,181 ms | YES | YES | completed |
| 2 | `3aa247a4-b3a0-4136-94db-2ff17749e6d4` | 10,542 | 9,122 | 4 | 110,501 ms | NO | YES | completed |

Observed range: `67.181s - 110.501s`. One of two valid samples would time out under the unchanged 90-second contract. Both returned `finish_reason=tool_calls`; neither ended for length.

## Optimized Request Shape

| Property | Value |
|---|---:|
| Messages | 3: 1 system + 2 user |
| Estimated input tokens | 9,565-9,571 |
| Provider-reported prompt tokens | 10,536-10,542 |
| Prompt content chars | 31,840 |
| Tool schemas | 7 |
| Tool schema chars | 9,118 |
| Serialized request chars | 42,073 |
| Tool choice | none |
| Thinking / reasoning effort | high / high |
| Max output tokens | 12,288 |

## Baseline vs Optimized Shape

The baseline is the earlier single valid `deepseek-v4-pro` provider diagnostic on the pre-V0-V2 request shape. Latency is shown only as context because one baseline and two optimized samples do not establish a latency distribution.

| Metric | Baseline | Optimized | Change |
|---|---:|---:|---:|
| Messages | 4 | 3 | -25.0% |
| Estimated input tokens | 11,704 | 9,568 midpoint | about -18.3% |
| Provider prompt tokens | 12,777 | 10,539 midpoint | about -17.5% |
| Tool schemas | 8 | 7 | -12.5% |
| Tool schema chars | 14,230 | 9,118 | -35.9% |
| Serialized request chars | 50,562 | 42,073 | -16.8% |
| Observed latency | 72,508 ms | 67,181 / 110,501 ms | inconclusive |

The input reduction is deterministic and reproduced in both optimized samples. Provider prompt usage differed by only six tokens between those samples, while completion usage differed by 3,862 tokens and latency differed by 43.320 seconds. This is evidence that request assembly became smaller, but the remaining latency variance is more closely associated with output/reasoning length than with input-size variance. It is not proof of a single causal mechanism.

## Contract Interpretation

- V0-V2 achieved the intended request-shape reduction without changing the system prompt, selected diff/file/structure context, thinking policy, or 12,288 exploration cap.
- The 90-second contract is still not reliable for this fixture: `1/2` valid optimized samples exceeded it.
- The diagnostic-only 180-second cap was useful: the 110.501-second response would otherwise have been recorded only as a timeout, despite completing normally with tool calls.
- These two samples are insufficient to justify a formal production timeout change. A future full Reviewer smoke may use an explicitly non-contract diagnostic cap while retaining `would_timeout_at_90s` as the readiness signal.

## Excluded Configuration Batch

An initial three-sample collection resolved to `deepseek-v4-flash` because `src/config.py` loads the repository `.env` with `override=True` at import time. Those samples completed in `96.879s-101.928s`, but are excluded from the pro-model result and all comparisons above. No target `deepseek-v4-pro` sample was consumed by the subsequent script-validation failure; it exited before provider dispatch.

## Conclusion

`REQUEST SHAPE IMPROVED; 90-SECOND LATENCY STABILITY NOT YET ACHIEVED.`

V0-V2 reduced provider prompt tokens by about 17.5% and serialized request size by about 16.8%. The optimized request can finish comfortably below 90 seconds, but can also require about 110 seconds when completion/reasoning usage grows. Do not claim a stable latency improvement from this sample. Do not start formal Graph A/B on the strength of these two first-request observations alone.
