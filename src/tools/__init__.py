"""Tool system — tool definitions, registry, and execution.

This package is owned by the *Integration Agent* role.  All tools expose a
uniform JSON-Schema interface so the LLM can invoke them via tool-calling.

Tools are categorised by safety level:
- Read-only: safe for concurrent execution (e.g. read_file, grep)
- Write: requires confirmation / serialisation (e.g. apply_patch)
- Execute: sandboxed execution (e.g. run_test)
"""
