"""Tool system: tool definitions, registry, and execution.

This package is owned by the Integration Agent role. All tools expose a
uniform JSON-Schema interface so the LLM can invoke them via tool-calling.

Tools are categorized by safety level:
- Read-only: safe for concurrent execution (e.g. read_file, glob_files, grep_files)
- Write: requires confirmation or serialization (e.g. apply_patch)
- Execute: sandboxed execution (e.g. run_test)
"""

from src.tools.base import ToolRegistry
from src.tools.file_read import FileReadTool
from src.tools.grep_tool import GrepTool
from src.tools.glob_tool import GlobTool


def create_default_registry() -> ToolRegistry:
    """Build the default tool registry for one orchestrator session."""
    registry = ToolRegistry()
    registry.register(FileReadTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    return registry
