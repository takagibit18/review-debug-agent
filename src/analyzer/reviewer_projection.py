"""Reviewer-only projection of internal context manifests.

The planner manifest remains the source of truth for binding and verification.  This
module deliberately returns a smaller, model-facing copy and never mutates the input.
"""

from __future__ import annotations

from typing import Any

from src.models.token_telemetry import estimate_tokens, serialize_json


_SPAN_FIELDS = (
    "span_id",
    "file",
    "start_line",
    "end_line",
    "symbol_id",
    "role",
    "content",
    "retrieval_source",
    "context_hash",
)
_ANCHOR_FIELDS = (
    "anchor_id",
    "file",
    "line",
    "end_line",
    "symbol_id",
    "change_kind",
)
_HEADER_SPAN_FIELDS = (
    "span_id",
    "file",
    "start_line",
    "end_line",
    "symbol_id",
    "role",
    "retrieval_source",
    "context_hash",
)
_EDGE_FIELDS = (
    "edge_id",
    "kind",
    "path",
    "line",
    "resolver",
    "confidence",
    "evidence_eligibility",
    "reason",
    "derived_from_edge",
)


def project_manifest_for_reviewer(
    manifest: dict[str, Any],
    *,
    telemetry_sink: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the compact reviewer projection and optional path dedupe telemetry."""

    raw_paths = manifest.get("included_graph_paths", [])
    paths = [item for item in raw_paths if isinstance(item, dict)]
    retained_paths, path_telemetry = _deduplicate_paths(
        str(manifest.get("candidate_id", "")), paths
    )

    projection: dict[str, Any] = {}
    candidate_id = manifest.get("candidate_id")
    if candidate_id is not None:
        projection["candidate_id"] = candidate_id
    anchor = manifest.get("changed_anchor")
    if isinstance(anchor, dict):
        compact_anchor = _compact_fields(anchor, _ANCHOR_FIELDS)
        if compact_anchor:
            projection["changed_anchor"] = compact_anchor
    spans = manifest.get("included_spans", [])
    if isinstance(spans, list):
        projection["included_spans"] = [
            _compact_fields(span, _SPAN_FIELDS)
            for span in spans
            if isinstance(span, dict)
        ]
    projection["included_graph_paths"] = [
        _compact_path(path) for path in retained_paths
    ]

    if telemetry_sink is not None:
        _merge_projection_telemetry(telemetry_sink, path_telemetry)
    return projection


def project_path_for_reviewer(path: dict[str, Any]) -> dict[str, Any]:
    """Return one graph path without audit fields or repeated endpoints."""

    return _compact_path(path)


def project_manifest_header_for_reviewer(
    manifest: dict[str, Any],
    *,
    max_spans: int = 6,
) -> dict[str, Any]:
    """Return a small candidate header suitable for path reservation.

    A full reviewer manifest may contain several long source spans.  Keeping a
    bounded, content-free header lets the reviewer see the causal graph path
    even when source context is under pressure.  The actual source remains
    available through the diff, file parts, or targeted tools.
    """

    projection: dict[str, Any] = {}
    candidate_id = manifest.get("candidate_id")
    if candidate_id is not None:
        projection["candidate_id"] = candidate_id
    anchor = manifest.get("changed_anchor")
    if isinstance(anchor, dict):
        compact_anchor = _compact_fields(anchor, _ANCHOR_FIELDS)
        if compact_anchor:
            projection["changed_anchor"] = compact_anchor
    spans = manifest.get("included_spans", [])
    if isinstance(spans, list):
        header_spans: list[dict[str, Any]] = []
        for span in spans:
            if not isinstance(span, dict):
                continue
            compact_span = _compact_fields(span, _HEADER_SPAN_FIELDS)
            if compact_span:
                header_spans.append(compact_span)
            if len(header_spans) >= max(0, max_spans):
                break
        if header_spans:
            projection["included_spans"] = header_spans
    projection["included_graph_paths"] = []
    return projection


def _compact_path(path: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("path_id", "semantic_role", "evidence_eligibility"):
        if key in path:
            compact[key] = path[key]

    raw_nodes = path.get("node_ids")
    nodes = [str(item) for item in raw_nodes if item is not None] if isinstance(raw_nodes, list) else []
    raw_edges = path.get("edges")
    edges = [item for item in raw_edges if isinstance(item, dict)] if isinstance(raw_edges, list) else []
    if not nodes:
        if edges and edges[0].get("source") is not None:
            nodes.append(str(edges[0]["source"]))
        for edge in edges:
            if edge.get("target") is not None:
                nodes.append(str(edge["target"]))
    if nodes:
        compact["node_ids"] = nodes
    if edges:
        compact["edges"] = [
            _compact_fields(edge, _EDGE_FIELDS) for edge in edges
        ]
    return compact


def _compact_fields(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: _compact_edge_value(key, value[key])
        for key in fields
        if key in value
        and value[key] is not None
        and not (key == "derived_from_edge" and value[key] == "")
    }


def _compact_edge_value(key: str, value: Any) -> Any:
    if key == "path" and isinstance(value, str):
        return value.replace("\\", "/").lstrip("./")
    if key == "reason" and isinstance(value, str):
        # The shared system policy already explains these two boilerplate facts.
        for marker in ("; CALLS does not prove", "; inverse of CALLS"):
            value = value.split(marker, 1)[0]
        return value.strip()
    if key == "derived_from_edge":
        return str(value)
    return value


def _deduplicate_paths(
    candidate_id: str, paths: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    duplicate_pairs: list[dict[str, Any]] = []
    semantic_duplicate_count = 0

    for path in paths:
        duplicate_index = next(
            (
                index
                for index, existing in enumerate(retained)
                if _same_semantic_path(existing, path)
            ),
            None,
        )
        if duplicate_index is None:
            retained.append(path)
            continue

        semantic_duplicate_count += 1
        existing = retained[duplicate_index]
        keep_new = _path_rank(path) > _path_rank(existing)
        if keep_new:
            retained[duplicate_index] = path
            kept, dropped = path, existing
        else:
            kept, dropped = existing, path
        reason = _duplicate_reason(kept, dropped)
        duplicate_pairs.append(
            {
                "retained_path_id": str(kept.get("path_id", "")),
                "dropped_path_id": str(dropped.get("path_id", "")),
                "reason": reason,
                "dropped_path_prompt_token_cost": estimate_tokens(
                    serialize_json(
                        {
                            "candidate_id": candidate_id,
                            "path": _compact_path(dropped),
                        }
                    )
                ),
            }
        )

    dropped_path_cost = sum(
        int(item.get("dropped_path_prompt_token_cost", 0) or 0)
        for item in duplicate_pairs
    )
    return retained, {
        "semantic_duplicate_path_count": semantic_duplicate_count,
        "dropped_semantic_duplicate_path_count": len(duplicate_pairs),
        "semantic_duplicate_prompt_token_cost": dropped_path_cost,
        "retained_path_ids": [str(item.get("path_id", "")) for item in retained],
        "dropped_path_ids": [
            str(item.get("dropped_path_id", "")) for item in duplicate_pairs
        ],
        "semantic_duplicate_paths": duplicate_pairs,
    }


def _same_semantic_path(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _is_test_relation_pair(left, right):
        return True
    return _semantic_key(left) == _semantic_key(right)


def _semantic_key(path: dict[str, Any]) -> tuple[Any, ...]:
    edges = _path_edges(path)
    return (
        tuple(_path_nodes(path)),
        _path_role(path),
        tuple(
            (
                str(edge.get("path", "")).replace("\\", "/").lstrip("./"),
                int(edge.get("line", 0) or 0),
                str(edge.get("derived_from_edge", "") or edge.get("edge_id", "")),
                str(edge.get("kind", "")),
            )
            for edge in edges
        ),
    )


def _is_test_relation_pair(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_kinds = {str(edge.get("kind", "")) for edge in _path_edges(left)}
    right_kinds = {str(edge.get("kind", "")) for edge in _path_edges(right)}
    if not (
        ("TESTED_BY" in left_kinds and "CALLED_BY" in right_kinds)
        or ("CALLED_BY" in left_kinds and "TESTED_BY" in right_kinds)
    ):
        return False
    if _path_nodes(left) != _path_nodes(right):
        return False
    left_edges = _path_edges(left)
    right_edges = _path_edges(right)
    if not left_edges or not right_edges:
        return False
    tested_path, reciprocal_path = (
        (left, right)
        if "TESTED_BY" in left_kinds
        else (right, left)
    )
    if _path_role(tested_path) != "related_test":
        return False
    if not _is_test_related_reciprocal(reciprocal_path):
        return False
    # The derived relation is only eligible for this collapse when it carries
    # an explicit trace to the original test call.
    tested_edges = [
        edge
        for edge in [*left_edges, *right_edges]
        if str(edge.get("kind", "")) == "TESTED_BY"
    ]
    derived_ids = {
        str(edge.get("derived_from_edge", "")).strip()
        for edge in tested_edges
        if str(edge.get("derived_from_edge", "")).strip()
    }
    if not derived_ids:
        return False
    left_signature = _location_confidence_signature(left_edges)
    right_signature = _location_confidence_signature(right_edges)
    if _path_evidence_signature(left) != _path_evidence_signature(right):
        return False
    # The graph builder derives TESTED_BY from the underlying test call.  Some
    # providers expose the CALLS edge id while others expose the reciprocal
    # CALLED_BY id; matching path/location/confidence metadata proves that the
    # derived and reciprocal records describe the same call site.
    return left_signature == right_signature


def _is_test_related_reciprocal(path: dict[str, Any]) -> bool:
    """Recognize the planner's CALLED_BY label for a test-call reciprocal.

    The graph planner may label the TESTED_BY path ``related_test`` while its
    reciprocal CALLED_BY path retains the generic ``execution_flow`` role.
    Only treat that mismatch as a duplicate when the reciprocal edge points
    at an identifiable test node/file; ordinary production callers remain
    distinct.
    """

    role = _path_role(path)
    if role == "related_test":
        return True
    if role != "execution_flow":
        return False
    for edge in _path_edges(path):
        if str(edge.get("kind", "")) != "CALLED_BY":
            continue
        target = str(edge.get("target", ""))
        edge_path = str(edge.get("path", "")).replace("\\", "/").lower()
        if _looks_like_test_node(target) or "/tests/" in f"/{edge_path}":
            return True
    return False


def _looks_like_test_node(node_id: str) -> bool:
    normalized = node_id.replace("\\", "/").lower()
    if "/tests/" in f"/{normalized}" or "|test|" in normalized:
        return True
    leaf = normalized.rsplit("/", 1)[-1].split("|", 1)[0]
    return leaf.startswith(("test_", "test-"))


def _path_evidence_signature(path: dict[str, Any]) -> str:
    """Require equivalent path-level evidence strength before collapsing paths."""

    return str(path.get("evidence_eligibility", "") or "")


def _location_confidence_signature(edges: list[dict[str, Any]]) -> tuple[Any, ...]:
    return tuple(
        (
            str(edge.get("path", "")).replace("\\", "/").lstrip("./"),
            int(edge.get("line", 0) or 0),
            float(edge.get("confidence", 0.0) or 0.0),
            str(edge.get("evidence_eligibility", "")),
        )
        for edge in edges
    )


def _path_edges(path: dict[str, Any]) -> list[dict[str, Any]]:
    raw = path.get("edges")
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _path_nodes(path: dict[str, Any]) -> list[str]:
    raw = path.get("node_ids")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    edges = _path_edges(path)
    if not edges:
        return []
    nodes = [str(edges[0].get("source", ""))]
    nodes.extend(str(edge.get("target", "")) for edge in edges)
    return nodes


def _path_role(path: dict[str, Any]) -> str:
    return str(path.get("semantic_role", path.get("destination_role", "")) or "")


def _path_rank(path: dict[str, Any]) -> tuple[int, int, float, str]:
    edges = _path_edges(path)
    is_tested = int(any(str(edge.get("kind", "")) == "TESTED_BY" for edge in edges))
    eligibility = int(
        str(path.get("evidence_eligibility", "")) == "strong"
        or all(str(edge.get("evidence_eligibility", "")) == "strong" for edge in edges)
    )
    confidence = min(
        (float(edge.get("confidence", 0.0) or 0.0) for edge in edges),
        default=0.0,
    )
    return is_tested, eligibility, confidence, str(path.get("path_id", ""))


def _duplicate_reason(kept: dict[str, Any], dropped: dict[str, Any]) -> str:
    kept_kinds = {str(edge.get("kind", "")) for edge in _path_edges(kept)}
    dropped_kinds = {str(edge.get("kind", "")) for edge in _path_edges(dropped)}
    if "TESTED_BY" in kept_kinds and "CALLED_BY" in dropped_kinds:
        return "dominated_by_tested_by"
    return "same_semantic_path"


def _merge_projection_telemetry(
    sink: dict[str, Any], telemetry: dict[str, Any]
) -> None:
    sink["semantic_duplicate_path_count"] = int(
        sink.get("semantic_duplicate_path_count", 0)
    ) + int(telemetry.get("semantic_duplicate_path_count", 0) or 0)
    sink["dropped_semantic_duplicate_path_count"] = int(
        sink.get("dropped_semantic_duplicate_path_count", 0)
    ) + int(telemetry.get("dropped_semantic_duplicate_path_count", 0) or 0)
    sink["semantic_duplicate_prompt_token_cost"] = int(
        sink.get("semantic_duplicate_prompt_token_cost", 0)
    ) + int(telemetry.get("semantic_duplicate_prompt_token_cost", 0) or 0)
    sink.setdefault("retained_path_ids", []).extend(
        telemetry.get("retained_path_ids", [])
    )
    sink.setdefault("dropped_path_ids", []).extend(
        telemetry.get("dropped_path_ids", [])
    )
    sink.setdefault("semantic_duplicate_paths", []).extend(
        telemetry.get("semantic_duplicate_paths", [])
    )
