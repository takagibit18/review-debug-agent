"""Stable public types for review context and graph-cache variants."""

from typing import Literal

ReviewContextMode = Literal["agent_search", "graph_hybrid"]
GraphCacheMode = Literal["disabled", "cold", "warm", "not_applicable"]
