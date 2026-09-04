"""Review context strategies for graph-free and graph-assisted review modes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from src.analyzer.code_graph import attach_changed_hunks, extract_changed_anchors
from src.analyzer.context_mode import GraphCacheMode, ReviewContextMode
from src.analyzer.context_planner import ChangeCenteredContextPlanner
from src.analyzer.event_log import EventType
from src.analyzer.language_resolver import UnavailableLspResolver
from src.analyzer.persistent_index import RelationGraphIndex
from src.analyzer.schemas import ReviewRequest
from src.config import Settings

EventRecorder = Callable[[EventType, str, dict[str, Any]], None]


class ReviewContext(BaseModel):
    """Mode-neutral context returned to the unified review pipeline."""

    context_mode: ReviewContextMode
    candidate_context_manifests: list[dict[str, Any]] = Field(default_factory=list)
    graph_telemetry: dict[str, Any] = Field(default_factory=dict)


class ContextStrategy(Protocol):
    """Prepare review context without changing the downstream review contract."""

    mode: ReviewContextMode

    async def prepare(self, request: ReviewRequest) -> ReviewContext: ...


class AgentSearchContextStrategy:
    """Graph-free context strategy backed by diff and safe read-only tools."""

    mode: ReviewContextMode = "agent_search"

    async def prepare(self, request: ReviewRequest) -> ReviewContext:
        del request
        return ReviewContext(
            context_mode=self.mode,
            candidate_context_manifests=[],
            graph_telemetry={
                "enabled": False,
                "status": "disabled",
                "graph_status": "disabled",
                "graph_cache_mode": "not_applicable",
                "build_latency_seconds": None,
                "incremental_update_latency_seconds": None,
                "parsed_file_count": None,
                "node_count": None,
                "edge_count": None,
                "manifest_count": 0,
                "manifest_token_cost": 0,
                "cache_hit": None,
                "cache_hit_rate": None,
                "fallback_reason": "",
                "available_graph_path_count": 0,
                "selected_reviewer_path_count": 0,
                "dropped_repeated_prefix_path_count": 0,
                "selected_direct_path_count": 0,
                "selected_production_path_count": 0,
                "selected_low_hop_path_count": 0,
                "required_production_path_count": 0,
                "missing_production_path_count": 0,
                "graph_reviewer_context_token_estimate": 0,
                "path_selection_reason_counts": {},
            },
        )


class GraphHybridContextStrategy:
    """Current change-centred relation graph and manifest implementation."""

    mode: ReviewContextMode = "graph_hybrid"

    def __init__(
        self,
        *,
        settings: Settings,
        workspace_root: Path | None,
        relation_graph_index_path: str | Path | None = None,
        record_event: EventRecorder | None = None,
    ) -> None:
        self._settings = settings
        self._workspace_root = workspace_root
        self._index_path_override = relation_graph_index_path
        self._record_event = record_event

    async def prepare(self, request: ReviewRequest) -> ReviewContext:
        if (
            not request.diff_mode
            or not (request.diff_text or "").strip()
            or self._workspace_root is None
        ):
            return ReviewContext(
                context_mode=self.mode,
                graph_telemetry={
                    "enabled": True,
                    "status": "not_applicable",
                    "graph_status": "not_applicable",
                    "graph_cache_mode": "not_applicable",
                    "manifest_count": 0,
                    "manifest_token_cost": 0,
                    "fallback_reason": "missing_diff_or_workspace",
                    "available_graph_path_count": 0,
                    "selected_reviewer_path_count": 0,
                    "dropped_repeated_prefix_path_count": 0,
                    "selected_direct_path_count": 0,
                    "selected_production_path_count": 0,
                    "selected_low_hop_path_count": 0,
                    "required_production_path_count": 0,
                    "missing_production_path_count": 0,
                    "graph_reviewer_context_token_estimate": 0,
                    "path_selection_reason_counts": {},
                },
            )

        index_path = Path(
            self._index_path_override or self._settings.relation_graph_index_path
        )
        if not index_path.is_absolute():
            index_path = self._workspace_root / index_path
        resolver = (
            UnavailableLspResolver()
            if self._settings.relation_graph_resolver_mode == "lsp"
            else None
        )
        try:
            index_result = RelationGraphIndex(
                self._workspace_root,
                persistence_enabled=self._settings.relation_graph_persistence_enabled,
                index_path=index_path,
                resolver_mode=self._settings.relation_graph_resolver_mode,
                language_resolver=resolver,
                max_files=self._settings.relation_graph_max_files,
                max_ambiguous_targets=(
                    self._settings.relation_graph_max_ambiguous_targets
                ),
            ).build()
            graph = index_result.graph
            anchors = extract_changed_anchors(request.diff_text or "", graph)
            attach_changed_hunks(graph, anchors)
            plan = ChangeCenteredContextPlanner(
                self._workspace_root,
                max_depth=self._settings.relation_graph_max_depth,
                max_nodes=self._settings.relation_graph_max_nodes,
                max_context_tokens=self._settings.relation_graph_max_context_tokens,
                min_evidence_confidence=(
                    self._settings.relation_graph_min_evidence_confidence
                ),
                max_paths_per_prefix=(
                    self._settings.relation_graph_max_paths_per_prefix
                ),
            ).plan(graph, anchors)
        except Exception as exc:  # noqa: BLE001
            self._emit(
                EventType.PIPELINE_FALLBACK,
                "relation_graph",
                {
                    "stage": "relation_graph_context",
                    "fallback": "agent_search_tool_context",
                    "error": exc.__class__.__name__,
                    "message": str(exc)[:300],
                },
            )
            return ReviewContext(
                context_mode=self.mode,
                graph_telemetry={
                    "enabled": True,
                    "status": "fallback_agent_search",
                    "graph_status": "failed",
                    "graph_cache_mode": "not_applicable",
                    "manifest_count": 0,
                    "manifest_token_cost": 0,
                    "fallback_reason": exc.__class__.__name__,
                    "available_graph_path_count": 0,
                    "selected_reviewer_path_count": 0,
                    "dropped_repeated_prefix_path_count": 0,
                    "selected_direct_path_count": 0,
                    "graph_reviewer_context_token_estimate": 0,
                    "path_selection_reason_counts": {},
                },
            )

        cache_mode: GraphCacheMode = "warm" if index_result.cache_hit else "cold"
        telemetry = {
            "enabled": True,
            "status": index_result.status,
            "graph_status": "ready",
            "graph_cache_mode": cache_mode,
            "repository_id": index_result.repository_id,
            "revision": index_result.revision,
            "cache_hit": index_result.cache_hit,
            "cache_hit_rate": index_result.cache_hit_rate,
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "changed_anchor_count": len(anchors),
            "manifest_count": len(plan.manifests),
            "manifest_token_cost": plan.total_token_cost,
            "context_token_cost": plan.total_token_cost,
            "included_graph_path_count": plan.total_included_paths,
            "discarded_graph_path_count": plan.total_discarded_paths,
            "available_graph_path_count": plan.available_graph_path_count,
            "selected_reviewer_path_count": plan.selected_reviewer_path_count,
            "dropped_repeated_prefix_path_count": (
                plan.dropped_repeated_prefix_path_count
            ),
            "selected_direct_path_count": plan.selected_direct_path_count,
            "selected_production_path_count": plan.selected_production_path_count,
            "selected_low_hop_path_count": plan.selected_low_hop_path_count,
            "required_production_path_count": plan.required_production_path_count,
            "missing_production_path_count": plan.missing_production_path_count,
            "graph_reviewer_context_token_estimate": (
                plan.graph_reviewer_context_token_estimate
            ),
            "path_selection_reason_counts": plan.path_selection_reason_counts,
            "parsed_file_count": index_result.parsed_file_count,
            "build_latency_seconds": index_result.build_latency_seconds,
            "incremental_update_latency_seconds": (
                index_result.incremental_update_latency_seconds
            ),
            "resolver_mode": self._settings.relation_graph_resolver_mode,
            "fallback_reason": str(index_result.fallback or ""),
            "ambiguous_resolution_truncation_count": graph.metadata.get(
                "ambiguous_resolution_truncation_count", 0
            ),
            "omitted_ambiguous_candidate_count": graph.metadata.get(
                "omitted_ambiguous_candidate_count", 0
            ),
            "skipped_weak_test_relation_count": graph.metadata.get(
                "skipped_weak_test_relation_count", 0
            ),
        }
        self._emit_graph_events(index_result, graph, anchors, plan)
        return ReviewContext(
            context_mode=self.mode,
            candidate_context_manifests=[
                manifest.model_dump(mode="json") for manifest in plan.manifests
            ],
            graph_telemetry=telemetry,
        )

    def _emit_graph_events(
        self, index_result: Any, graph: Any, anchors: Any, plan: Any
    ) -> None:
        self._emit(
            EventType.INDEX_LIFECYCLE,
            "relation_graph",
            {
                "status": index_result.status,
                "cache_hit": index_result.cache_hit,
                "cache_hit_rate": index_result.cache_hit_rate,
                "file_count": index_result.file_count,
                "changed_file_count": len(index_result.changed_files),
                "deleted_file_count": len(index_result.deleted_files),
                "affected_file_count": len(index_result.affected_files),
                "parsed_file_count": index_result.parsed_file_count,
                "fallback": index_result.fallback,
                "build_latency_seconds": index_result.build_latency_seconds,
                "incremental_update_latency_seconds": index_result.incremental_update_latency_seconds,
            },
        )
        self._emit(
            EventType.CHANGED_ANCHORS_EXTRACTED,
            "relation_graph",
            {
                "changed_anchor_count": len(anchors),
                "anchors": [anchor.model_dump(mode="json") for anchor in anchors],
            },
        )
        self._emit(
            EventType.RELATION_GRAPH_BUILT,
            "relation_graph",
            {
                "node_count": len(graph.nodes),
                "edge_count": len(graph.edges),
                "diagnostic_count": len(graph.diagnostics),
                "resolver_mode": self._settings.relation_graph_resolver_mode,
            },
        )
        self._emit(
            EventType.CONTEXT_PLAN_COMPLETED,
            "context_planner",
            {
                "manifest_count": len(plan.manifests),
                "token_cost": plan.total_token_cost,
                "included_node_count": plan.total_included_nodes,
                "included_path_count": plan.total_included_paths,
                "discarded_path_count": plan.total_discarded_paths,
                "available_graph_path_count": plan.available_graph_path_count,
                "selected_reviewer_path_count": plan.selected_reviewer_path_count,
                "dropped_repeated_prefix_path_count": (
                    plan.dropped_repeated_prefix_path_count
                ),
                "selected_direct_path_count": plan.selected_direct_path_count,
                "selected_production_path_count": plan.selected_production_path_count,
                "selected_low_hop_path_count": plan.selected_low_hop_path_count,
                "required_production_path_count": plan.required_production_path_count,
                "missing_production_path_count": plan.missing_production_path_count,
                "graph_reviewer_context_token_estimate": (
                    plan.graph_reviewer_context_token_estimate
                ),
                "path_selection_reason_counts": plan.path_selection_reason_counts,
            },
        )
        for manifest in plan.manifests:
            self._emit(
                EventType.CONTEXT_MANIFEST_CREATED,
                "context_planner",
                {
                    "candidate_id": manifest.candidate_id,
                    "token_cost": manifest.token_cost,
                    "included_span_count": len(manifest.included_spans),
                    "changed_anchor": {
                        "file": manifest.changed_anchor.file,
                        "line": manifest.changed_anchor.line,
                        "symbol_id": manifest.changed_anchor.symbol_id,
                    },
                    "included_spans": [
                        {
                            "file": span.file,
                            "start_line": span.start_line,
                            "end_line": span.end_line,
                            "symbol_id": span.symbol_id,
                            "role": span.role,
                            "retrieval_source": span.retrieval_source,
                            "truncated": span.truncated,
                        }
                        for span in manifest.included_spans
                    ],
                    "included_path_count": len(manifest.included_graph_paths),
                    "discarded_path_count": len(manifest.discarded_paths),
                    "available_graph_path_count": manifest.available_graph_path_count,
                    "selected_reviewer_path_count": (
                        manifest.selected_reviewer_path_count
                    ),
                    "dropped_repeated_prefix_path_count": (
                        manifest.dropped_repeated_prefix_path_count
                    ),
                    "selected_direct_path_count": manifest.selected_direct_path_count,
                    "selected_production_path_count": (
                        manifest.selected_production_path_count
                    ),
                    "selected_low_hop_path_count": manifest.selected_low_hop_path_count,
                    "required_production_path_count": (
                        manifest.required_production_path_count
                    ),
                    "missing_production_path_count": (
                        manifest.missing_production_path_count
                    ),
                    "graph_reviewer_context_token_estimate": (
                        manifest.graph_reviewer_context_token_estimate
                    ),
                    "path_selection_reason_counts": (
                        manifest.path_selection_reason_counts
                    ),
                    "truncation_reasons": manifest.truncation_reasons,
                },
            )

    def _emit(self, event_type: EventType, phase: str, payload: dict[str, Any]) -> None:
        if self._record_event is not None:
            self._record_event(event_type, phase, payload)


def build_context_strategy(
    mode: ReviewContextMode,
    *,
    settings: Settings,
    workspace_root: Path | None,
    relation_graph_index_path: str | Path | None = None,
    record_event: EventRecorder | None = None,
) -> ContextStrategy:
    """Create the explicitly selected context strategy."""

    if mode == "agent_search":
        return AgentSearchContextStrategy()
    return GraphHybridContextStrategy(
        settings=settings,
        workspace_root=workspace_root,
        relation_graph_index_path=relation_graph_index_path,
        record_event=record_event,
    )
