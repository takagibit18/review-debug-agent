"""Tool base class and registry.

Every concrete tool inherits from ``BaseTool`` and is automatically
registered via the ``ToolRegistry``.  The registry exposes the full
JSON-Schema catalogue to the inference engine for tool-calling.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ToolSafety(str, Enum):
    """Classifies a tool's side-effect level."""

    READONLY = "readonly"
    WRITE = "write"
    EXECUTE = "execute"


class ToolSpec(BaseModel):
    """Machine-readable tool specification sent to the LLM."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    safety: ToolSafety = ToolSafety.READONLY


class ToolResult(BaseModel):
    """Standardized serializable tool execution envelope."""

    ok: bool = Field(default=True)
    data: Any = Field(default=None)
    error: str | None = Field(default=None)


class BaseTool(ABC):
    """Abstract base for all agent tools."""

    @abstractmethod
    def spec(self) -> ToolSpec:
        """Return the JSON-Schema-style specification for this tool."""

    @abstractmethod
    async def execute(self, **kwargs: Any) -> Any:
        """Run the tool with validated parameters and return a result."""

    def is_enabled(self) -> bool:
        """Whether this tool is available in the current environment."""
        return True

    def is_concurrency_safe(self) -> bool:
        """Whether this tool can safely run in parallel with others."""
        return self.spec().safety == ToolSafety.READONLY


class ToolRegistry:
    """Singleton registry that collects all available tools."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Add a tool to the registry."""
        self._tools[tool.spec().name] = tool

    def get(self, name: str) -> BaseTool | None:
        """Look up a tool by name."""
        return self._tools.get(name)

    def list_specs(self) -> list[ToolSpec]:
        """Return specs for all enabled tools."""
        return [t.spec() for t in self._tools.values() if t.is_enabled()]
