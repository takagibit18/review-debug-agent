"""Optional language/LSP enrichment abstraction for relation graphs.

No language server is a hard dependency.  Callers may supply an adapter with an
``enrich`` method; failures are recorded and the AST graph remains usable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Protocol

from pydantic import BaseModel, Field

from src.analyzer.code_graph import CodeRelationGraph, ConfidenceTier, RelationEdge


ResolverMode = Literal["ast", "resolver", "lsp"]


class ResolverDiagnostic(BaseModel):
    resolver: str
    path: str = ""
    fallback: str = "ast"
    error: str = ""
    detail: str = ""


class EnrichedRelation(BaseModel):
    """Provider-neutral relation returned by an optional resolver adapter."""

    source_symbol_id: str
    target_symbol_id: str
    kind: str
    path: str
    line: int = Field(ge=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    resolver: str
    fallback: str = "ast"


class LanguageGraphResolver(Protocol):
    name: str

    def enrich(
        self,
        repo_root: Path,
        graph: CodeRelationGraph,
        paths: list[Path],
    ) -> CodeRelationGraph:
        """Return a graph enriched with resolver-backed bindings."""


class CallableLanguageResolver:
    """Small adapter for a project-owned language or LSP client callback."""

    def __init__(
        self,
        name: str,
        callback: Callable[
            [Path, CodeRelationGraph, list[Path]], list[EnrichedRelation]
        ],
    ) -> None:
        self.name = name
        self._callback = callback

    def enrich(
        self,
        repo_root: Path,
        graph: CodeRelationGraph,
        paths: list[Path],
    ) -> CodeRelationGraph:
        enriched = graph.model_copy(deep=True)
        try:
            relations = self._callback(repo_root, enriched, paths)
        except Exception as exc:  # noqa: BLE001
            enriched.diagnostics.append(
                ResolverDiagnostic(
                    resolver=self.name,
                    fallback="ast",
                    error=f"{exc.__class__.__name__}: {exc}",
                ).model_dump(mode="json")
            )
            return enriched
        for relation in relations:
            source = enriched.nodes.get(relation.source_symbol_id)
            target = enriched.nodes.get(relation.target_symbol_id)
            if source is None or target is None:
                enriched.diagnostics.append(
                    ResolverDiagnostic(
                        resolver=relation.resolver,
                        path=relation.path,
                        fallback=relation.fallback,
                        error="enriched_relation_symbol_missing",
                        detail=relation.reason,
                    ).model_dump(mode="json")
                )
                continue
            try:
                candidate = RelationEdge.model_validate(
                    {
                        "source": source.node_id,
                        "target": target.node_id,
                        "kind": relation.kind,
                        "path": relation.path,
                        "line": relation.line,
                        "resolver": relation.resolver,
                        "confidence": relation.confidence,
                        "confidence_tier": ConfidenceTier.RESOLVED,
                        "evidence_eligibility": (
                            "strong" if relation.confidence >= 0.65 else "exploratory"
                        ),
                        "reason": relation.reason,
                        "metadata": {"fallback": relation.fallback, "enriched": True},
                    }
                )
            except Exception as exc:  # noqa: BLE001
                enriched.diagnostics.append(
                    ResolverDiagnostic(
                        resolver=relation.resolver,
                        path=relation.path,
                        fallback=relation.fallback,
                        error=f"invalid_enriched_relation:{exc}",
                    ).model_dump(mode="json")
                )
                continue
            enriched.add_edge(candidate)
        return enriched


class UnavailableLspResolver:
    """Explicit optional-LSP adapter that documents fallback without pretending precision."""

    name = "lsp_unavailable"

    def enrich(
        self,
        repo_root: Path,
        graph: CodeRelationGraph,
        paths: list[Path],
    ) -> CodeRelationGraph:
        enriched = graph.model_copy(deep=True)
        enriched.diagnostics.append(
            ResolverDiagnostic(
                resolver=self.name,
                fallback="ast",
                error="lsp_not_configured",
                detail="AST-only output retained; no precise LSP binding was claimed.",
            ).model_dump(mode="json")
        )
        return enriched
