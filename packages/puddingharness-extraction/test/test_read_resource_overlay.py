"""Behavior and dependency-boundary tests for the Harness read-resource overlay."""

from __future__ import annotations

import asyncio
import ast
from io import BytesIO
from pathlib import Path
import re
import sys
import importlib.util
import types

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
OVERLAY = REPOSITORY_ROOT / "packages/puddingharness-extraction/overlays/backend/tools/read_resource_tool.py"

from graph.attachment_store import attachment_store


@pytest.fixture(autouse=True)
def target_resource_reader(monkeypatch):
    # Load this overlay explicitly; a partial overlay directory must never
    # replace the whole backend package search path during test collection.
    spec = importlib.util.spec_from_file_location("target_read_resource_overlay", OVERLAY)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setitem(globals(), "ReadResourceTool", module.ReadResourceTool)


def test_overlay_has_no_legacy_business_imports_or_virtual_mounts() -> None:
    source = OVERLAY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(module.startswith(("knowledge", "analytics", "vanna")) for module in imported_modules)
    for business_mount in (
        "/knowledge/",
        "/semantic-assets/",
        "/sql-guardrails/",
        "/analytics-models/",
    ):
        assert business_mount not in source


def test_reads_image_from_workspace_virtual_path(tmp_path: Path) -> None:
    image = tmp_path / "charts" / "plot.png"
    image.parent.mkdir()
    image.write_bytes(b"png")

    result = ReadResourceTool(workspace_path=str(tmp_path)).invoke(
        {"resource": "/workspace/charts/plot.png"}
    )

    assert "Local resource:" in result
    assert "Type: image" in result
    assert str(image.resolve()) in result
    assert f"PuddingClaw-Resource-Image-Path: {image.resolve()}" in result


def test_workspace_virtual_path_cannot_escape_workspace(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(b"png")

    result = ReadResourceTool(workspace_path=str(tmp_path)).invoke(
        {"resource": "/workspace/../outside.png"}
    )

    assert "Permission required" in result or "File not found" in result
    assert "PuddingClaw-Resource-Image-Path" not in result


def test_reads_arbitrary_mcp_resource_through_explicit_async_adapter() -> None:
    calls: list[tuple[str, str, int, int]] = []

    async def read_resource(
        server: str, uri: str, offset: int, limit: int
    ) -> dict[str, object]:
        calls.append((server, uri, offset, limit))
        return {"contents": [{"uri": uri, "text": "external resource"}]}

    result = asyncio.run(
        ReadResourceTool(resource_reader=read_resource)._arun(
            "vendor-resource://server/item",
            offset=0,
            limit=17,
            mcp_server="vendor",
        )
    )

    assert result == "MCP Resource: vendor-resource://server/item\nexternal resource"
    assert calls == [("vendor", "vendor-resource://server/item", 0, 17)]


def test_sync_mcp_adapter_is_rejected_without_event_loop_bridge() -> None:
    def read_resource(*_args: object) -> dict[str, object]:
        return {"contents": []}

    result = ReadResourceTool(resource_reader=read_resource).invoke(
        {
            "resource": "vendor-resource://server/item",
            "mcp_server": "vendor",
        }
    )

    assert "reader is async" in result


def test_mcp_resource_requires_runtime_adapter() -> None:
    result = ReadResourceTool().invoke(
        {"resource": "resource://server/item", "mcp_server": "vendor"}
    )
    assert "MCP Resource reader unavailable" in result


def test_mcp_uri_never_broadcasts_or_guesses_a_server() -> None:
    result = ReadResourceTool().invoke({"resource": "resource://server/item"})
    assert "explicit mcp_server" in result


def test_mcp_failure_is_a_tool_error_for_tool_calls() -> None:
    result = asyncio.run(
        ReadResourceTool().ainvoke(
            {
                "name": "read_resource",
                "args": {"resource": "resource://server/item"},
                "id": "mcp-error-1",
                "type": "tool_call",
            }
        )
    )
    assert result.status == "error"
    assert "explicit mcp_server" in result.content


def test_binding_returns_copy_and_empty_binding_stays_unbound(monkeypatch) -> None:
    module = sys.modules["target_read_resource_overlay"]
    original = ReadResourceTool()
    empty = module.bind_mcp_resource_reader(original, [])
    assert empty is not original
    assert original.resource_reader is None
    assert empty.resource_reader is None

    class FakeReader:
        def __init__(self, names):
            self.names = tuple(names)

        async def read(self, server, uri, offset, limit):
            return {"contents": [{"uri": uri, "text": f"{server}:{offset}:{limit}"}]}

    fake_resources = types.ModuleType("mcp_clients.resources")
    fake_resources.McpResourceReader = FakeReader
    fake_package = types.ModuleType("mcp_clients")
    fake_package.__path__ = []
    monkeypatch.setitem(sys.modules, "mcp_clients", fake_package)
    monkeypatch.setitem(sys.modules, "mcp_clients.resources", fake_resources)

    bound = module.bind_mcp_resource_reader(original, ["platform"])
    assert bound is not original
    assert original.resource_reader is None
    assert bound.resource_reader is not None


def test_attachment_scope_and_legacy_marker_are_preserved(tmp_path: Path) -> None:
    attachment_store.initialize(tmp_path)
    attachment = attachment_store.save(
        session_id="session-1",
        filename="diagram.png",
        mime_type="image/png",
        source="upload",
        stream=BytesIO(b"png"),
        attachment_id="att_image",
    )
    result = ReadResourceTool(
        session_id="session-1",
        allowed_attachment_ids=[attachment["id"]],
        enforce_attachment_allowlist=True,
    ).invoke({"resource": attachment["id"]})

    assert re.search(
        r"^PuddingClaw-Resource-Image:\s*(att_[A-Za-z0-9_-]+)\s*$",
        result,
        re.MULTILINE,
    )


def test_http_urls_remain_owned_by_fetch_url() -> None:
    result = ReadResourceTool(resource_reader=lambda *_: "must not run").invoke(
        {"resource": "https://example.invalid/image.png"}
    )
    assert "use fetch_url" in result
