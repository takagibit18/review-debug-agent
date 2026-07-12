# Forced Tool Provider Compatibility Design

## Goal

Make every forced structured tool call compatible with providers that reject
`tool_choice` while thinking mode is enabled, without weakening verifier schema
enforcement or fail-closed behavior.

## Architecture

`ModelClient.chat` is the common boundary for all provider requests. Before the
payload is built, it derives an effective copy of `ModelConfig`. When
`tool_choice` is present, the client merges the provider-specific thinking
disable override into `extra_body`:

- DeepSeek model or base URL: `{"thinking": {"type": "disabled"}}`
- DashScope base URL or Qwen/GLM model: `{"enable_thinking": false}`
- Other providers: no change

Caller-owned configuration is never mutated. Existing unrelated `extra_body`
keys remain intact. An explicitly compatible disable value remains idempotent.

## Failure behavior

The compatibility layer does not retry with weaker output rules and does not
remove `tool_choice`. Provider failures still propagate through the existing
typed model errors; verifier enforcement therefore remains fail-closed.

## Verification

Unit tests reproduce forced verifier-style calls for DeepSeek and DashScope,
assert payload compatibility, preservation of extra fields, and no change for
ordinary calls. Existing model-client and verifier tests must pass. The local
smoke Eval must show a completed semantic verifier call rather than HTTP 400.
