"""Tests for static symbol context tooling."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.tools.symbol_backends import StaticSymbolBackend
from src.tools.symbol_context_tool import FindSymbolContextTool


def test_python_static_backend_finds_classes_functions_async_and_enclosing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "sample.py"
    source.write_text(
        "\n".join(
            [
                "import os",
                "VALUE = 1",
                "",
                "class Worker:",
                "    value = 0",
                "    def run(self):",
                "        return helper()",
                "",
                "async def helper():",
                "    return 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    tool = FindSymbolContextTool(tmp_path, backend=StaticSymbolBackend(tmp_path))

    result = asyncio.run(tool.execute(symbol="Worker", path="sample.py", mode="all", line=7))

    assert result["backend"] == "static"
    assert result["language"] == "python"
    assert any(item["kind"] == "class" and item["name"] == "Worker" for item in result["definitions"])
    assert any(item["kind"] == "function" and item["name"] == "run" for item in result["enclosing_symbols"])
    assert any(item["kind"] == "class" and item["name"] == "Worker" for item in result["enclosing_symbols"])


def test_static_backend_finds_csharp_class_field_method_and_constructor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "Engine.cs"
    source.write_text(
        "\n".join(
            [
                "using System;",
                "public class EngineRpcModule",
                "{",
                "    private readonly GCKeeper _gcKeeper;",
                "    public EngineRpcModule(GCKeeper gcKeeper)",
                "    {",
                "        _gcKeeper = gcKeeper;",
                "    }",
                "    public void NewPayload()",
                "    {",
                "        _gcKeeper.TryStartNoGCRegion();",
                "    }",
                "}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    backend = StaticSymbolBackend(tmp_path)

    records = backend.document_symbols(source)

    assert any(item.kind == "class" and item.name == "EngineRpcModule" for item in records)
    assert any(item.kind == "field" and item.name == "_gcKeeper" for item in records)
    assert any(item.kind == "constructor" and item.name == "EngineRpcModule" for item in records)
    assert any(item.kind == "method" and item.name == "NewPayload" for item in records)


def test_static_backend_finds_rust_struct_fn_and_impl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "lib.rs"
    source.write_text(
        "\n".join(
            [
                "use crate::runtime;",
                "pub struct Keeper;",
                "impl Keeper {",
                "    pub fn run(&self) {}",
                "}",
                "fn helper() {}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    backend = StaticSymbolBackend(tmp_path)

    records = backend.document_symbols(source)

    assert any(item.kind == "struct" and item.name == "Keeper" for item in records)
    assert any(item.kind == "impl" and item.name == "Keeper" for item in records)
    assert any(item.kind == "function" and item.name == "run" for item in records)


def test_find_symbol_context_references_return_context_and_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "refs.py"
    source.write_text(
        "\n".join(
            [
                "target = 1",
                "value = target",
                "again = target + 1",
                "third = target + 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    tool = FindSymbolContextTool(tmp_path, backend=StaticSymbolBackend(tmp_path))

    result = asyncio.run(
        tool.execute(symbol="target", path="refs.py", mode="references", max_results=2, context_radius=1)
    )

    assert result["backend"] == "static"
    assert len(result["references"]) == 2
    assert result["truncated"] is True
    assert "1: target = 1" in result["references"][0]["context"]


def test_find_symbol_context_unknown_language_uses_low_confidence_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "notes.txt"
    source.write_text("alpha target\nbeta target\n", encoding="utf-8")
    tool = FindSymbolContextTool(tmp_path, backend=StaticSymbolBackend(tmp_path))

    result = asyncio.run(tool.execute(symbol="target", path="notes.txt", mode="all"))

    assert result["backend"] == "static"
    assert result["language"] == "unknown"
    assert result["definitions"][0]["confidence"] == 0.35
    assert result["references"][0]["confidence"] == 0.35
    assert result["warnings"] == ["static fallback used for unsupported language"]
