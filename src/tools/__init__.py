"""Tool system: tool definitions, registry, and execution.

This package is owned by the Integration Agent role. All tools expose a
uniform JSON-Schema interface so the LLM can invoke them via tool-calling.

Tools are categorized by safety level:
- Read-only: safe for concurrent execution (e.g. read_file, glob_files, grep_files, list_dir)
- Write: requires confirmation or serialization (e.g. apply_patch)
- Execute: sandboxed execution (e.g. run_command, run_tests) — only exposed in debug mode
"""

from src.config import get_settings
from src.tools.base import ToolRegistry
from src.tools.changed_context_tool import GetChangedContextTool
from src.tools.file_read import FileReadTool
from src.tools.grep_tool import GrepTool
from src.tools.glob_tool import GlobTool
from src.tools.list_dir_tool import ListDirTool
from src.tools.review_context import ReviewToolContext
from src.tools.review_draft_validator_tool import ValidateReviewDraftTool
from src.tools.run_command_tool import RunCommandTool
from src.tools.run_tests_tool import RunTestsTool
from src.tools.symbol_context_tool import FindSymbolContextTool


def create_default_registry(
    *,
    include_execute: bool = False,
    review_context: ReviewToolContext | None = None,
) -> ToolRegistry:
    """Build the default tool registry for one orchestrator session.

    ``include_execute`` controls whether execute-class tools are exposed to
    the model. Review mode passes ``False`` (read-only view); debug mode
    passes ``True``. The ``EXECUTE_ENABLED`` setting acts as a global kill
    switch: if disabled, execute tools are never registered.
    """
    registry = ToolRegistry()
    registry.register(FileReadTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(ListDirTool())
    if review_context is not None:
        registry.register(GetChangedContextTool(review_context))
        registry.register(FindSymbolContextTool(review_context))
        if not include_execute:
            registry.register(ValidateReviewDraftTool(review_context))
    if include_execute and get_settings().execute_enabled:
        registry.register(RunCommandTool())
        registry.register(RunTestsTool())
    return registry
