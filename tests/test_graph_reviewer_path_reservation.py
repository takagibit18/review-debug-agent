"""Regression tests for reviewer-visible Graph path reservation."""

from __future__ import annotations

import json

from src.analyzer.context_builder import ContextPart
from src.analyzer.context_priority import select_graph_prompt_parts


def test_large_manifest_is_replaced_by_header_before_graph_path_selection() -> None:
    manifest = ContextPart(
        priority=35_000,
        label="manifest:C-late",
        content=json.dumps(
            {
                "candidate_id": "C-late",
                "changed_anchor": {
                    "file": "src/breakpoint.py",
                    "line": 242,
                    "end_line": 242,
                    "symbol_id": "_create_pipeline_snapshot",
                    "change_kind": "logic",
                },
                "included_spans": [
                    {
                        "file": "src/breakpoint.py",
                        "start_line": 200,
                        "end_line": 360,
                        "symbol_id": "_create_pipeline_snapshot",
                        "role": "enclosing_symbol",
                        "content": "x" * 24_000,
                        "retrieval_source": "relation_graph",
                        "context_hash": "hash-large",
                    }
                ],
                "included_graph_paths": [],
            },
            sort_keys=True,
        ),
    )
    path = ContextPart(
        priority=36_000,
        label="manifest_path:C-late:path-production",
        content=json.dumps(
            {
                "candidate_id": "C-late",
                "path": {
                    "path_id": "path-production",
                    "semantic_role": "execution_flow",
                    "evidence_eligibility": "strong",
                    "node_ids": ["snapshot", "pipeline.run"],
                    "edges": [
                        {
                            "edge_id": "edge-call",
                            "source": "snapshot",
                            "target": "pipeline.run",
                            "kind": "CALLED_BY",
                            "path": "src/pipeline.py",
                            "line": 394,
                            "resolver": "ast",
                            "confidence": 0.92,
                            "evidence_eligibility": "strong",
                        }
                    ],
                },
            },
            sort_keys=True,
        ),
    )

    selected = select_graph_prompt_parts(
        [manifest, path],
        [manifest, path],
        token_budget=512,
    )

    labels = {part.label for part in selected}
    assert "manifest_path:C-late:path-production" in labels
    selected_manifest = next(
        part for part in selected if part.label == "manifest:C-late"
    )
    assert selected_manifest.token_count < len(manifest.content) // 4
    assert "content" not in selected_manifest.content
