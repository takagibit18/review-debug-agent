"""Change-centred static code relation graph.

The graph is intentionally evidence-aware.  It records what a resolver can
actually establish and never upgrades textual or ambiguous matches into exact
bindings.  Python uses the standard AST; the existing Rust/C# symbol backend is
retained as a conservative fallback.
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterable, Iterator
from enum import Enum
from pathlib import Path
from time import perf_counter
from typing import Any, ClassVar

from pydantic import BaseModel, Field, PrivateAttr

from src.analyzer.finding_schema import normalize_repo_path
from src.tools.symbol_backends import StaticSymbolBackend


class NodeKind(str, Enum):
    FILE = "File"
    CLASS = "Class"
    FUNCTION = "Function"
    METHOD = "Method"
    FIELD = "Field"
    TEST = "Test"
    CHANGED_HUNK = "ChangedHunk"


class EdgeKind(str, Enum):
    ENCLOSED_BY = "ENCLOSED_BY"
    CONTAINS = "CONTAINS"
    CALLS = "CALLS"
    CALLED_BY = "CALLED_BY"
    IMPORTS = "IMPORTS"
    REFERENCES = "REFERENCES"
    READS_FIELD = "READS_FIELD"
    WRITES_FIELD = "WRITES_FIELD"
    TESTED_BY = "TESTED_BY"
    INHERITS = "INHERITS"
    IMPLEMENTS = "IMPLEMENTS"


class ConfidenceTier(str, Enum):
    EXTRACTED = "EXTRACTED"
    RESOLVED = "RESOLVED"
    INFERRED = "INFERRED"
    AMBIGUOUS = "AMBIGUOUS"
    TEXTUAL = "TEXTUAL"


class CodeNode(BaseModel):
    node_id: str
    kind: NodeKind
    language: str
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=1, ge=1)
    symbol_id: str = ""
    qualified_name: str = ""
    name: str = ""
    signature: str = ""
    resolver: str = ""
    binding_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RelationEdge(BaseModel):
    edge_id: str = ""
    source: str
    target: str
    kind: EdgeKind
    path: str
    line: int = Field(default=1, ge=1)
    resolver: str
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_tier: ConfidenceTier
    evidence_eligibility: str = Field(pattern=r"^(strong|exploratory|none)$")
    reason: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: Any, /) -> None:
        if not self.edge_id:
            raw = "|".join(
                (
                    self.source,
                    self.target,
                    self.kind.value,
                    self.path,
                    str(self.line),
                    self.resolver,
                    self.reason,
                )
            )
            self.edge_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class ChangedAnchor(BaseModel):
    anchor_id: str
    file: str
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    changed_lines: list[int] = Field(default_factory=list)
    hunk_index: int = Field(ge=0)
    hunk_header: str = ""
    hunk_text: str = ""
    symbol_id: str = ""
    change_kind: str = "generic"


class CodeRelationGraph(BaseModel):
    """Serializable graph with deterministic, de-duplicated edges."""

    nodes: dict[str, CodeNode] = Field(default_factory=dict)
    edges: list[RelationEdge] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    _edge_ids: set[str] = PrivateAttr(default_factory=set)
    _outgoing_edges: dict[str, list[RelationEdge]] = PrivateAttr(default_factory=dict)
    _incoming_edges: dict[str, list[RelationEdge]] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any, /) -> None:
        self.rebuild_edge_indexes()

    def rebuild_edge_indexes(self) -> None:
        """Rebuild non-serialized lookup indexes after bulk graph replacement."""

        self._edge_ids = set()
        self._outgoing_edges = {}
        self._incoming_edges = {}
        deduplicated: list[RelationEdge] = []
        for edge in self.edges:
            if edge.edge_id in self._edge_ids:
                continue
            self._edge_ids.add(edge.edge_id)
            deduplicated.append(edge)
            self._outgoing_edges.setdefault(edge.source, []).append(edge)
            self._incoming_edges.setdefault(edge.target, []).append(edge)
        if len(deduplicated) != len(self.edges):
            self.edges = deduplicated

    def add_node(self, node: CodeNode) -> CodeNode:
        existing = self.nodes.get(node.node_id)
        if existing is None:
            self.nodes[node.node_id] = node
            return node
        return existing

    def add_edge(self, edge: RelationEdge) -> RelationEdge:
        if edge.edge_id in self._edge_ids:
            return edge
        self._edge_ids.add(edge.edge_id)
        self.edges.append(edge)
        self._outgoing_edges.setdefault(edge.source, []).append(edge)
        self._incoming_edges.setdefault(edge.target, []).append(edge)
        return edge

    def outgoing(
        self, node_id: str, kinds: set[EdgeKind] | None = None
    ) -> list[RelationEdge]:
        edges = self._outgoing_edges.get(node_id, [])
        return (
            list(edges)
            if kinds is None
            else [edge for edge in edges if edge.kind in kinds]
        )

    def incoming(
        self, node_id: str, kinds: set[EdgeKind] | None = None
    ) -> list[RelationEdge]:
        edges = self._incoming_edges.get(node_id, [])
        return (
            list(edges)
            if kinds is None
            else [edge for edge in edges if edge.kind in kinds]
        )

    def nodes_for_file(self, path: str) -> list[CodeNode]:
        normalized = normalize_repo_path(path)
        return [node for node in self.nodes.values() if node.path == normalized]

    def symbol_for_line(self, path: str, line: int) -> CodeNode | None:
        candidates = [
            node
            for node in self.nodes_for_file(path)
            if node.kind not in {NodeKind.FILE, NodeKind.CHANGED_HUNK}
            and node.start_line <= line <= node.end_line
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda node: (
                node.end_line - node.start_line,
                0
                if node.kind in {NodeKind.METHOD, NodeKind.FUNCTION, NodeKind.TEST}
                else 1,
                -node.start_line,
            ),
        )

    def remove_files(self, paths: set[str]) -> None:
        normalized = {normalize_repo_path(path) for path in paths}
        removed_ids = {
            node_id for node_id, node in self.nodes.items() if node.path in normalized
        }
        self.nodes = {
            node_id: node
            for node_id, node in self.nodes.items()
            if node_id not in removed_ids
        }
        self.edges = [
            edge
            for edge in self.edges
            if edge.source not in removed_ids
            and edge.target not in removed_ids
            and edge.path not in normalized
        ]
        self.rebuild_edge_indexes()


class _PythonDocument:
    def __init__(self, path: Path, rel: str, source: str, tree: ast.Module) -> None:
        self.path = path
        self.rel = rel
        self.source = source
        self.lines = source.splitlines()
        self.tree = tree
        self.node_for_ast: dict[int, CodeNode] = {}
        self.parent_for_ast: dict[int, ast.AST] = {}
        self.import_aliases: dict[str, tuple[str, str]] = {}
        self.class_fields: dict[tuple[str, str], CodeNode] = {}


class StaticRelationGraphBuilder:
    """Build a bounded repository graph with precise Python AST relations."""

    SUPPORTED_SUFFIXES: ClassVar[set[str]] = {".py", ".rs", ".cs"}
    EXCLUDED_PARTS: ClassVar[set[str]] = {
        ".git",
        ".mergewarden",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
    }

    def __init__(
        self,
        repo_root: Path | str,
        *,
        resolver_mode: str = "ast",
        language_resolver: Any | None = None,
        max_files: int = 5_000,
        max_ambiguous_targets: int = 4,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.resolver_mode = resolver_mode
        self.language_resolver = language_resolver
        self.max_files = max(1, max_files)
        self.max_ambiguous_targets = max(1, max_ambiguous_targets)
        self._backend = StaticSymbolBackend(self.repo_root)
        self._symbols_by_name_index: dict[str, list[CodeNode]] = {}
        self._ambiguous_truncations: dict[str, dict[str, int]] = {}
        self._skipped_weak_test_relations = 0

    def discover_files(self) -> list[Path]:
        if not self.repo_root.is_dir():
            return []
        output: list[Path] = []
        for path in self.repo_root.rglob("*"):
            if len(output) >= self.max_files:
                break
            if not path.is_file() or path.suffix.lower() not in self.SUPPORTED_SUFFIXES:
                continue
            try:
                rel_parts = path.relative_to(self.repo_root).parts
            except ValueError:
                continue
            if any(
                part in self.EXCLUDED_PARTS or part.startswith(".")
                for part in rel_parts
            ):
                continue
            output.append(path)
        return sorted(output, key=lambda value: value.as_posix())

    def build_profile(self) -> dict[str, Any]:
        """Return the persisted settings that affect graph contents."""

        return {
            "resolver_mode": self.resolver_mode,
            "max_files": self.max_files,
            "max_ambiguous_targets": self.max_ambiguous_targets,
        }

    def build(
        self,
        *,
        files: Iterable[Path | str] | None = None,
        base_graph: CodeRelationGraph | None = None,
    ) -> CodeRelationGraph:
        started = perf_counter()
        graph = (
            base_graph.model_copy(deep=True)
            if base_graph is not None
            else CodeRelationGraph()
        )
        graph.rebuild_edge_indexes()
        self._ambiguous_truncations = {}
        self._skipped_weak_test_relations = 0
        paths = [self._resolve(path) for path in (files or self.discover_files())]
        paths = [path for path in paths if path.is_file()]
        if base_graph is not None:
            graph.remove_files({self._relative(path) for path in paths})

        python_docs: list[_PythonDocument] = []
        fallback_paths: list[Path] = []
        for path in paths:
            rel = self._relative(path)
            suffix = path.suffix.lower()
            if suffix == ".py":
                try:
                    source = path.read_text(encoding="utf-8", errors="ignore")
                    tree = ast.parse(source)
                except (OSError, SyntaxError) as exc:
                    graph.diagnostics.append(
                        {
                            "path": rel,
                            "resolver": "python_ast",
                            "fallback": "file_only",
                            "error": exc.__class__.__name__,
                        }
                    )
                    self._ensure_file_node(
                        graph, rel, "python", "python_ast_fallback", 0.3
                    )
                    continue
                document = _PythonDocument(path, rel, source, tree)
                self._extract_python_definitions(graph, document)
                python_docs.append(document)
            else:
                fallback_paths.append(path)

        for path in fallback_paths:
            self._extract_fallback_definitions(graph, path)
        self._rebuild_symbol_name_index(graph)
        for document in python_docs:
            self._extract_python_relations(graph, document)
        self._extract_fallback_relations(graph, fallback_paths)
        self._derive_test_relations(graph)
        self._apply_optional_enrichment(graph, paths)
        graph.metadata.update(
            {
                "build_latency_seconds": perf_counter() - started,
                "node_count": len(graph.nodes),
                "edge_count": len(graph.edges),
                "resolver_mode": self.resolver_mode,
                "build_profile": self.build_profile(),
                "ambiguous_resolution_truncation_count": sum(
                    item["resolution_count"]
                    for item in self._ambiguous_truncations.values()
                ),
                "omitted_ambiguous_candidate_count": sum(
                    item["candidate_count"]
                    for item in self._ambiguous_truncations.values()
                ),
                "skipped_weak_test_relation_count": (self._skipped_weak_test_relations),
            }
        )
        for resolver, counts in sorted(self._ambiguous_truncations.items()):
            graph.diagnostics.append(
                {
                    "stage": "relation_resolution",
                    "resolver": resolver,
                    "fallback": "ambiguous_candidates_omitted",
                    "max_ambiguous_targets": self.max_ambiguous_targets,
                    **counts,
                }
            )
        return graph

    def _extract_python_definitions(
        self, graph: CodeRelationGraph, document: _PythonDocument
    ) -> None:
        file_node = self._ensure_file_node(
            graph, document.rel, "python", "python_ast", 1.0
        )
        for parent in ast.walk(document.tree):
            for child in ast.iter_child_nodes(parent):
                document.parent_for_ast[id(child)] = parent

        def visit_body(
            body: list[ast.stmt],
            scope: list[str],
            owner: CodeNode,
            class_owner: CodeNode | None,
        ) -> None:
            for statement in body:
                if isinstance(statement, ast.ClassDef):
                    qualified_scope = [*scope, statement.name]
                    node = self._python_node(
                        document,
                        statement,
                        NodeKind.CLASS,
                        statement.name,
                        qualified_scope,
                        metadata={
                            "bases": [
                                self._expr_name(base) for base in statement.bases
                            ],
                            "is_protocol": any(
                                self._expr_name(base).split(".")[-1] == "Protocol"
                                for base in statement.bases
                            ),
                        },
                    )
                    graph.add_node(node)
                    document.node_for_ast[id(statement)] = node
                    self._add_containment(
                        graph, owner, node, statement.lineno, "ast_scope"
                    )
                    self._extract_class_fields(
                        graph, document, statement, node, qualified_scope
                    )
                    visit_body(statement.body, qualified_scope, node, node)
                elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = (
                        NodeKind.METHOD
                        if class_owner is not None
                        else NodeKind.FUNCTION
                    )
                    if statement.name.startswith("test_") or self._is_test_path(
                        document.rel
                    ):
                        kind = NodeKind.TEST
                    qualified_scope = [*scope, statement.name]
                    node = self._python_node(
                        document,
                        statement,
                        kind,
                        statement.name,
                        qualified_scope,
                        metadata={
                            "parameters": [arg.arg for arg in statement.args.args],
                            "is_async": isinstance(statement, ast.AsyncFunctionDef),
                            "class_symbol_id": class_owner.symbol_id
                            if class_owner
                            else "",
                        },
                    )
                    graph.add_node(node)
                    document.node_for_ast[id(statement)] = node
                    self._add_containment(
                        graph, owner, node, statement.lineno, "ast_scope"
                    )
                    visit_body(statement.body, qualified_scope, node, class_owner)
                elif owner.kind == NodeKind.FILE:
                    for name, target in self._module_assignment_names(statement):
                        node = self._python_node(
                            document,
                            statement,
                            NodeKind.FIELD,
                            name,
                            [*scope, name],
                            metadata={"declaration": target, "scope": "module"},
                        )
                        graph.add_node(node)
                        document.node_for_ast.setdefault(id(statement), node)
                        self._add_containment(
                            graph, owner, node, node.start_line, "ast_assignment"
                        )

        visit_body(document.tree.body, [], file_node, None)

    def _extract_class_fields(
        self,
        graph: CodeRelationGraph,
        document: _PythonDocument,
        class_ast: ast.ClassDef,
        class_node: CodeNode,
        qualified_scope: list[str],
    ) -> None:
        declarations: dict[str, tuple[ast.AST, str]] = {}
        for statement in class_ast.body:
            for name, rendered in self._module_assignment_names(statement):
                declarations.setdefault(name, (statement, rendered))
        for descendant in ast.walk(class_ast):
            if not isinstance(descendant, ast.Attribute):
                continue
            if not isinstance(
                descendant.value, ast.Name
            ) or descendant.value.id not in {"self", "cls"}:
                continue
            if not isinstance(descendant.ctx, (ast.Store, ast.Del)):
                continue
            declarations.setdefault(
                descendant.attr,
                (descendant, f"{descendant.value.id}.{descendant.attr}"),
            )
        for name, (node_ast, declaration) in declarations.items():
            field = self._python_node(
                document,
                node_ast,
                NodeKind.FIELD,
                name,
                [*qualified_scope, name],
                metadata={"declaration": declaration, "scope": "class"},
            )
            graph.add_node(field)
            document.class_fields[(class_node.symbol_id, name)] = field
            self._add_containment(
                graph, class_node, field, field.start_line, "ast_field"
            )

    def _extract_python_relations(
        self, graph: CodeRelationGraph, document: _PythonDocument
    ) -> None:
        file_node = graph.nodes[self._file_node_id(document.rel)]
        self._extract_imports(graph, document, file_node)

        for node_ast in ast.walk(document.tree):
            source = self._enclosing_code_node(document, node_ast) or file_node
            if isinstance(node_ast, ast.Call):
                targets, confidence, tier, resolver, reason = self._resolve_expression(
                    graph, document, node_ast.func, source
                )
                for target in targets:
                    eligibility = (
                        "strong"
                        if confidence >= 0.65 and len(targets) == 1
                        else "exploratory"
                    )
                    self._add_bidirectional_call(
                        graph,
                        source,
                        target,
                        getattr(node_ast, "lineno", source.start_line),
                        document.rel,
                        resolver,
                        confidence,
                        tier,
                        eligibility,
                        reason,
                    )
            if isinstance(node_ast, ast.Attribute):
                self._extract_field_access(graph, document, node_ast, source)
            if isinstance(node_ast, ast.Return) and node_ast.value is not None:
                targets, confidence, tier, resolver, _ = self._resolve_expression(
                    graph, document, node_ast.value, source
                )
                for target in targets:
                    if target.node_id == source.node_id:
                        continue
                    graph.add_edge(
                        self._edge(
                            source,
                            target,
                            EdgeKind.REFERENCES,
                            document.rel,
                            getattr(node_ast, "lineno", source.start_line),
                            resolver,
                            confidence,
                            tier,
                            "strong" if confidence >= 0.65 else "exploratory",
                            "directly_returned_symbol",
                        )
                    )

        self._extract_inheritance(graph, document)

    def _extract_imports(
        self,
        graph: CodeRelationGraph,
        document: _PythonDocument,
        file_node: CodeNode,
    ) -> None:
        for statement in document.tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    local = alias.asname or alias.name.split(".")[0]
                    document.import_aliases[local] = (alias.name, "")
                    self._add_import_edge(
                        graph, document, file_node, alias.name, statement.lineno, local
                    )
            elif isinstance(statement, ast.ImportFrom):
                module = self._absolute_import_module(document.rel, statement)
                for alias in statement.names:
                    local = alias.asname or alias.name
                    document.import_aliases[local] = (module, alias.name)
                self._add_import_edge(
                    graph,
                    document,
                    file_node,
                    module,
                    statement.lineno,
                    ",".join(alias.asname or alias.name for alias in statement.names),
                )

    def _add_import_edge(
        self,
        graph: CodeRelationGraph,
        document: _PythonDocument,
        file_node: CodeNode,
        module: str,
        line: int,
        alias: str,
    ) -> None:
        target_path = self._module_path(module)
        if not target_path:
            graph.diagnostics.append(
                {
                    "path": document.rel,
                    "line": line,
                    "resolver": "ast_import",
                    "fallback": "external_or_unresolved",
                    "module": module,
                }
            )
            return
        target = graph.nodes.get(self._file_node_id(target_path))
        if target is None:
            target = self._ensure_file_node(
                graph, target_path, "python", "ast_import", 0.9
            )
        graph.add_edge(
            self._edge(
                file_node,
                target,
                EdgeKind.IMPORTS,
                document.rel,
                line,
                "ast_import",
                0.95,
                ConfidenceTier.RESOLVED,
                "strong",
                f"import {module} as {alias}",
                metadata={"module": module, "alias": alias},
            )
        )

    def _extract_field_access(
        self,
        graph: CodeRelationGraph,
        document: _PythonDocument,
        attribute: ast.Attribute,
        source: CodeNode,
    ) -> None:
        if not isinstance(attribute.value, ast.Name) or attribute.value.id not in {
            "self",
            "cls",
        }:
            return
        class_symbol_id = str(source.metadata.get("class_symbol_id", ""))
        if not class_symbol_id:
            class_node = self._containing_class(graph, source)
            class_symbol_id = class_node.symbol_id if class_node else ""
        field = document.class_fields.get((class_symbol_id, attribute.attr))
        if field is None:
            return
        kinds: list[EdgeKind] = []
        if isinstance(attribute.ctx, ast.Load):
            kinds.append(EdgeKind.READS_FIELD)
        elif isinstance(attribute.ctx, (ast.Store, ast.Del)):
            kinds.append(EdgeKind.WRITES_FIELD)
        parent = document.parent_for_ast.get(id(attribute))
        if isinstance(parent, ast.AugAssign):
            kinds = [EdgeKind.READS_FIELD, EdgeKind.WRITES_FIELD]
        direct_parameter = False
        if EdgeKind.WRITES_FIELD in kinds and isinstance(
            parent, (ast.Assign, ast.AnnAssign)
        ):
            value = parent.value
            parameters = set(source.metadata.get("parameters", []))
            direct_parameter = isinstance(value, ast.Name) and value.id in parameters
        for kind in kinds:
            graph.add_edge(
                self._edge(
                    source,
                    field,
                    kind,
                    document.rel,
                    attribute.lineno,
                    "python_ast_field",
                    1.0,
                    ConfidenceTier.EXTRACTED,
                    "strong",
                    (
                        f"{attribute.value.id}.{attribute.attr} has AST context "
                        f"{type(attribute.ctx).__name__}; READS_FIELD does not identify "
                        "a particular write and WRITES_FIELD does not prove path execution"
                    ),
                    metadata={"direct_parameter_assignment": direct_parameter},
                )
            )

    def _extract_inheritance(
        self, graph: CodeRelationGraph, document: _PythonDocument
    ) -> None:
        for class_ast in (
            node for node in ast.walk(document.tree) if isinstance(node, ast.ClassDef)
        ):
            source = document.node_for_ast.get(id(class_ast))
            if source is None:
                continue
            for base in class_ast.bases:
                name = self._expr_name(base).split(".")[-1]
                targets = self._symbols_by_name(graph, name, kinds={NodeKind.CLASS})
                if not targets:
                    continue
                unique = len(targets) == 1
                if not unique:
                    targets = self._bounded_ambiguous_candidates(
                        targets, "python_ast_base_candidates"
                    )
                if not targets:
                    continue
                for target in targets:
                    is_protocol = bool(
                        target.metadata.get("is_protocol")
                    ) or name.endswith("Protocol")
                    graph.add_edge(
                        self._edge(
                            source,
                            target,
                            EdgeKind.IMPLEMENTS if is_protocol else EdgeKind.INHERITS,
                            document.rel,
                            class_ast.lineno,
                            "python_ast_base_resolution"
                            if unique
                            else "python_ast_base_candidates",
                            0.9 if unique else 0.45,
                            ConfidenceTier.RESOLVED
                            if unique
                            else ConfidenceTier.AMBIGUOUS,
                            "strong" if unique else "exploratory",
                            f"class base expression {self._expr_name(base)}",
                        )
                    )

    def _resolve_expression(
        self,
        graph: CodeRelationGraph,
        document: _PythonDocument,
        expression: ast.AST,
        source: CodeNode,
    ) -> tuple[list[CodeNode], float, ConfidenceTier, str, str]:
        if isinstance(expression, ast.Name):
            imported = document.import_aliases.get(expression.id)
            if imported and imported[1]:
                target_path = self._module_path(imported[0])
                targets = self._symbols_by_name(
                    graph, imported[1], path=target_path or None
                )
                if targets:
                    unique = len(targets) == 1
                    if not unique:
                        targets = self._bounded_ambiguous_candidates(
                            targets, "ast_import_alias_candidates"
                        )
                    if not targets:
                        return self._unresolved_expression()
                    return (
                        targets,
                        0.93 if unique else 0.45,
                        ConfidenceTier.RESOLVED if unique else ConfidenceTier.AMBIGUOUS,
                        "ast_import_alias_binding"
                        if unique
                        else "ast_import_alias_candidates",
                        f"name {expression.id} imported from {imported[0]}.{imported[1]}",
                    )
            local = self._lexical_candidates(graph, source, expression.id)
            if local:
                return (
                    local,
                    0.96,
                    ConfidenceTier.RESOLVED,
                    "ast_lexical_binding",
                    "same lexical scope",
                )
            candidates = self._symbols_by_name(graph, expression.id)
            if candidates:
                unique = len(candidates) == 1
                if not unique:
                    candidates = self._bounded_ambiguous_candidates(
                        candidates, "ast_bare_name_candidates"
                    )
                if not candidates:
                    return self._unresolved_expression()
                return (
                    candidates,
                    0.85 if unique else 0.4,
                    ConfidenceTier.RESOLVED if unique else ConfidenceTier.AMBIGUOUS,
                    "ast_repo_unique_name" if unique else "ast_bare_name_candidates",
                    "repository symbol-name resolution"
                    if unique
                    else "multiple bare-name candidates",
                )
        if isinstance(expression, ast.Attribute):
            base = self._expr_name(expression.value)
            if base in {"self", "cls"}:
                class_node = self._containing_class(graph, source)
                if class_node:
                    children = [
                        graph.nodes[edge.target]
                        for edge in graph.outgoing(
                            class_node.node_id, {EdgeKind.CONTAINS}
                        )
                        if edge.target in graph.nodes
                        and graph.nodes[edge.target].name == expression.attr
                    ]
                    if children:
                        return (
                            children,
                            0.98,
                            ConfidenceTier.RESOLVED,
                            "ast_self_attribute",
                            "class member binding",
                        )
            imported = document.import_aliases.get(base.split(".")[0])
            if imported:
                target_path = self._module_path(imported[0])
                candidates = self._symbols_by_name(
                    graph, expression.attr, path=target_path or None
                )
                if candidates:
                    unique = len(candidates) == 1
                    if not unique:
                        candidates = self._bounded_ambiguous_candidates(
                            candidates, "ast_import_attribute_candidates"
                        )
                    if not candidates:
                        return self._unresolved_expression()
                    return (
                        candidates,
                        0.92 if unique else 0.45,
                        ConfidenceTier.RESOLVED if unique else ConfidenceTier.AMBIGUOUS,
                        "ast_import_attribute"
                        if unique
                        else "ast_import_attribute_candidates",
                        f"qualified attribute through import alias {base}",
                    )
            candidates = self._symbols_by_name(graph, expression.attr)
            if candidates:
                unique = len(candidates) == 1
                if not unique:
                    candidates = self._bounded_ambiguous_candidates(
                        candidates, "ast_attribute_candidates"
                    )
                if not candidates:
                    return self._unresolved_expression()
                return (
                    candidates,
                    0.75 if unique else 0.35,
                    ConfidenceTier.INFERRED if unique else ConfidenceTier.AMBIGUOUS,
                    "ast_unique_attribute_candidate"
                    if unique
                    else "ast_attribute_candidates",
                    "attribute receiver type unavailable",
                )
        return self._unresolved_expression()

    @staticmethod
    def _unresolved_expression() -> tuple[
        list[CodeNode], float, ConfidenceTier, str, str
    ]:
        return (
            [],
            0.0,
            ConfidenceTier.TEXTUAL,
            "unresolved_ast_expression",
            "no safe binding",
        )

    def _bounded_ambiguous_candidates(
        self, candidates: list[CodeNode], resolver: str
    ) -> list[CodeNode]:
        if len(candidates) <= self.max_ambiguous_targets:
            return candidates
        counts = self._ambiguous_truncations.setdefault(
            resolver, {"resolution_count": 0, "candidate_count": 0}
        )
        counts["resolution_count"] += 1
        counts["candidate_count"] += len(candidates)
        return []

    def _extract_fallback_definitions(
        self, graph: CodeRelationGraph, path: Path
    ) -> None:
        rel = self._relative(path)
        language = self._backend.language_for_path(path)
        if language == "unknown":
            file_node = self._ensure_file_node(
                graph, rel, language, "textual_file_fallback", 0.2
            )
            graph.diagnostics.append(
                {
                    "path": rel,
                    "resolver": "textual_file_fallback",
                    "fallback": "file_node_only",
                    "error": "unsupported_language",
                }
            )
        else:
            file_node = self._ensure_file_node(
                graph, rel, language, "static_rule_parser", 0.75
            )
        records = self._backend.document_symbols(path)
        for record in records:
            kind = self._fallback_kind(record.kind, rel, record.name)
            qualified_scope = record.name
            node = CodeNode(
                node_id=self._symbol_id(
                    language, rel, qualified_scope, kind, record.line, record.end_line
                ),
                symbol_id=self._symbol_id(
                    language, rel, qualified_scope, kind, record.line, record.end_line
                ),
                kind=kind,
                language=language,
                path=rel,
                start_line=record.line,
                end_line=max(record.line, record.end_line),
                qualified_name=f"{rel}::{qualified_scope}",
                name=record.name,
                signature=record.signature,
                resolver="static_rule_parser",
                binding_confidence=record.confidence,
                metadata={"legacy_kind": record.kind},
            )
            graph.add_node(node)
            self._add_containment(
                graph, file_node, node, node.start_line, "static_rule_scope"
            )

    def _extract_fallback_relations(
        self, graph: CodeRelationGraph, paths: list[Path]
    ) -> None:
        for path in paths:
            rel = self._relative(path)
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            owners = [
                node
                for node in graph.nodes_for_file(rel)
                if node.kind not in {NodeKind.FILE, NodeKind.FIELD}
            ]
            for line_number, line in enumerate(lines, start=1):
                owner = next(
                    (
                        node
                        for node in sorted(
                            owners, key=lambda item: item.start_line, reverse=True
                        )
                        if node.start_line <= line_number <= node.end_line
                    ),
                    graph.nodes.get(self._file_node_id(rel)),
                )
                if owner is None:
                    continue
                for match in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", line):
                    candidates = self._symbols_by_name(graph, match.group(1))
                    if len(candidates) != 1 or candidates[0].node_id == owner.node_id:
                        continue
                    self._add_bidirectional_call(
                        graph,
                        owner,
                        candidates[0],
                        line_number,
                        rel,
                        "static_text_call",
                        0.45,
                        ConfidenceTier.TEXTUAL,
                        "exploratory",
                        "textual call-like syntax; receiver/type unresolved",
                    )

    def _derive_test_relations(self, graph: CodeRelationGraph) -> None:
        existing = {edge.edge_id for edge in graph.edges}
        for edge in list(graph.edges):
            source = graph.nodes.get(edge.source)
            target = graph.nodes.get(edge.target)
            if source is None or target is None or source.kind != NodeKind.TEST:
                continue
            if edge.kind not in {EdgeKind.CALLS, EdgeKind.REFERENCES}:
                continue
            if target.kind == NodeKind.TEST:
                continue
            if edge.evidence_eligibility != "strong" or edge.confidence < 0.65:
                self._skipped_weak_test_relations += 1
                continue
            tested = self._edge(
                target,
                source,
                EdgeKind.TESTED_BY,
                edge.path,
                edge.line,
                "derived_from_test_reference",
                edge.confidence,
                edge.confidence_tier,
                edge.evidence_eligibility,
                f"test node has {edge.kind.value} relation to symbol; does not prove branch coverage",
                metadata={"derived_from_edge": edge.edge_id},
            )
            if tested.edge_id not in existing:
                graph.add_edge(tested)
                existing.add(tested.edge_id)

    def _apply_optional_enrichment(
        self, graph: CodeRelationGraph, paths: list[Path]
    ) -> None:
        if self.resolver_mode == "ast":
            return
        if self.language_resolver is None:
            graph.diagnostics.append(
                {
                    "resolver": "language_resolver"
                    if self.resolver_mode == "resolver"
                    else "lsp",
                    "fallback": "ast",
                    "error": "resolver_not_configured",
                }
            )
            return
        try:
            result = self.language_resolver.enrich(self.repo_root, graph, paths)
            if isinstance(result, CodeRelationGraph):
                graph.nodes = result.nodes
                graph.edges = result.edges
                graph.diagnostics.extend(result.diagnostics)
                graph.rebuild_edge_indexes()
        except Exception as exc:  # noqa: BLE001
            graph.diagnostics.append(
                {
                    "resolver": getattr(
                        self.language_resolver, "name", self.resolver_mode
                    ),
                    "fallback": "ast",
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )

    def _python_node(
        self,
        document: _PythonDocument,
        node_ast: ast.AST,
        kind: NodeKind,
        name: str,
        qualified_scope: list[str],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> CodeNode:
        start = int(getattr(node_ast, "lineno", 1))
        end = int(getattr(node_ast, "end_lineno", start))
        scope = ".".join(qualified_scope)
        symbol_id = self._symbol_id("python", document.rel, scope, kind, start, end)
        signature = (
            document.lines[start - 1].strip() if start <= len(document.lines) else name
        )
        return CodeNode(
            node_id=symbol_id,
            symbol_id=symbol_id,
            kind=kind,
            language="python",
            path=document.rel,
            start_line=start,
            end_line=max(start, end),
            qualified_name=f"{document.rel}::{scope}",
            name=name,
            signature=signature,
            resolver="python_ast",
            binding_confidence=1.0,
            metadata=metadata or {},
        )

    def _ensure_file_node(
        self,
        graph: CodeRelationGraph,
        rel: str,
        language: str,
        resolver: str,
        confidence: float,
    ) -> CodeNode:
        node_id = self._file_node_id(rel)
        node = CodeNode(
            node_id=node_id,
            kind=NodeKind.FILE,
            language=language,
            path=rel,
            start_line=1,
            end_line=self._line_count(rel),
            qualified_name=rel,
            name=Path(rel).name,
            resolver=resolver,
            binding_confidence=confidence,
        )
        return graph.add_node(node)

    def _add_containment(
        self,
        graph: CodeRelationGraph,
        owner: CodeNode,
        child: CodeNode,
        line: int,
        resolver: str,
    ) -> None:
        graph.add_edge(
            self._edge(
                owner,
                child,
                EdgeKind.CONTAINS,
                child.path,
                line,
                resolver,
                1.0,
                ConfidenceTier.EXTRACTED,
                "strong",
                "lexical containment",
            )
        )
        graph.add_edge(
            self._edge(
                child,
                owner,
                EdgeKind.ENCLOSED_BY,
                child.path,
                line,
                resolver,
                1.0,
                ConfidenceTier.EXTRACTED,
                "strong",
                "lexical enclosing scope",
            )
        )

    def _add_bidirectional_call(
        self,
        graph: CodeRelationGraph,
        source: CodeNode,
        target: CodeNode,
        line: int,
        path: str,
        resolver: str,
        confidence: float,
        tier: ConfidenceTier,
        eligibility: str,
        reason: str,
    ) -> None:
        graph.add_edge(
            self._edge(
                source,
                target,
                EdgeKind.CALLS,
                path,
                line,
                resolver,
                confidence,
                tier,
                eligibility,
                reason + "; CALLS does not prove argument-value identity",
            )
        )
        graph.add_edge(
            self._edge(
                target,
                source,
                EdgeKind.CALLED_BY,
                path,
                line,
                resolver,
                confidence,
                tier,
                eligibility,
                reason + "; inverse of CALLS",
            )
        )

    @staticmethod
    def _edge(
        source: CodeNode,
        target: CodeNode,
        kind: EdgeKind,
        path: str,
        line: int,
        resolver: str,
        confidence: float,
        tier: ConfidenceTier,
        eligibility: str,
        reason: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> RelationEdge:
        return RelationEdge(
            source=source.node_id,
            target=target.node_id,
            kind=kind,
            path=path,
            line=max(1, int(line)),
            resolver=resolver,
            confidence=confidence,
            confidence_tier=tier,
            evidence_eligibility=eligibility,
            reason=reason,
            metadata=metadata or {},
        )

    def _lexical_candidates(
        self, graph: CodeRelationGraph, source: CodeNode, name: str
    ) -> list[CodeNode]:
        same_file = self._symbols_by_name(graph, name, path=source.path)
        if not same_file:
            return []
        if "::" not in source.qualified_name:
            return same_file[:1] if len(same_file) == 1 else []
        scope = source.qualified_name.split("::", 1)[1].split(".")[:-1]
        ranked = sorted(
            same_file,
            key=lambda node: (
                0
                if any(
                    node.qualified_name.endswith(".".join([*scope[:index], name]))
                    for index in range(len(scope), -1, -1)
                )
                else 1,
                abs(node.start_line - source.start_line),
            ),
        )
        best = ranked[0]
        return (
            [best]
            if len(ranked) == 1 or ranked[0].qualified_name != ranked[1].qualified_name
            else []
        )

    def _symbols_by_name(
        self,
        graph: CodeRelationGraph,
        name: str,
        *,
        path: str | None = None,
        kinds: set[NodeKind] | None = None,
    ) -> list[CodeNode]:
        excluded = {NodeKind.FILE, NodeKind.CHANGED_HUNK, NodeKind.FIELD}
        candidates = self._symbols_by_name_index.get(name, [])
        return [
            node
            for node in candidates
            if (path is None or node.path == path)
            and (
                kinds is not None
                and node.kind in kinds
                or kinds is None
                and node.kind not in excluded
            )
        ]

    def _rebuild_symbol_name_index(self, graph: CodeRelationGraph) -> None:
        index: dict[str, list[CodeNode]] = {}
        for node in graph.nodes.values():
            index.setdefault(node.name, []).append(node)
        for nodes in index.values():
            nodes.sort(
                key=lambda node: (node.path, node.start_line, node.qualified_name)
            )
        self._symbols_by_name_index = index

    def _enclosing_code_node(
        self, document: _PythonDocument, node_ast: ast.AST
    ) -> CodeNode | None:
        current: ast.AST | None = node_ast
        while current is not None:
            candidate = document.node_for_ast.get(id(current))
            if candidate is not None and candidate.kind in {
                NodeKind.FUNCTION,
                NodeKind.METHOD,
                NodeKind.TEST,
            }:
                return candidate
            current = document.parent_for_ast.get(id(current))
        return None

    @staticmethod
    def _containing_class(
        graph: CodeRelationGraph, source: CodeNode
    ) -> CodeNode | None:
        current = source
        visited: set[str] = set()
        while current.node_id not in visited:
            visited.add(current.node_id)
            parent_edges = graph.outgoing(current.node_id, {EdgeKind.ENCLOSED_BY})
            if not parent_edges:
                return None
            parent = graph.nodes.get(parent_edges[0].target)
            if parent is None:
                return None
            if parent.kind == NodeKind.CLASS:
                return parent
            current = parent
        return None

    @staticmethod
    def _module_assignment_names(statement: ast.stmt) -> list[tuple[str, str]]:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        output: list[tuple[str, str]] = []
        for target in targets:
            if isinstance(target, ast.Name):
                output.append((target.id, target.id))
        return output

    def _absolute_import_module(self, rel: str, statement: ast.ImportFrom) -> str:
        module = statement.module or ""
        if statement.level <= 0:
            return module
        package = list(Path(rel).with_suffix("").parts[:-1])
        trim = max(0, statement.level - 1)
        if trim:
            package = package[:-trim] if trim <= len(package) else []
        if module:
            package.extend(module.split("."))
        return ".".join(package)

    def _module_path(self, module: str) -> str:
        if not module:
            return ""
        candidates = [
            self.repo_root / (module.replace(".", "/") + ".py"),
            self.repo_root / module.replace(".", "/") / "__init__.py",
        ]
        for path in candidates:
            if path.is_file():
                return self._relative(path)
        return ""

    @staticmethod
    def _expr_name(expression: ast.AST) -> str:
        if isinstance(expression, ast.Name):
            return expression.id
        if isinstance(expression, ast.Attribute):
            prefix = StaticRelationGraphBuilder._expr_name(expression.value)
            return f"{prefix}.{expression.attr}" if prefix else expression.attr
        if isinstance(expression, ast.Call):
            return StaticRelationGraphBuilder._expr_name(expression.func)
        if isinstance(expression, ast.Subscript):
            return StaticRelationGraphBuilder._expr_name(expression.value)
        return ""

    @staticmethod
    def _fallback_kind(raw: str, rel: str, name: str) -> NodeKind:
        if name.startswith("test_") or StaticRelationGraphBuilder._is_test_path(rel):
            return NodeKind.TEST
        if raw in {"class", "struct", "trait", "interface", "enum", "impl"}:
            return NodeKind.CLASS
        if raw in {"method", "constructor"}:
            return NodeKind.METHOD
        if raw in {"field", "assign"}:
            return NodeKind.FIELD
        return NodeKind.FUNCTION

    @staticmethod
    def _is_test_path(rel: str) -> bool:
        path = Path(rel)
        return (
            "tests" in path.parts
            or path.name.startswith("test_")
            or ".test." in path.name
            or ".spec." in path.name
        )

    def _line_count(self, rel: str) -> int:
        try:
            return max(
                1,
                len(
                    (self.repo_root / rel)
                    .read_text(encoding="utf-8", errors="ignore")
                    .splitlines()
                ),
            )
        except OSError:
            return 1

    def _resolve(self, value: Path | str) -> Path:
        path = Path(value)
        return (
            path.resolve() if path.is_absolute() else (self.repo_root / path).resolve()
        )

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return path.as_posix()

    @staticmethod
    def _file_node_id(rel: str) -> str:
        return "file|" + normalize_repo_path(rel)

    @staticmethod
    def _symbol_id(
        language: str,
        rel: str,
        scope: str,
        kind: NodeKind,
        start: int,
        end: int,
    ) -> str:
        return f"{language}|{normalize_repo_path(rel)}|{scope}|{kind.value.lower()}|{start}:{end}"


_HUNK_HEADER = re.compile(
    r"^@@\s+-(?P<old_start>\d+)(?:,(?P<old_count>\d+))?\s+"
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))?\s+@@"
)


def extract_changed_anchors(
    diff_text: str, graph: CodeRelationGraph | None = None
) -> list[ChangedAnchor]:
    """Extract one anchor per changed hunk and attach its enclosing symbol."""

    anchors: list[ChangedAnchor] = []
    current_file = ""
    lines = diff_text.splitlines()
    index = 0
    hunk_index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("+++ "):
            current_file = normalize_repo_path(line[4:].split("\t", 1)[0])
            if current_file == "/dev/null":
                current_file = ""
            index += 1
            continue
        header = _HUNK_HEADER.match(line)
        if header is None or not current_file:
            index += 1
            continue
        new_line = int(header.group("new_start"))
        changed: list[int] = []
        hunk_lines = [line]
        index += 1
        while index < len(lines) and not lines[index].startswith(
            ("@@ ", "diff --git ", "+++ ")
        ):
            body = lines[index]
            hunk_lines.append(body)
            if body.startswith("+") and not body.startswith("+++"):
                changed.append(new_line)
                new_line += 1
            elif body.startswith("-") and not body.startswith("---"):
                pass
            else:
                new_line += 1
            index += 1
        if not changed:
            hunk_index += 1
            continue
        primary_line = changed[0]
        symbol = graph.symbol_for_line(current_file, primary_line) if graph else None
        hunk_text = "\n".join(hunk_lines)
        raw_id = f"{current_file}|{hunk_index}|{primary_line}|{changed[-1]}|{line}"
        anchors.append(
            ChangedAnchor(
                anchor_id="A-"
                + hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:10],
                file=current_file,
                line=primary_line,
                end_line=changed[-1],
                changed_lines=changed,
                hunk_index=hunk_index,
                hunk_header=line,
                hunk_text=hunk_text,
                symbol_id=symbol.symbol_id if symbol else "",
                change_kind=classify_change(hunk_text, symbol),
            )
        )
        hunk_index += 1
    return anchors


def attach_changed_hunks(
    graph: CodeRelationGraph, anchors: list[ChangedAnchor]
) -> None:
    """Materialize changed-hunk nodes without treating them as semantic proof."""

    for anchor in anchors:
        node = CodeNode(
            node_id=f"hunk|{anchor.anchor_id}",
            kind=NodeKind.CHANGED_HUNK,
            language=_language_from_path(anchor.file),
            path=anchor.file,
            start_line=anchor.line,
            end_line=anchor.end_line,
            qualified_name=f"{anchor.file}::{anchor.anchor_id}",
            name=anchor.anchor_id,
            signature=anchor.hunk_header,
            resolver="git_diff",
            binding_confidence=1.0,
            metadata={
                "changed_lines": anchor.changed_lines,
                "change_kind": anchor.change_kind,
            },
        )
        graph.add_node(node)
        owner = graph.nodes.get(anchor.symbol_id) if anchor.symbol_id else None
        if owner is None:
            owner = graph.nodes.get("file|" + anchor.file)
        if owner is None:
            continue
        builder_edge = RelationEdge(
            source=node.node_id,
            target=owner.node_id,
            kind=EdgeKind.ENCLOSED_BY,
            path=anchor.file,
            line=anchor.line,
            resolver="diff_symbol_intersection",
            confidence=1.0 if anchor.symbol_id else 0.8,
            confidence_tier=ConfidenceTier.EXTRACTED,
            evidence_eligibility="strong",
            reason="changed hunk line intersects enclosing symbol span",
        )
        graph.add_edge(builder_edge)
        graph.add_edge(
            RelationEdge(
                source=owner.node_id,
                target=node.node_id,
                kind=EdgeKind.CONTAINS,
                path=anchor.file,
                line=anchor.line,
                resolver="diff_symbol_intersection",
                confidence=builder_edge.confidence,
                confidence_tier=ConfidenceTier.EXTRACTED,
                evidence_eligibility="strong",
                reason="enclosing symbol contains changed hunk",
            )
        )


def classify_change(hunk_text: str, symbol: CodeNode | None = None) -> str:
    lowered = hunk_text.lower()
    if re.search(
        r"\b(cache|state|invalidate|language|model|memo|self\.|_[a-z])", lowered
    ):
        return "field_state"
    if re.search(r"^[+-]\s*(?:async\s+)?def\s+", hunk_text, re.MULTILINE):
        return "signature"
    if re.search(r"\b(protocol|interface|inherits|extends|class\s+\w+\s*\()", lowered):
        return "type_protocol"
    if re.search(r"\b(route|router|endpoint|handler|request|response|http)\b", lowered):
        return "api_handler"
    if symbol is not None and symbol.kind == NodeKind.CLASS:
        return "type_protocol"
    return "generic"


def _language_from_path(path: str) -> str:
    return {".py": "python", ".rs": "rust", ".cs": "csharp"}.get(
        Path(path).suffix.lower(), "unknown"
    )


def iter_execution_paths(
    graph: CodeRelationGraph,
    start_node: str,
    *,
    max_depth: int = 2,
    max_paths: int = 50,
) -> Iterator[list[RelationEdge]]:
    """Yield bounded caller/callee/field paths around a changed symbol."""

    allowed = {
        EdgeKind.CALLS,
        EdgeKind.CALLED_BY,
        EdgeKind.READS_FIELD,
        EdgeKind.WRITES_FIELD,
        EdgeKind.TESTED_BY,
        EdgeKind.INHERITS,
        EdgeKind.IMPLEMENTS,
    }
    queue: list[tuple[str, list[RelationEdge], set[str]]] = [
        (start_node, [], {start_node})
    ]
    emitted = 0
    while queue and emitted < max_paths:
        node_id, path, visited = queue.pop(0)
        if path:
            emitted += 1
            yield path
        if len(path) >= max_depth:
            continue
        for edge in graph.outgoing(node_id, allowed):
            if edge.target in visited:
                continue
            queue.append((edge.target, [*path, edge], {*visited, edge.target}))
