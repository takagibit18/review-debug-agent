"""Change-centred context planner and actual manifest payload tests."""

from __future__ import annotations

import json
from pathlib import Path

from src.analyzer.code_graph import (
    EdgeKind,
    StaticRelationGraphBuilder,
    extract_changed_anchors,
)
from src.analyzer.context_planner import ChangeCenteredContextPlanner
from src.analyzer.context_state import ContextState
from src.analyzer.prompts import USER_PREFIX_REVIEW, build_review_messages
from src.analyzer.schemas import ReviewRequest


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _diff(path: str, line: int, old: str, new: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -{line},1 +{line},1 @@\n"
        f"-{old}\n+{new}\n"
    )


def _plan(
    tmp_path: Path,
    files: list[Path],
    diff: str,
    **kwargs: object,
):
    graph = StaticRelationGraphBuilder(tmp_path).build(files=files)
    anchors = extract_changed_anchors(diff, graph)
    result = ChangeCenteredContextPlanner(tmp_path, **kwargs).plan(graph, anchors)
    return graph, anchors, result


def test_changed_hunk_enclosing_symbol_and_signature_are_forced(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "service.py",
        "def load(value):\n    normalized = value.strip()\n    return normalized\n",
    )
    diff = _diff(
        "service.py", 2, "    normalized = value", "    normalized = value.strip()"
    )

    _, _, result = _plan(tmp_path, [source], diff)
    manifest = result.manifests[0]

    roles = {span.role for span in manifest.included_spans}
    assert "changed_hunk" in roles
    assert "symbol_signature" in roles
    assert "enclosing_symbol" in roles
    assert all(
        span.forced
        for span in manifest.included_spans
        if span.role in {"changed_hunk", "symbol_signature", "enclosing_symbol"}
    )
    assert manifest.changed_anchor.symbol_id


def test_token_and_character_budget_discards_optional_paths_not_changed_hunk(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "large.py",
        "def helper():\n    return 1\n\n"
        "def changed():\n"
        + "".join(f"    value_{index} = helper()\n" for index in range(50))
        + "    return value_49\n",
    )
    diff = _diff("large.py", 5, "    value_0 = 0", "    value_0 = helper()")

    _, _, result = _plan(
        tmp_path,
        [source],
        diff,
        max_context_tokens=80,
        max_context_chars=320,
        max_depth=2,
    )
    manifest = result.manifests[0]

    assert any(span.role == "changed_hunk" for span in manifest.included_spans)
    assert manifest.truncation_reasons
    assert any(
        reason in {"token_budget", "character_budget"}
        for reason in manifest.truncation_reasons
    ) or any("clipped" in reason for reason in manifest.truncation_reasons)


def test_depth_truncation_limits_execution_flow(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "flow.py",
        "def leaf():\n    return 1\n\n"
        "def middle():\n    return leaf()\n\n"
        "def entry():\n    return middle()\n",
    )
    diff = _diff("flow.py", 8, "    return 0", "    return middle()")

    _, _, shallow = _plan(
        tmp_path,
        [source],
        diff,
        max_depth=1,
        max_context_tokens=1000,
    )
    _, _, deep = _plan(
        tmp_path,
        [source],
        diff,
        max_depth=2,
        max_context_tokens=1000,
    )

    shallow_nodes = {
        node_id
        for path in shallow.manifests[0].included_graph_paths
        for node_id in path.node_ids
    }
    deep_nodes = {
        node_id
        for path in deep.manifests[0].included_graph_paths
        for node_id in path.node_ids
    }
    leaf_id = next(node_id for node_id in deep_nodes if "|leaf|" in node_id)
    assert leaf_id not in shallow_nodes
    assert leaf_id in deep_nodes


def test_node_and_span_deduplication(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "dedupe.py",
        "def target():\n    return 1\n\n"
        "def caller():\n    return target() + target()\n",
    )
    diff = _diff(
        "dedupe.py", 5, "    return target()", "    return target() + target()"
    )

    _, _, result = _plan(tmp_path, [source], diff, max_context_tokens=1000)
    manifest = result.manifests[0]

    span_keys = {
        (span.file, span.start_line, span.end_line, span.context_hash)
        for span in manifest.included_spans
    }
    assert len(span_keys) == len(manifest.included_spans)
    assert len({path.path_id for path in manifest.included_graph_paths}) == len(
        manifest.included_graph_paths
    )


