"""Budgeted, change-centred context selection over the code relation graph."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

from src.analyzer.code_graph import (
    ChangedAnchor,
    CodeNode,
    CodeRelationGraph,
    EdgeKind,
    NodeKind,
    RelationEdge,
    iter_execution_paths,
)
from src.analyzer.finding_schema import context_hash, normalize_repo_path
from src.analyzer.reviewer_projection import project_path_for_reviewer
from src.models.token_telemetry import estimate_tokens, serialize_json


DEFAULT_EDGE_WEIGHTS: dict[EdgeKind, float] = {
    EdgeKind.ENCLOSED_BY: 1.0,
    EdgeKind.CONTAINS: 0.9,
    EdgeKind.CALLS: 0.88,
    EdgeKind.CALLED_BY: 0.92,
    EdgeKind.IMPORTS: 0.45,
    EdgeKind.REFERENCES: 0.55,
    EdgeKind.READS_FIELD: 1.0,
    EdgeKind.WRITES_FIELD: 1.0,
    EdgeKind.TESTED_BY: 0.9,
    EdgeKind.INHERITS: 0.9,
    EdgeKind.IMPLEMENTS: 0.95,
}


class IncludedSpan(BaseModel):
    span_id: str
    file: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol_id: str = ""
    role: str
    content: str
    context_hash: str
    retrieval_source: str
    forced: bool = False
    truncated: bool = False
    token_cost: int = Field(default=0, ge=0)


class ManifestGraphEdge(BaseModel):
    edge_id: str
    source: str
    target: str
    kind: str
    path: str
    line: int = Field(ge=1)
    resolver: str
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_tier: str
    evidence_eligibility: str
    reason: str
    derived_from_edge: str = ""


class IncludedGraphPath(BaseModel):
    path_id: str
    node_ids: list[str]
    edges: list[ManifestGraphEdge]
    score: float = Field(ge=0.0)
    semantic_role: str
    evidence_eligibility: str
    explanation: str


class ExcludedGraphPath(BaseModel):
    path_id: str
    node_ids: list[str]
    edge_kinds: list[str]
    reason: str
    max_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    first_hop_prefix: str = ""


class CandidateContextManifest(BaseModel):
    """Audit record for exactly the context supplied to one reviewer candidate."""

    candidate_id: str
    changed_anchor: ChangedAnchor
    included_spans: list[IncludedSpan] = Field(default_factory=list)
    included_graph_paths: list[IncludedGraphPath] = Field(default_factory=list)
    excluded_low_confidence_paths: list[ExcludedGraphPath] = Field(default_factory=list)
    discarded_paths: list[ExcludedGraphPath] = Field(default_factory=list)
    token_cost: int = Field(default=0, ge=0)
    char_cost: int = Field(default=0, ge=0)
    included_node_count: int = Field(default=0, ge=0)
    max_depth: int = Field(default=0, ge=0)
    truncation_reasons: list[str] = Field(default_factory=list)
    parent_manifest_ids: list[str] = Field(default_factory=list)
    retrieval_provenance: list[dict[str, Any]] = Field(default_factory=list)
    available_graph_path_count: int = Field(default=0, ge=0)
    selected_reviewer_path_count: int = Field(default=0, ge=0)
    dropped_repeated_prefix_path_count: int = Field(default=0, ge=0)
    selected_direct_path_count: int = Field(default=0, ge=0)
    selected_production_path_count: int = Field(default=0, ge=0)
    selected_low_hop_path_count: int = Field(default=0, ge=0)
    required_production_path_count: int = Field(default=0, ge=0)
    missing_production_path_count: int = Field(default=0, ge=0)
    graph_reviewer_context_token_estimate: int = Field(default=0, ge=0)
    path_selection_reason_counts: dict[str, int] = Field(default_factory=dict)

    def prompt_payload(self) -> dict[str, Any]:
        """Return the evidence envelope eligible for the reviewer prompt."""

        from src.analyzer.reviewer_projection import project_manifest_for_reviewer

        return project_manifest_for_reviewer(self.model_dump(mode="json"))

    def contains_location(
        self, file: str, line: int, end_line: int | None = None
    ) -> bool:
        normalized = normalize_repo_path(file)
        end = end_line or line
        return any(
            span.file == normalized and line <= span.end_line and span.start_line <= end
            for span in self.included_spans
        )

    def evidence_edge(self, edge_id: str) -> ManifestGraphEdge | None:
        for path in self.included_graph_paths:
            for edge in path.edges:
                if edge.edge_id == edge_id:
                    return edge
        return None


class ContextPlanResult(BaseModel):
    manifests: list[CandidateContextManifest] = Field(default_factory=list)
    total_token_cost: int = Field(default=0, ge=0)
    total_included_nodes: int = Field(default=0, ge=0)
    total_included_paths: int = Field(default=0, ge=0)
    total_discarded_paths: int = Field(default=0, ge=0)
    available_graph_path_count: int = Field(default=0, ge=0)
    selected_reviewer_path_count: int = Field(default=0, ge=0)
    dropped_repeated_prefix_path_count: int = Field(default=0, ge=0)
    selected_direct_path_count: int = Field(default=0, ge=0)
    selected_production_path_count: int = Field(default=0, ge=0)
    selected_low_hop_path_count: int = Field(default=0, ge=0)
    required_production_path_count: int = Field(default=0, ge=0)
    missing_production_path_count: int = Field(default=0, ge=0)
    graph_reviewer_context_token_estimate: int = Field(default=0, ge=0)
    path_selection_reason_counts: dict[str, int] = Field(default_factory=dict)


class ChangeCenteredContextPlanner:
    """Select evidence-rich graph paths instead of dumping all neighbours."""

    def __init__(
        self,
        repo_root: Path | str,
        *,
        max_depth: int = 2,
        max_nodes: int = 40,
        max_context_tokens: int = 4_000,
        max_context_chars: int | None = None,
        min_evidence_confidence: float = 0.65,
        max_paths_per_prefix: int = 2,
        edge_weights: dict[EdgeKind, float] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.max_depth = max(0, max_depth)
        self.max_nodes = max(1, max_nodes)
        self.max_context_tokens = max(1, max_context_tokens)
        self.max_context_chars = max_context_chars or max_context_tokens * 4
        self.min_evidence_confidence = min(1.0, max(0.0, min_evidence_confidence))
        self.max_paths_per_prefix = max(1, max_paths_per_prefix)
        self.edge_weights = {**DEFAULT_EDGE_WEIGHTS, **(edge_weights or {})}

    def plan(
        self,
        graph: CodeRelationGraph,
        anchors: list[ChangedAnchor],
    ) -> ContextPlanResult:
        manifests = [
            self.plan_candidate(graph, anchor, index)
            for index, anchor in enumerate(anchors, start=1)
        ]
        reason_counts: dict[str, int] = {}
        for manifest in manifests:
            for reason, count in manifest.path_selection_reason_counts.items():
                reason_counts[reason] = reason_counts.get(reason, 0) + count
        return ContextPlanResult(
            manifests=manifests,
            total_token_cost=sum(item.token_cost for item in manifests),
            total_included_nodes=sum(item.included_node_count for item in manifests),
            total_included_paths=sum(
                len(item.included_graph_paths) for item in manifests
            ),
            total_discarded_paths=sum(
                len(item.discarded_paths) + len(item.excluded_low_confidence_paths)
                for item in manifests
            ),
            available_graph_path_count=sum(
                item.available_graph_path_count for item in manifests
            ),
            selected_reviewer_path_count=sum(
                item.selected_reviewer_path_count for item in manifests
            ),
            dropped_repeated_prefix_path_count=sum(
                item.dropped_repeated_prefix_path_count for item in manifests
            ),
            selected_direct_path_count=sum(
                item.selected_direct_path_count for item in manifests
            ),
            selected_production_path_count=sum(
                item.selected_production_path_count for item in manifests
            ),
            selected_low_hop_path_count=sum(
                item.selected_low_hop_path_count for item in manifests
            ),
            required_production_path_count=sum(
                item.required_production_path_count for item in manifests
            ),
            missing_production_path_count=sum(
                item.missing_production_path_count for item in manifests
            ),
            graph_reviewer_context_token_estimate=sum(
                item.graph_reviewer_context_token_estimate for item in manifests
            ),
            path_selection_reason_counts=reason_counts,
        )

    def plan_candidate(
        self,
        graph: CodeRelationGraph,
        anchor: ChangedAnchor,
        sequence: int = 1,
    ) -> CandidateContextManifest:
        manifest = CandidateContextManifest(
            candidate_id=f"C-{sequence:03d}-{anchor.anchor_id[2:8]}",
            changed_anchor=anchor,
            max_depth=self.max_depth,
        )
        selected_nodes: set[str] = set()
        used_spans: set[tuple[str, int, int, str]] = set()

        hunk_span = self._hunk_span(anchor)
        self._force_span(manifest, hunk_span, used_spans, reason="changed_hunk")

        start = graph.nodes.get(anchor.symbol_id) if anchor.symbol_id else None
        if start is None:
            start = graph.symbol_for_line(anchor.file, anchor.line)
        if start is not None:
            selected_nodes.add(start.node_id)
            signature = self._node_span(
                start, role="symbol_signature", signature_only=True
            )
            self._force_span(manifest, signature, used_spans, reason="symbol_signature")
            enclosing = self._node_span(start, role="enclosing_symbol")
            enclosing = self._fit_required_symbol(enclosing, anchor, manifest)
            self._force_span(manifest, enclosing, used_spans, reason="enclosing_symbol")
            self._include_class_context(
                graph, start, manifest, selected_nodes, used_spans
            )
            direct_path_ids = self._include_direct_fields(
                graph, start, manifest, selected_nodes, used_spans
            )
        else:
            direct_path_ids = set()

        if start is not None and self.max_depth > 0:
            paths: list[list[RelationEdge]] = []
            if start.kind == NodeKind.FIELD:
                paths.extend(
                    [edge]
                    for edge in graph.incoming(
                        start.node_id,
                        {EdgeKind.READS_FIELD, EdgeKind.WRITES_FIELD},
                    )
                )
            paths.extend(
                iter_execution_paths(
                    graph,
                    start.node_id,
                    max_depth=self.max_depth,
                    max_paths=max(50, self.max_nodes * 4),
                )
            )
            unique_paths: list[list[RelationEdge]] = []
            seen_path_ids = set(direct_path_ids)
            for path in paths:
                path_id = self._path_id(path)
                if path_id in seen_path_ids:
                    continue
                seen_path_ids.add(path_id)
                unique_paths.append(path)
            manifest.available_graph_path_count = len(seen_path_ids)
            scored = sorted(
                ((self._path_score(anchor, path), path) for path in unique_paths),
                key=lambda item: (
                    self._path_role_priority(anchor, item[1]),
                    0 if len(item[1]) == 1 else 1,
                    -item[0],
                    self._path_id(item[1]),
                ),
            )
            prefix_counts: dict[str, int] = {}
            required_production_paths = [
                item
                for item in sorted(
                    ((score, path) for score, path in scored),
                    key=lambda item: (-item[0], len(item[1]), self._path_id(item[1])),
                )
                if len(item[1]) >= 2
                and self._path_role_priority(anchor, item[1]) == 0
            ]
            manifest.required_production_path_count = min(
                1, len(required_production_paths)
            )
            required_path_ids: set[str] = set()
            if required_production_paths:
                score, path = required_production_paths[0]
                required_path_ids.add(self._path_id(path))
                self._consider_path(
                    graph,
                    anchor,
                    manifest,
                    path,
                    score,
                    selected_nodes,
                    used_spans,
                    prefix_counts,
                )
            for score, path in scored:
                if self._path_id(path) in required_path_ids:
                    continue
                self._consider_path(
                    graph,
                    anchor,
                    manifest,
                    path,
                    score,
                    selected_nodes,
                    used_spans,
                    prefix_counts,
                )
        else:
            manifest.available_graph_path_count = len(direct_path_ids)

        manifest.included_node_count = len(selected_nodes)
        manifest.char_cost = sum(len(span.content) for span in manifest.included_spans)
        manifest.token_cost = sum(span.token_cost for span in manifest.included_spans)
        manifest.selected_reviewer_path_count = len(manifest.included_graph_paths)
        manifest.selected_direct_path_count = sum(
            len(path.edges) == 1 for path in manifest.included_graph_paths
        )
        manifest.selected_production_path_count = sum(
            self._is_production_path(path)
            for path in manifest.included_graph_paths
        )
        manifest.selected_low_hop_path_count = sum(
            len(path.edges) == 2 for path in manifest.included_graph_paths
        )
        manifest.missing_production_path_count = max(
            0,
            manifest.required_production_path_count
            - manifest.selected_production_path_count,
        )
        manifest.dropped_repeated_prefix_path_count = manifest.path_selection_reason_counts.get(
            "repeated_first_hop_prefix", 0
        )
        manifest.graph_reviewer_context_token_estimate = sum(
            self._estimate_tokens(
                serialize_json(
                    project_path_for_reviewer(path.model_dump(mode="json"))
                )
            )
            for path in manifest.included_graph_paths
        )
        if manifest.token_cost > self.max_context_tokens:
            manifest.truncation_reasons.append("forced_context_exceeds_token_budget")
        if manifest.char_cost > self.max_context_chars:
            manifest.truncation_reasons.append("forced_context_exceeds_char_budget")
        return manifest

    def _consider_path(
        self,
        graph: CodeRelationGraph,
        anchor: ChangedAnchor,
        manifest: CandidateContextManifest,
        path: list[RelationEdge],
        score: float,
        selected_nodes: set[str],
        used_spans: set[tuple[str, int, int, str]],
        prefix_counts: dict[str, int],
    ) -> None:
        path_id = self._path_id(path)
        node_ids = [path[0].source, *[edge.target for edge in path]]
        first_hop_prefix = self._path_prefix_key(path)
        confidence = min((edge.confidence for edge in path), default=0.0)
        eligibility = (
            "strong"
            if all(edge.evidence_eligibility == "strong" for edge in path)
            and confidence >= self.min_evidence_confidence
            else "exploratory"
        )
        if eligibility != "strong":
            manifest.excluded_low_confidence_paths.append(
                ExcludedGraphPath(
                    path_id=path_id,
                    node_ids=node_ids,
                    edge_kinds=[edge.kind.value for edge in path],
                    reason="low_confidence_or_exploratory_edge",
                    max_confidence=max((edge.confidence for edge in path), default=0.0),
                    first_hop_prefix=first_hop_prefix,
                )
            )
            self._record_path_selection_reason(
                manifest, "low_confidence_or_exploratory_edge"
            )
            return

        is_direct = len(path) == 1
        if not is_direct and prefix_counts.get(first_hop_prefix, 0) >= (
            self.max_paths_per_prefix
        ):
            manifest.discarded_paths.append(
                ExcludedGraphPath(
                    path_id=path_id,
                    node_ids=node_ids,
                    edge_kinds=[edge.kind.value for edge in path],
                    reason="repeated_first_hop_prefix",
                    max_confidence=max(edge.confidence for edge in path),
                    first_hop_prefix=first_hop_prefix,
                )
            )
            self._record_path_selection_reason(
                manifest, "repeated_first_hop_prefix"
            )
            return
        new_nodes = [node_id for node_id in node_ids if node_id not in selected_nodes]
        if len(selected_nodes) + len(new_nodes) > self.max_nodes:
            manifest.discarded_paths.append(
                ExcludedGraphPath(
                    path_id=path_id,
                    node_ids=node_ids,
                    edge_kinds=[edge.kind.value for edge in path],
                    reason="max_nodes",
                    max_confidence=max(edge.confidence for edge in path),
                    first_hop_prefix=first_hop_prefix,
                )
            )
            self._append_once(manifest.truncation_reasons, "max_nodes")
            self._record_path_selection_reason(manifest, "max_nodes")
            return

        candidate_spans: list[IncludedSpan] = []
        # A multi-hop production path is primarily a navigation contract: it
        # tells the reviewer which runtime symbols and edges must be followed.
        # Including every body on that path makes the planner spend its small
        # source-span budget before the path itself can reach the reviewer.
        # Keep endpoint signatures for multi-hop production paths; the normal
        # file/symbol tools can hydrate the full bodies when the path is used.
        compact_path_evidence = (
            len(path) >= 2 and self._path_role_priority(anchor, path) == 0
        )
        for node_id in new_nodes:
            node = graph.nodes.get(node_id)
            if node is None or node.kind in {NodeKind.FILE, NodeKind.CHANGED_HUNK}:
                continue
            candidate_spans.append(
                self._node_span(
                    node,
                    role=self._semantic_role(anchor, path),
                    signature_only=compact_path_evidence,
                )
            )
        additional_tokens = sum(
            span.token_cost
            for span in candidate_spans
            if self._span_key(span) not in used_spans
        )
        additional_chars = sum(
            len(span.content)
            for span in candidate_spans
            if self._span_key(span) not in used_spans
        )
        current_tokens = sum(span.token_cost for span in manifest.included_spans)
        current_chars = sum(len(span.content) for span in manifest.included_spans)
        if current_tokens + additional_tokens > self.max_context_tokens:
            manifest.discarded_paths.append(
                ExcludedGraphPath(
                    path_id=path_id,
                    node_ids=node_ids,
                    edge_kinds=[edge.kind.value for edge in path],
                    reason="token_budget",
                    max_confidence=max(edge.confidence for edge in path),
                    first_hop_prefix=first_hop_prefix,
                )
            )
            self._append_once(manifest.truncation_reasons, "token_budget")
            self._record_path_selection_reason(manifest, "token_budget")
            return
        if current_chars + additional_chars > self.max_context_chars:
            manifest.discarded_paths.append(
                ExcludedGraphPath(
                    path_id=path_id,
                    node_ids=node_ids,
                    edge_kinds=[edge.kind.value for edge in path],
                    reason="character_budget",
                    max_confidence=max(edge.confidence for edge in path),
                    first_hop_prefix=first_hop_prefix,
                )
            )
            self._append_once(manifest.truncation_reasons, "character_budget")
            self._record_path_selection_reason(manifest, "character_budget")
            return

        selected_nodes.update(node_ids)
        for span in candidate_spans:
            self._append_span(manifest, span, used_spans)
        manifest.included_graph_paths.append(
            IncludedGraphPath(
                path_id=path_id,
                node_ids=node_ids,
                edges=[self._manifest_edge(edge) for edge in path],
                score=score,
                semantic_role=self._semantic_role(anchor, path),
                evidence_eligibility=eligibility,
                explanation=self._path_explanation(anchor, path, score),
            )
        )
        if not is_direct:
            prefix_counts[first_hop_prefix] = prefix_counts.get(first_hop_prefix, 0) + 1
        self._record_path_selection_reason(
            manifest,
            "selected_direct" if is_direct else "selected_low_hop" if len(path) == 2 else "selected",
        )

    def _include_class_context(
        self,
        graph: CodeRelationGraph,
        start: CodeNode,
        manifest: CandidateContextManifest,
        selected_nodes: set[str],
        used_spans: set[tuple[str, int, int, str]],
    ) -> None:
        current = start
        while True:
            parent_edges = graph.outgoing(current.node_id, {EdgeKind.ENCLOSED_BY})
            if not parent_edges:
                return
            parent = graph.nodes.get(parent_edges[0].target)
            if parent is None:
                return
            if parent.kind == NodeKind.CLASS:
                selected_nodes.add(parent.node_id)
                self._append_if_budget(
                    manifest,
                    self._node_span(parent, role="class_context", signature_only=True),
                    used_spans,
                    "class_context_budget",
                )
                return
            if parent.kind == NodeKind.FILE:
                return
            current = parent

    def _include_direct_fields(
        self,
        graph: CodeRelationGraph,
        start: CodeNode,
        manifest: CandidateContextManifest,
        selected_nodes: set[str],
        used_spans: set[tuple[str, int, int, str]],
    ) -> set[str]:
        direct_path_ids: set[str] = set()
        field_edges = graph.outgoing(
            start.node_id, {EdgeKind.READS_FIELD, EdgeKind.WRITES_FIELD}
        )
        for edge in sorted(
            field_edges,
            key=lambda value: (
                0 if value.kind == EdgeKind.WRITES_FIELD else 1,
                -value.confidence,
                value.target,
            ),
        ):
            target = graph.nodes.get(edge.target)
            if target is None or target.kind != NodeKind.FIELD:
                continue
            path_id = self._path_id([edge])
            direct_path_ids.add(path_id)
            if edge.confidence < self.min_evidence_confidence:
                manifest.excluded_low_confidence_paths.append(
                    ExcludedGraphPath(
                        path_id=path_id,
                        node_ids=[edge.source, edge.target],
                        edge_kinds=[edge.kind.value],
                        reason="low_confidence_or_exploratory_edge",
                        max_confidence=edge.confidence,
                        first_hop_prefix=self._path_prefix_key([edge]),
                    )
                )
                self._record_path_selection_reason(
                    manifest, "low_confidence_or_exploratory_edge"
                )
                continue
            selected_nodes.add(target.node_id)
            self._append_if_budget(
                manifest,
                self._node_span(target, role="field_definition"),
                used_spans,
                "field_context_budget",
            )
            if not any(
                item.path_id == path_id for item in manifest.included_graph_paths
            ):
                manifest.included_graph_paths.append(
                    IncludedGraphPath(
                        path_id=path_id,
                        node_ids=[edge.source, edge.target],
                        edges=[self._manifest_edge(edge)],
                        score=self._path_score(manifest.changed_anchor, [edge]),
                        semantic_role="field_state",
                        evidence_eligibility=edge.evidence_eligibility,
                        explanation=(
                            "Direct field read/write required for state-change review."
                        ),
                    )
                )
                self._record_path_selection_reason(manifest, "selected_direct")
        return direct_path_ids

    def _fit_required_symbol(
        self,
        span: IncludedSpan,
        anchor: ChangedAnchor,
        manifest: CandidateContextManifest,
    ) -> IncludedSpan:
        remaining_tokens = max(
            0,
            self.max_context_tokens
            - sum(item.token_cost for item in manifest.included_spans),
        )
        remaining_chars = max(
            0,
            self.max_context_chars
            - sum(len(item.content) for item in manifest.included_spans),
        )
        if span.token_cost <= remaining_tokens and len(span.content) <= remaining_chars:
            return span
        try:
            lines = (
                (self.repo_root / span.file)
                .read_text(encoding="utf-8", errors="ignore")
                .splitlines()
            )
        except OSError:
            return span
        radius = max(2, min(20, remaining_chars // 160))
        start = max(span.start_line, anchor.line - radius)
        end = min(span.end_line, anchor.end_line + radius)
        content_lines = lines[start - 1 : end]
        content = "\n".join(
            f"{line_number}: {text}"
            for line_number, text in _line_pairs(start, content_lines)
        )
        clipped = span.model_copy(
            update={
                "start_line": start,
                "end_line": end,
                "content": content,
                "context_hash": context_hash(content),
                "truncated": True,
                "token_cost": self._estimate_tokens(content),
            }
        )
        self._append_once(manifest.truncation_reasons, "enclosing_symbol_clipped")
        return clipped

    def _hunk_span(self, anchor: ChangedAnchor) -> IncludedSpan:
        content = anchor.hunk_text
        return IncludedSpan(
            span_id="span-"
            + hashlib.sha256(
                f"{anchor.file}|{anchor.line}|{anchor.end_line}|hunk".encode("utf-8")
            ).hexdigest()[:16],
            file=anchor.file,
            start_line=anchor.line,
            end_line=max(anchor.line, anchor.end_line),
            symbol_id=anchor.symbol_id,
            role="changed_hunk",
            content=content,
            context_hash=context_hash(content),
            retrieval_source="git_diff",
            forced=True,
            token_cost=self._estimate_tokens(content),
        )

    def _node_span(
        self,
        node: CodeNode,
        *,
        role: str,
        signature_only: bool = False,
    ) -> IncludedSpan:
        start = node.start_line
        end = node.start_line if signature_only else node.end_line
        content = self._read_numbered_span(node.path, start, end)
        if not content:
            content = node.signature
        raw = f"{node.path}|{start}|{end}|{role}|{node.symbol_id}"
        return IncludedSpan(
            span_id="span-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
            file=node.path,
            start_line=start,
            end_line=max(start, end),
            symbol_id=node.symbol_id,
            role=role,
            content=content,
            context_hash=context_hash(content),
            retrieval_source="relation_graph",
            token_cost=self._estimate_tokens(content),
        )

    def _force_span(
        self,
        manifest: CandidateContextManifest,
        span: IncludedSpan,
        used_spans: set[tuple[str, int, int, str]],
        *,
        reason: str,
    ) -> None:
        span.forced = True
        self._append_span(manifest, span, used_spans)
        if (
            sum(item.token_cost for item in manifest.included_spans)
            > self.max_context_tokens
        ):
            self._append_once(
                manifest.truncation_reasons, f"forced:{reason}:token_budget_override"
            )

    def _append_if_budget(
        self,
        manifest: CandidateContextManifest,
        span: IncludedSpan,
        used_spans: set[tuple[str, int, int, str]],
        reason: str,
    ) -> bool:
        if self._span_key(span) in used_spans:
            return True
        tokens = sum(item.token_cost for item in manifest.included_spans)
        chars = sum(len(item.content) for item in manifest.included_spans)
        if (
            tokens + span.token_cost > self.max_context_tokens
            or chars + len(span.content) > self.max_context_chars
        ):
            self._append_once(manifest.truncation_reasons, reason)
            return False
        self._append_span(manifest, span, used_spans)
        return True

    @staticmethod
    def _append_span(
        manifest: CandidateContextManifest,
        span: IncludedSpan,
        used_spans: set[tuple[str, int, int, str]],
    ) -> None:
        key = ChangeCenteredContextPlanner._span_key(span)
        if key in used_spans:
            return
        used_spans.add(key)
        manifest.included_spans.append(span)

    def _path_score(self, anchor: ChangedAnchor, path: list[RelationEdge]) -> float:
        if not path:
            return 0.0
        role_weight = self._role_weight(anchor.change_kind, path)
        edge_weight = 1.0
        edge_confidence = 1.0
        evidence_value = 1.0
        for edge in path:
            edge_weight *= self.edge_weights.get(edge.kind, 0.4)
            edge_confidence *= edge.confidence
            if edge.evidence_eligibility != "strong":
                evidence_value *= 0.25
        distance_decay = 1.0 / (1.0 + 0.45 * max(0, len(path) - 1))
        change_relevance = 1.0 if role_weight >= 1.0 else 0.75
        return max(
            0.0,
            change_relevance
            * edge_confidence
            * edge_weight
            * role_weight
            * distance_decay
            * evidence_value,
        )

    @staticmethod
    def _role_weight(change_kind: str, path: list[RelationEdge]) -> float:
        kinds = {edge.kind for edge in path}
        if change_kind == "field_state":
            if kinds & {EdgeKind.READS_FIELD, EdgeKind.WRITES_FIELD}:
                return 1.5
            if kinds & {EdgeKind.CALLED_BY, EdgeKind.CALLS}:
                return 1.05
        if change_kind == "signature":
            if kinds & {
                EdgeKind.CALLED_BY,
                EdgeKind.TESTED_BY,
                EdgeKind.IMPLEMENTS,
                EdgeKind.INHERITS,
            }:
                return 1.4
        if change_kind == "type_protocol":
            if kinds & {EdgeKind.INHERITS, EdgeKind.IMPLEMENTS, EdgeKind.CALLED_BY}:
                return 1.5
        if change_kind == "api_handler":
            if kinds & {EdgeKind.CALLED_BY, EdgeKind.CALLS, EdgeKind.TESTED_BY}:
                return 1.45
        if EdgeKind.TESTED_BY in kinds:
            return 1.15
        return 0.9

    @staticmethod
    def _semantic_role(anchor: ChangedAnchor, path: list[RelationEdge]) -> str:
        kinds = {edge.kind for edge in path}
        if kinds & {EdgeKind.READS_FIELD, EdgeKind.WRITES_FIELD}:
            return "field_state"
        if EdgeKind.TESTED_BY in kinds:
            return "related_test"
        if kinds & {EdgeKind.INHERITS, EdgeKind.IMPLEMENTS}:
            return "type_contract"
        if kinds & {EdgeKind.CALLED_BY, EdgeKind.CALLS}:
            return "execution_flow"
        return anchor.change_kind

    @staticmethod
    def _manifest_edge(edge: RelationEdge) -> ManifestGraphEdge:
        return ManifestGraphEdge(
            edge_id=edge.edge_id,
            source=edge.source,
            target=edge.target,
            kind=edge.kind.value,
            path=edge.path,
            line=edge.line,
            resolver=edge.resolver,
            confidence=edge.confidence,
            confidence_tier=edge.confidence_tier.value,
            evidence_eligibility=edge.evidence_eligibility,
            reason=edge.reason,
            derived_from_edge=str(edge.metadata.get("derived_from_edge", "") or ""),
        )

    @staticmethod
    def _path_id(path: list[RelationEdge]) -> str:
        raw = "|".join(edge.edge_id for edge in path)
        return "path-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _path_prefix_key(path: list[RelationEdge]) -> str:
        """Return the stable first-hop prefix used for diversity capping."""

        if not path:
            return ""
        first = path[0]
        return f"{first.target}|{normalize_repo_path(first.path)}"

    @staticmethod
    def _record_path_selection_reason(
        manifest: CandidateContextManifest, reason: str
    ) -> None:
        manifest.path_selection_reason_counts[reason] = (
            manifest.path_selection_reason_counts.get(reason, 0) + 1
        )

    @staticmethod
    def _path_role_priority(
        anchor: ChangedAnchor, path: list[RelationEdge]
    ) -> int:
        """Rank causal production paths ahead of tests and generic navigation."""

        kinds = {edge.kind for edge in path}
        if kinds & {EdgeKind.READS_FIELD, EdgeKind.WRITES_FIELD}:
            return 0
        if kinds & {EdgeKind.CALLS, EdgeKind.CALLED_BY}:
            return 0
        if EdgeKind.TESTED_BY in kinds:
            return 2
        # Keep the anchor's change type available for future role-specific
        # ranking without treating generic graph structure as proof.
        del anchor
        return 1

    @classmethod
    def _is_production_path(cls, path: IncludedGraphPath) -> bool:
        kinds = {edge.kind for edge in path.edges}
        return bool(
            kinds
            & {
                EdgeKind.CALLS.value,
                EdgeKind.CALLED_BY.value,
                EdgeKind.READS_FIELD.value,
                EdgeKind.WRITES_FIELD.value,
            }
        ) and path.semantic_role != "related_test"

    @staticmethod
    def _path_explanation(
        anchor: ChangedAnchor, path: list[RelationEdge], score: float
    ) -> str:
        return (
            f"change_kind={anchor.change_kind}; edges="
            f"{','.join(edge.kind.value for edge in path)}; score={score:.6f}; "
            "edge semantics are navigation constraints, not runtime identity proof"
        )

    @staticmethod
    def _span_key(span: IncludedSpan) -> tuple[str, int, int, str]:
        return (span.file, span.start_line, span.end_line, span.context_hash)

    @staticmethod
    def _estimate_tokens(content: str) -> int:
        return estimate_tokens(content)

    def _read_numbered_span(self, path: str, start: int, end: int) -> str:
        try:
            lines = (
                (self.repo_root / path)
                .read_text(encoding="utf-8", errors="ignore")
                .splitlines()
            )
        except OSError:
            return ""
        bounded_start = max(1, start)
        bounded_end = min(len(lines), max(start, end))
        return "\n".join(
            f"{line_number}: {lines[line_number - 1]}"
            for line_number in range(bounded_start, bounded_end + 1)
        )

    @staticmethod
    def _append_once(values: list[str], value: str) -> None:
        if value not in values:
            values.append(value)


def extend_manifest(
    manifest: CandidateContextManifest,
    spans: Iterable[IncludedSpan],
    *,
    retrieval_source: str,
    reason: str,
) -> CandidateContextManifest:
    """Create an auditable consolidation-time retrieval extension."""

    additions = [span.model_copy(deep=True) for span in spans]
    raw = json.dumps(
        {
            "parent": manifest.candidate_id,
            "hashes": [span.context_hash for span in additions],
            "source": retrieval_source,
            "reason": reason,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    extension_id = f"{manifest.candidate_id}-X{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:6]}"
    extended = manifest.model_copy(deep=True)
    extended.candidate_id = extension_id
    extended.parent_manifest_ids = [
        *manifest.parent_manifest_ids,
        manifest.candidate_id,
    ]
    seen = {span.context_hash for span in extended.included_spans}
    for span in additions:
        span.retrieval_source = retrieval_source
        if span.context_hash not in seen:
            extended.included_spans.append(span)
            seen.add(span.context_hash)
    extended.retrieval_provenance.append(
        {
            "source": retrieval_source,
            "reason": reason,
            "parent_manifest_id": manifest.candidate_id,
            "added_context_hashes": [span.context_hash for span in additions],
        }
    )
    extended.char_cost = sum(len(span.content) for span in extended.included_spans)
    extended.token_cost = sum(span.token_cost for span in extended.included_spans)
    return extended


def manifest_union_context_ids(
    manifests: Iterable[CandidateContextManifest],
) -> set[str]:
    """Return exactly the manifest lineage available to a consolidator."""

    output: set[str] = set()
    for manifest in manifests:
        output.add(manifest.candidate_id)
        output.update(manifest.parent_manifest_ids)
    return output


def _line_pairs(start: int, lines: list[str]) -> list[tuple[int, str]]:
    return [(start + index, line) for index, line in enumerate(lines)]
