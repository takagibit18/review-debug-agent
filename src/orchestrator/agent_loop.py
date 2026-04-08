"""Agent main loop — orchestrates the 5-phase cycle.

Each invocation drives a single review or debug session through:
1. Context preparation (load diff, related files)
2. Model analysis (LLM reasoning)
3. Tool execution (read files, run tests, grep, etc.)
4. Result processing (aggregate, format)
5. Continue / terminate decision
"""

# TODO: implement the 5-phase loop with ContextState integration
