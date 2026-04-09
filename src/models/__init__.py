"""Model layer — LLM provider abstraction.

Wraps the OpenAI-compatible API (and potentially other providers) behind a
unified async interface so the rest of the codebase is provider-agnostic.
"""
