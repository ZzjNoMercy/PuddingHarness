"""Behavior checks for the target-only generic MCP client/cache overlay."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


OVERLAY = Path(__file__).parents[1] / "overlays/backend/mcp_clients/__init__.py"


def _load_overlay(tmp_path: Path, monkeypatch):
    package_name = f"fake_harness_mcp_client_{tmp_path.name.replace('-', '_')}"
    fake_package = types.ModuleType("mcp_clients")
    fake_package.__path__ = []
    fake_servers = types.ModuleType("mcp_clients.servers")
    fake_servers.build_mcp_servers_config = lambda _names: {
        "gbrain": {
            "transport": "stdio",
            "command": "user-mcp",
            "args": ["serve"],
        }
    }
    fake_servers.filter_mcp_tools = lambda _server, tools: list(tools)
    fake_package.servers = fake_servers
    monkeypatch.setitem(sys.modules, "mcp_clients", fake_package)
    monkeypatch.setitem(sys.modules, "mcp_clients.servers", fake_servers)

    spec = importlib.util.spec_from_file_location(package_name, OVERLAY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package_name, module)
    spec.loader.exec_module(module)
    return module


def test_user_named_gbrain_uses_generic_discovery_and_cache(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)

    class FakeMCPClient:
        calls = 0

        def __init__(self, _config, *, tool_name_prefix=False):
            assert tool_name_prefix is True

        async def get_tools(self, *, server_name: str):
            type(self).calls += 1
            return [
                SimpleNamespace(name=f"{server_name}_query"),
                SimpleNamespace(name=f"{server_name}_admin"),
            ]

    monkeypatch.setattr(module, "MultiServerMCPClient", FakeMCPClient)
    module.invalidate_mcp_tool_cache()

    async def run() -> None:
        first = await module.load_filtered_mcp_tools(["gbrain"])
        second = await module.load_filtered_mcp_tools(["gbrain"])
        assert [tool.name for tool in first] == ["gbrain_query", "gbrain_admin"]
        assert [tool.name for tool in second] == ["gbrain_query", "gbrain_admin"]

    asyncio.run(run())
    assert FakeMCPClient.calls == 1


def test_cache_runtime_signature_has_no_product_specific_files(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)
    cfg = {
        "gbrain": {
            "transport": "stdio",
            "command": "user-mcp",
            "env": {
                "GBRAIN_HOME": str(tmp_path / "home"),
                "GBRAIN_SCHEMA_PACK": "custom-pack",
            },
        }
    }

    assert module._runtime_signatures(cfg) == []
    assert "GBRAIN_HOME" not in OVERLAY.read_text(encoding="utf-8")
    assert "GBRAIN_SCHEMA_PACK" not in OVERLAY.read_text(encoding="utf-8")
