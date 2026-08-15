# Provider Latency Diagnostic

> One measured attempt per case; no automatic retry, fallback model, timeout change, or full Reviewer A/B run.

## Environment / Runtime Contract

- Runtime base commit: `51c75685afcb89f3f20a4ea1f548a58cf898377a`
- Model / provider: `deepseek-v4-pro` / `deepseek`
- Endpoint type: `https://api.deepseek.com` (credentials and URL path omitted)
- Timeout: `90.0` seconds; default max output: `4096`; provider attempts per case: `1`
- Retry controls: diagnostic SDK retries `0`; production SDK default observed `2`; ModelClient outer attempts `1`
- ModelProfile / ProviderCompat: `{"api": "openai-completions", "compat": {"requires_assistant_content_for_tool_calls": true, "requires_reasoning_replay_for_tool_calls": true, "supports_reasoning_effort": true, "supports_tool_choice_with_thinking": false, "thinking_format": "deepseek"}, "model": "deepseek-v4-pro", "provider": "deepseek"}`

## Latency Matrix

| Case | Input Tokens | Tools | Thinking | Latency | Termination | PASS |
|---|---:|---:|---|---:|---|---|
| Plain | 16 | 0 | off | 1790 ms | completed | YES |
| Minimal Tool | 80 | 1 | high | 2920 ms | completed | YES |
| Reviewer pytest#9350 | 11704 | 8 | high | 72508 ms | completed | YES |

`response_latency_ms` is total non-streaming response latency, not TTFT.

## Request Shape Matrix

| Property | Plain | Minimal Tool | Reviewer |
|---|---:|---:|---:|
| message_count | 2 | 2 | 4 |
| system_message_count | 1 | 1 | 1 |
| user_message_count | 1 | 1 | 3 |
| assistant_message_count | 0 | 0 | 0 |
| tool_message_count | 0 | 0 | 0 |
| input_tokens_est | 16 | 80 | 11704 |
| prompt_chars | 79 | 86 | 35035 |
| tool_schema_count | 0 | 1 | 8 |
| tool_schema_chars | 0 | 249 | 14230 |
| thinking | off | high | high |
| tool_choice | none | none | none |
| reasoning replay | True | True | True |
| assistant content required | True | True | True |
| reasoning_effort | none | high | high |
| provider transforms | ['extra_body:thinking'] | ['extra_body:thinking', 'reasoning_effort=high'] | ['extra_body:thinking', 'reasoning_effort=high'] |
| max_output_tokens | 4096 | 4096 | 12288 |
| serialized_request_chars | 293 | 587 | 50562 |

## Observed Evidence

- The Reviewer request was `731.5x` the estimated input tokens of Plain, with `8` tool schemas / `14230` schema characters, and took `72508 ms`.
- Although the Core runtime default is `4096`, the exact first Reviewer exploration request resolved to `12288` max output tokens. This diagnostic records that existing behavior and does not change it.
- All three single attempts completed. This sample therefore cannot distinguish transient provider availability from Reviewer request-shape latency, and does not support diagnosing the 90-second policy as too strict.

## Case Details

### Plain

Started `2026-08-14T17:12:45.780701+00:00`; response latency / total request elapsed `1790 / 1790 ms`; termination `completed`; exception `none` / `none`; provider attempts `1`. Finish reason `stop`; response content / tool calls `23 chars / 0`; provider-reported prompt / completion tokens `23 / 9`.

### Minimal Tool

Started `2026-08-14T17:12:47.589772+00:00`; response latency / total request elapsed `2920 / 2920 ms`; termination `completed`; exception `none` / `none`; provider attempts `1`. Finish reason `tool_calls`; response content / tool calls `0 chars / 1`; provider-reported prompt / completion tokens `376 / 117`.

### Reviewer pytest#9350

Started `2026-08-14T17:12:52.860166+00:00`; response latency / total request elapsed `72508 / 72508 ms`; termination `completed`; exception `none` / `none`; provider attempts `1`. Finish reason `tool_calls`; response content / tool calls `124 chars / 3`; provider-reported prompt / completion tokens `12777 / 5624`. Reviewer run_id `a12d0b41-468a-4841-beb0-2d96442238f7`; request preparation `2174 ms`.

## Failure Attribution

`Failure Stage = none`

E. INCONCLUSIVE: all three single attempts completed; the prior provider timeout was not reproduced.

## Ready to rerun Reviewer/Runtime Smoke?

**YES**

Rerun the Reviewer/Runtime smoke once under the unchanged runtime contract; do not start formal Graph A/B yet.
