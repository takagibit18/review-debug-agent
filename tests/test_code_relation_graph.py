"""Static relation graph, resolver confidence, and index lifecycle tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.analyzer import persistent_index
from src.analyzer.code_graph import (
    CodeNode,
    CodeRelationGraph,
    ConfidenceTier,
    EdgeKind,
    NodeKind,
    RelationEdge,
    StaticRelationGraphBuilder,
)
from src.analyzer.language_resolver import CallableLanguageResolver, EnrichedRelation
from src.analyzer.persistent_index import RelationGraphIndex, repository_identity


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _relation_edge(sequence: int, *, source: str = "source") -> RelationEdge:
    return RelationEdge(
        source=source,
        target=f"target-{sequence}",
        kind=EdgeKind.CALLS,
        path="service.py",
        line=max(1, sequence),
        resolver="test",
        confidence=0.9,
        confidence_tier=ConfidenceTier.RESOLVED,
        evidence_eligibility="strong",
        reason="test relation",
    )


def test_edge_indexes_survive_serialization_copy_and_file_removal() -> None:
    graph = CodeRelationGraph(
        nodes={
            "source": CodeNode(
                node_id="source",
                kind=NodeKind.FUNCTION,
                language="python",
                path="service.py",
            ),
            "target-1": CodeNode(
                node_id="target-1",
                kind=NodeKind.FUNCTION,
                language="python",
                path="target.py",
            ),
        }
    )
    edge = _relation_edge(1)
    graph.add_edge(edge)
    graph.add_edge(edge.model_copy(deep=True))

    restored = CodeRelationGraph.model_validate_json(graph.model_dump_json())
    copied = restored.model_copy(deep=True)

    assert len(restored.edges) == 1
    assert restored.outgoing("source") == restored.edges
    assert restored.incoming("target-1") == restored.edges
    copied.remove_files({"target.py"})
    assert copied.edges == []
    assert copied.outgoing("source") == []
    assert copied.incoming("target-1") == []


def test_large_edge_batch_is_deduplicated_without_changing_order() -> None:
    graph = CodeRelationGraph()
    edges = [_relation_edge(index) for index in range(1, 5_001)]

    for edge in edges:
        graph.add_edge(edge)
    for edge in edges:
        graph.add_edge(edge)

    assert len(graph.edges) == 5_000
    assert graph.edges == edges
    assert len(graph.outgoing("source")) == 5_000


def test_qualified_symbol_identity_separates_same_bare_method_names(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "service.py",
        "class Alpha:\n"
        "    def run(self):\n"
        "        return 1\n\n"
        "class Beta:\n"
        "    def run(self):\n"
        "        return 2\n",
    )

    graph = StaticRelationGraphBuilder(tmp_path).build(files=[source])
    methods = [node for node in graph.nodes.values() if node.name == "run"]

    assert len(methods) == 2
    assert len({node.symbol_id for node in methods}) == 2
    assert {node.qualified_name for node in methods} == {
        "service.py::Alpha.run",
        "service.py::Beta.run",
    }
    assert all("python|service.py|" in node.symbol_id for node in methods)


def test_direct_call_and_import_alias_are_resolved_with_provenance(
    tmp_path: Path,
) -> None:
    service = _write(tmp_path / "service.py", "def execute():\n    return 1\n")
    caller = _write(
        tmp_path / "caller.py",
        "from service import execute as run\n\ndef invoke():\n    return run()\n",
    )

    graph = StaticRelationGraphBuilder(tmp_path).build(files=[service, caller])
    invoke = next(
        node
        for node in graph.nodes.values()
        if node.qualified_name == "caller.py::invoke"
    )
    execute = next(
        node
        for node in graph.nodes.values()
        if node.qualified_name == "service.py::execute"
    )
    call = next(
        edge
        for edge in graph.edges
        if edge.source == invoke.node_id
        and edge.target == execute.node_id
        and edge.kind == EdgeKind.CALLS
    )

    assert call.resolver == "ast_import_alias_binding"
    assert call.confidence >= 0.9
    assert call.confidence_tier == ConfidenceTier.RESOLVED
    assert call.evidence_eligibility == "strong"
    assert "does not prove argument-value identity" in call.reason
    assert any(edge.kind == EdgeKind.IMPORTS for edge in graph.edges)
    assert any(
        edge.kind == EdgeKind.CALLED_BY
        and edge.source == execute.node_id
        and edge.target == invoke.node_id
        for edge in graph.edges
    )


def test_ambiguous_bare_call_remains_exploratory(tmp_path: Path) -> None:
    files = [
        _write(tmp_path / "first.py", "def target():\n    return 1\n"),
        _write(tmp_path / "second.py", "def target():\n    return 2\n"),
        _write(tmp_path / "caller.py", "def invoke():\n    return target()\n"),
    ]

    graph = StaticRelationGraphBuilder(tmp_path).build(files=files)
    invoke = next(
        node
        for node in graph.nodes.values()
        if node.qualified_name == "caller.py::invoke"
    )
    calls = [
        edge
        for edge in graph.edges
        if edge.source == invoke.node_id and edge.kind == EdgeKind.CALLS
    ]

    assert len(calls) == 2
    assert all(edge.confidence_tier == ConfidenceTier.AMBIGUOUS for edge in calls)
    assert all(edge.evidence_eligibility == "exploratory" for edge in calls)
    assert all(edge.confidence < 0.65 for edge in calls)


def test_ambiguous_attribute_candidates_are_kept_at_limit(tmp_path: Path) -> None:
    files = [
        _write(
            tmp_path / f"service_{index}.py",
            f"class Service{index}:\n    def execute(self):\n        return {index}\n",
        )
        for index in range(4)
    ]
    files.append(
        _write(
            tmp_path / "caller.py",
            "def invoke(client):\n    return client.execute()\n",
        )
    )

    graph = StaticRelationGraphBuilder(tmp_path, max_ambiguous_targets=4).build(
        files=files
    )
    invoke = next(node for node in graph.nodes.values() if node.name == "invoke")
    calls = graph.outgoing(invoke.node_id, {EdgeKind.CALLS})

    assert len(calls) == 4
    assert all(edge.resolver == "ast_attribute_candidates" for edge in calls)
    assert graph.metadata["ambiguous_resolution_truncation_count"] == 0


def test_ambiguous_attribute_candidates_over_limit_are_omitted(
    tmp_path: Path,
) -> None:
    files = [
        _write(
            tmp_path / f"service_{index}.py",
            f"class Service{index}:\n    def execute(self):\n        return {index}\n",
        )
        for index in range(5)
    ]
    files.append(
        _write(
            tmp_path / "tests" / "test_service.py",
            "def test_execute(client):\n    return client.execute()\n",
        )
    )

    graph = StaticRelationGraphBuilder(tmp_path, max_ambiguous_targets=4).build(
        files=files
    )
    test_node = next(
        node for node in graph.nodes.values() if node.name == "test_execute"
    )

    assert graph.outgoing(test_node.node_id, {EdgeKind.CALLS}) == []
    assert not any(edge.kind == EdgeKind.TESTED_BY for edge in graph.edges)
    assert graph.metadata["ambiguous_resolution_truncation_count"] == 1
    assert graph.metadata["omitted_ambiguous_candidate_count"] == 5
    assert any(
        diagnostic.get("resolver") == "ast_attribute_candidates"
        and diagnostic.get("fallback") == "ambiguous_candidates_omitted"
        for diagnostic in graph.diagnostics
    )


def test_weak_test_call_does_not_derive_tested_by(tmp_path: Path) -> None:
    files = [
        _write(
            tmp_path / f"service_{index}.py",
            f"class Service{index}:\n    def execute(self):\n        return {index}\n",
        )
        for index in range(2)
    ]
    files.append(
        _write(
            tmp_path / "tests" / "test_service.py",
            "def test_execute(client):\n    return client.execute()\n",
        )
    )

    graph = StaticRelationGraphBuilder(tmp_path).build(files=files)

    assert any(
        edge.kind == EdgeKind.CALLS and edge.evidence_eligibility == "exploratory"
        for edge in graph.edges
    )
    assert not any(edge.kind == EdgeKind.TESTED_BY for edge in graph.edges)
    assert graph.metadata["skipped_weak_test_relation_count"] == 2


def test_python_field_reads_and_writes_include_direct_parameter_metadata(
    tmp_path: Path,
) -> None:
    source = _write(
        tmp_path / "cache.py",
        "class Cache:\n"
        "    def __init__(self, value):\n"
        "        self.value = value\n\n"
        "    def get(self):\n"
        "        return self.value\n\n"
        "    def bump(self):\n"
        "        self.value += 1\n",
    )

    graph = StaticRelationGraphBuilder(tmp_path).build(files=[source])
    field = next(
        node
        for node in graph.nodes.values()
        if node.kind == NodeKind.FIELD
        and node.qualified_name == "cache.py::Cache.value"
    )
    reads = [
        edge
        for edge in graph.edges
        if edge.kind == EdgeKind.READS_FIELD and edge.target == field.node_id
    ]
    writes = [
        edge
        for edge in graph.edges
        if edge.kind == EdgeKind.WRITES_FIELD and edge.target == field.node_id
    ]

    assert {graph.nodes[edge.source].name for edge in reads} == {"get", "bump"}
    assert {graph.nodes[edge.source].name for edge in writes} == {"__init__", "bump"}
    init_write = next(
        edge for edge in writes if graph.nodes[edge.source].name == "__init__"
    )
    assert init_write.metadata["direct_parameter_assignment"] is True
    assert all(edge.resolver == "python_ast_field" for edge in reads + writes)


def test_inheritance_implementation_and_test_relations(tmp_path: Path) -> None:
    protocol = _write(
        tmp_path / "service.py",
        "from typing import Protocol\n\n"
        "class RunnerProtocol(Protocol):\n"
        "    def run(self): ...\n\n"
        "class Base:\n"
        "    pass\n\n"
        "class Child(Base):\n"
        "    def run(self):\n"
        "        return 1\n",
    )
    test_file = _write(
        tmp_path / "tests" / "test_service.py",
        "from service import Child\n\ndef test_child():\n    return Child()\n",
    )

    graph = StaticRelationGraphBuilder(tmp_path).build(files=[protocol, test_file])
    child = next(
        node
        for node in graph.nodes.values()
        if node.qualified_name == "service.py::Child"
    )
    base = next(
        node
        for node in graph.nodes.values()
        if node.qualified_name == "service.py::Base"
    )
    test_node = next(node for node in graph.nodes.values() if node.name == "test_child")

    assert any(
        edge.kind == EdgeKind.INHERITS
        and edge.source == child.node_id
        and edge.target == base.node_id
        for edge in graph.edges
    )
    assert test_node.kind == NodeKind.TEST
    assert any(
        edge.kind == EdgeKind.TESTED_BY
        and edge.source == child.node_id
        and edge.target == test_node.node_id
        for edge in graph.edges
    )


def test_protocol_subclass_emits_implements_relation(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "protocols.py",
        "from typing import Protocol\n\n"
        "class ReaderProtocol(Protocol):\n"
        "    pass\n\n"
        "class Reader(ReaderProtocol):\n"
        "    pass\n",
    )

    graph = StaticRelationGraphBuilder(tmp_path).build(files=[source])

    assert any(edge.kind == EdgeKind.IMPLEMENTS for edge in graph.edges)


def test_incremental_index_cleans_deleted_and_renamed_symbols(tmp_path: Path) -> None:
    old = _write(tmp_path / "old_name.py", "class OldName:\n    pass\n")
    index_path = tmp_path / ".mergewarden" / "test-index.sqlite3"
    first = RelationGraphIndex(tmp_path, index_path=index_path).build()
    assert any(node.name == "OldName" for node in first.graph.nodes.values())

    old.unlink()
    _write(tmp_path / "new_name.py", "class NewName:\n    pass\n")
    second = RelationGraphIndex(tmp_path, index_path=index_path).build()

    assert second.status == "incremental_update"
    assert second.deleted_files == ["old_name.py"]
    assert "new_name.py" in second.changed_files
    assert not any(node.path == "old_name.py" for node in second.graph.nodes.values())
    assert any(node.name == "NewName" for node in second.graph.nodes.values())
    assert second.parsed_file_count >= 1


def test_persistent_index_reuses_unchanged_graph(tmp_path: Path) -> None:
    _write(tmp_path / "stable.py", "def stable():\n    return 1\n")
    index_path = tmp_path / ".mergewarden" / "reuse.sqlite3"
    first = RelationGraphIndex(tmp_path, index_path=index_path).build()
    second = RelationGraphIndex(tmp_path, index_path=index_path).build()

    assert first.status == "build"
    assert second.status == "reuse"
    assert second.cache_hit is True
    assert second.cache_hit_rate == 1.0
    assert second.parsed_file_count == 0


def test_persistent_index_rebuilds_when_build_profile_changes(tmp_path: Path) -> None:
    _write(tmp_path / "stable.py", "def stable():\n    return 1\n")
    index_path = tmp_path / ".mergewarden" / "profile.sqlite3"

    first = RelationGraphIndex(
        tmp_path,
        index_path=index_path,
        resolver_mode="ast",
        max_files=100,
        max_ambiguous_targets=4,
    ).build()
    reused = RelationGraphIndex(
        tmp_path,
        index_path=index_path,
        resolver_mode="ast",
        max_files=100,
        max_ambiguous_targets=4,
    ).build()
    changed_limit = RelationGraphIndex(
        tmp_path,
        index_path=index_path,
        resolver_mode="ast",
        max_files=100,
        max_ambiguous_targets=3,
    ).build()
    changed_files = RelationGraphIndex(
        tmp_path,
        index_path=index_path,
        resolver_mode="ast",
        max_files=99,
        max_ambiguous_targets=3,
    ).build()
    changed_resolver = RelationGraphIndex(
        tmp_path,
        index_path=index_path,
        resolver_mode="resolver",
        max_files=99,
        max_ambiguous_targets=3,
    ).build()

    assert first.status == "build"
    assert reused.status == "reuse"
    for result in (changed_limit, changed_files, changed_resolver):
        assert result.status == "rebuild"
        assert result.cache_hit is False
        assert result.fallback == "build_profile_changed"


def test_persistent_index_rebuilds_when_builder_version_changes(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "stable.py", "def stable():\n    return 1\n")
    index_path = tmp_path / ".mergewarden" / "version.sqlite3"
    RelationGraphIndex(tmp_path, index_path=index_path).build()
    connection = sqlite3.connect(index_path)
    try:
        connection.execute("UPDATE repositories SET build_version = 'v-old'")
        connection.commit()
    finally:
        connection.close()

    result = RelationGraphIndex(tmp_path, index_path=index_path).build()

    assert result.status == "rebuild"
    assert result.cache_hit is False
    assert result.fallback.startswith("build_version_changed:v-old->")


def test_repository_identity_prefers_stable_remote_over_checkout_path(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        persistent_index,
        "_git_output",
        lambda repo_root, args: "https://github.com/example/repo.git",
    )

    assert repository_identity(tmp_path / "checkout-1") == repository_identity(
        tmp_path / "checkout-2"
    )


def test_incremental_index_reparses_import_neighbor_but_not_unrelated_file(
    tmp_path: Path,
) -> None:
    service = _write(tmp_path / "service.py", "def run():\n    return 1\n")
    _write(
        tmp_path / "consumer.py",
        "from service import run\n\ndef consume():\n    return run()\n",
    )
    _write(tmp_path / "unrelated.py", "def untouched():\n    return 0\n")
    index_path = tmp_path / ".mergewarden" / "incremental.sqlite3"
    RelationGraphIndex(tmp_path, index_path=index_path).build()

    connection = sqlite3.connect(index_path)
    try:
        before = dict(connection.execute("SELECT path, updated_at FROM files"))
    finally:
        connection.close()
    service.write_text("def run():\n    return 2\n", encoding="utf-8")

    result = RelationGraphIndex(tmp_path, index_path=index_path).build()
    connection = sqlite3.connect(index_path)
    try:
        after = dict(connection.execute("SELECT path, updated_at FROM files"))
    finally:
        connection.close()

    assert result.status == "incremental_update"
    assert result.changed_files == ["service.py"]
    assert {"service.py", "consumer.py"}.issubset(result.affected_files)
    assert "unrelated.py" not in result.affected_files
    assert result.parsed_file_count == 2
    assert after["consumer.py"] != before["consumer.py"]
    assert after["unrelated.py"] == before["unrelated.py"]


def test_incompatible_cache_schema_is_preserved_and_rebuilt(tmp_path: Path) -> None:
    _write(tmp_path / "service.py", "def run():\n    return 1\n")
    index_path = tmp_path / ".mergewarden" / "schema.sqlite3"
    RelationGraphIndex(tmp_path, index_path=index_path).build()
    connection = sqlite3.connect(index_path)
    try:
        connection.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
        connection.commit()
    finally:
        connection.close()

    result = RelationGraphIndex(tmp_path, index_path=index_path).build()

    assert result.status == "rebuild"
    assert result.fallback.startswith("schema_rebuild:")
    assert list(index_path.parent.glob("schema.sqlite3.incompatible-*"))
    assert any(node.name == "run" for node in result.graph.nodes.values())


def test_corrupt_cache_is_preserved_and_rebuilt(tmp_path: Path) -> None:
    _write(tmp_path / "service.py", "def run():\n    return 1\n")
    index_path = tmp_path / ".mergewarden" / "corrupt.sqlite3"
    index_path.parent.mkdir(parents=True)
    index_path.write_bytes(b"not-a-sqlite-database")

    result = RelationGraphIndex(tmp_path, index_path=index_path).build()

    assert result.status == "rebuild"
    assert "rebuild" in result.fallback
    assert index_path.exists()
    assert list(index_path.parent.glob("corrupt.sqlite3.corrupt-*"))
    assert any(node.name == "run" for node in result.graph.nodes.values())


def test_unsupported_language_uses_file_only_low_confidence_fallback(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path / "notes.xyz", "target()\n")

    graph = StaticRelationGraphBuilder(tmp_path).build(files=[source])
    file_node = next(node for node in graph.nodes.values() if node.path == "notes.xyz")

    assert file_node.kind == NodeKind.FILE
    assert file_node.resolver == "textual_file_fallback"
    assert file_node.binding_confidence == 0.2
    assert any(
        item.get("error") == "unsupported_language" for item in graph.diagnostics
    )
    assert not any(edge.kind == EdgeKind.CALLS for edge in graph.edges)


def test_optional_language_resolver_adds_provenanced_edge(tmp_path: Path) -> None:
    source = _write(
        tmp_path / "service.py",
        "def source():\n    return 1\n\ndef target():\n    return 2\n",
    )

    def enrich(repo_root, graph, paths):  # type: ignore[no-untyped-def]
        del repo_root, paths
        symbols = sorted(
            (node for node in graph.nodes.values() if node.kind == NodeKind.FUNCTION),
            key=lambda node: node.name,
        )
        return [
            EnrichedRelation(
                source_symbol_id=symbols[0].symbol_id,
                target_symbol_id=symbols[1].symbol_id,
                kind="REFERENCES",
                path="service.py",
                line=1,
                confidence=0.93,
                reason="test resolver exact reference binding",
                resolver="fixture_language_resolver",
            )
        ]

    graph = StaticRelationGraphBuilder(
        tmp_path,
        resolver_mode="resolver",
        language_resolver=CallableLanguageResolver("fixture", enrich),
    ).build(files=[source])

    edge = next(
        item for item in graph.edges if item.resolver == "fixture_language_resolver"
    )
    assert edge.kind == EdgeKind.REFERENCES
    assert edge.confidence_tier == ConfidenceTier.RESOLVED
    assert edge.evidence_eligibility == "strong"
    assert edge.metadata == {"fallback": "ast", "enriched": True}
    assert graph.outgoing(edge.source, {EdgeKind.REFERENCES}) == [edge]
    assert graph.incoming(edge.target, {EdgeKind.REFERENCES}) == [edge]


def test_optional_language_resolver_failure_retains_ast_graph(tmp_path: Path) -> None:
    source = _write(tmp_path / "service.py", "def run():\n    return 1\n")

    def fail(repo_root, graph, paths):  # type: ignore[no-untyped-def]
        del repo_root, graph, paths
        raise RuntimeError("resolver unavailable")

    graph = StaticRelationGraphBuilder(
        tmp_path,
        resolver_mode="resolver",
        language_resolver=CallableLanguageResolver("broken", fail),
    ).build(files=[source])

    assert any(node.name == "run" for node in graph.nodes.values())
    assert any(
        item.get("resolver") == "broken"
        and item.get("fallback") == "ast"
        and "RuntimeError" in str(item.get("error"))
        for item in graph.diagnostics
    )