def test_low_confidence_ambiguous_call_is_excluded_from_evidence_paths(
    tmp_path: Path,
) -> None:
    files = [
        _write(tmp_path / "one.py", "def target():\n    return 1\n"),
        _write(tmp_path / "two.py", "def target():\n    return 2\n"),
        _write(tmp_path / "caller.py", "def caller():\n    return target()\n"),
    ]
    diff = _diff("caller.py", 2, "    return 0", "    return target()")

    _, _, result = _plan(
        tmp_path,
        files,
        diff,
        min_evidence_confidence=0.65,
        max_context_tokens=1000,
    )
    manifest = result.manifests[0]

    assert manifest.excluded_low_confidence_paths
    assert not any(
        any(edge.confidence < 0.65 for edge in path.edges)
        for path in manifest.included_graph_paths
    )


def test_field_state_change_prioritizes_field_read_write_paths(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "cache.py",
        "class Cache:\n"
        "    def __init__(self, value):\n"
        "        self.value = value\n\n"
        "    def read(self):\n"
        "        return self.value\n\n"
        "    def update(self, value):\n"
        "        self.value = value\n",
    )
    diff = _diff(
        "cache.py", 9, "        self.value = None", "        self.value = value"
    )

    _, anchors, result = _plan(tmp_path, [source], diff, max_context_tokens=1000)
    manifest = result.manifests[0]
    field_paths = [
        path
        for path in manifest.included_graph_paths
        if any(
            edge.kind in {EdgeKind.READS_FIELD.value, EdgeKind.WRITES_FIELD.value}
            for edge in path.edges
        )
    ]

    assert anchors[0].change_kind == "field_state"
    assert field_paths
    assert all(path.semantic_role == "field_state" for path in field_paths)
    assert max(path.score for path in field_paths) >= max(
        (path.score for path in manifest.included_graph_paths), default=0.0
    )


def test_api_handler_change_prioritizes_callers_and_tests(tmp_path: Path) -> None:
    app = _write(
        tmp_path / "api.py",
        "def handler(request):\n"
        "    return request.value\n\n"
        "def entry(request):\n"
        "    return handler(request)\n",
    )
    tests = _write(
        tmp_path / "tests" / "test_api.py",
        "from api import handler\n\n"
        "def test_handler():\n"
        "    return handler(object())\n",
    )
    diff = _diff("api.py", 2, "    return None", "    return request.value")

    _, anchors, result = _plan(
        tmp_path,
        [app, tests],
        diff,
        max_context_tokens=1500,
    )
    manifest = result.manifests[0]
    edge_kinds = {
        edge.kind for path in manifest.included_graph_paths for edge in path.edges
    }

    assert anchors[0].change_kind == "api_handler"
    assert EdgeKind.CALLED_BY.value in edge_kinds
    assert EdgeKind.TESTED_BY.value in edge_kinds


def test_custom_edge_weights_change_path_scores(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "weights.py",
        "class State:\n"
        "    def __init__(self):\n"
        "        self.value = 0\n\n"
        "    def helper(self):\n"
        "        return 1\n\n"
        "    def changed(self):\n"
        "        self.value = self.helper()\n",
    )
    diff = _diff(
        "weights.py",
        9,
        "        self.value = 0",
        "        self.value = self.helper()",
    )
    graph = StaticRelationGraphBuilder(tmp_path).build(files=[source])
    anchor = extract_changed_anchors(diff, graph)[0]
    field_heavy = ChangeCenteredContextPlanner(
        tmp_path,
        max_context_tokens=1000,
        edge_weights={EdgeKind.WRITES_FIELD: 2.0, EdgeKind.CALLS: 0.1},
    ).plan_candidate(graph, anchor)

    write_score = max(
        path.score
        for path in field_heavy.included_graph_paths
        if any(edge.kind == EdgeKind.WRITES_FIELD.value for edge in path.edges)
    )
    call_score = max(
        path.score
        for path in field_heavy.included_graph_paths
        if any(edge.kind == EdgeKind.CALLS.value for edge in path.edges)
    )
    assert write_score > call_score


def test_manifest_in_prompt_is_exact_planner_output_even_when_base_context_truncates(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path / "service.py", "def run():\n    return 2\n")
    diff = _diff("service.py", 2, "    return 1", "    return 2")
    _, _, result = _plan(tmp_path, [source], diff, max_context_tokens=500)
    expected = result.manifests[0].prompt_payload()
    state = ContextState(
        goal="review",
        candidate_context_manifests=[expected],
        relation_graph_summary={"node_count": 2},
    )
    request = ReviewRequest(
        repo_path=str(tmp_path),
        diff_mode=True,
        diff_text=diff,
    )

    messages = build_review_messages(
        request,
        state,
        diff,
        {"service.py": source.read_text(encoding="utf-8") * 100},
        prompt_token_budget=20,
    )
    payload = json.loads(messages[1].content[len(USER_PREFIX_REVIEW) :])

    assert payload["candidate_context_manifests"] == [expected]
    assert payload["candidate_context_manifests"][0]["included_spans"]
    assert payload["truncated"]["any"] is True
