"""Behavior checks for the target-only generic MCP server overlay."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


OVERLAY = Path(__file__).parents[1] / "overlays/backend/mcp_clients/servers.py"


def _load_overlay(tmp_path: Path, monkeypatch):
    module_name = f"fake_harness_mcp_servers_{tmp_path.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, OVERLAY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_overlay_has_no_code_owned_servers_or_business_filter(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)

    assert module._REGISTRY == {}
    assert module._SERVER_DISPLAY_NAMES == {}
    assert not hasattr(module, "_GBRAIN_ALLOWED_TOOLS")
    assert module.allowed_mcp_tool_names("gbrain") is None
    tools = [SimpleNamespace(name="gbrain_admin"), SimpleNamespace(name="gbrain_query")]
    assert module.filter_mcp_tools("gbrain", tools) == tools


def test_user_named_gbrain_uses_normal_config_and_discovery_path(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)
    monkeypatch.setenv("MCP_TOKEN", "env-secret")
    configured = {
        "gbrain": {
            "transport": "streamable-http",
            "url": "https://mcp.example.test",
            "name": "User supplied MCP",
            "headers": {"Authorization": "${MCP_TOKEN}"},
        }
    }

    config = module.build_mcp_servers_config(["gbrain"], configured)

    assert config == {
        "gbrain": {
            "transport": "streamable-http",
            "url": "https://mcp.example.test",
            "headers": {"Authorization": "env-secret"},
        }
    }
    assert (
        module._server_registry(configured, resolve_secrets=False)["gbrain"]["headers"]["Authorization"]
        == "${MCP_TOKEN}"
    )
    assert module.effective_mcp_server_names(["gbrain"]) == ["gbrain"]
    assert not hasattr(module, "gbrain_runtime_status")


def test_environment_and_vault_values_resolve_only_for_runtime_config(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)
    monkeypatch.setenv("MCP_TOKEN", "env-secret")
    fake_provider_registry = types.ModuleType("provider_registry")

    class FakeCredentialStore:
        def get(self, ref: str) -> str:
            assert ref == "vault://users/local/credentials/mcp"
            return "vault-secret"

    fake_provider_registry.LocalCredentialStore = FakeCredentialStore
    monkeypatch.setitem(sys.modules, "provider_registry", fake_provider_registry)
    configured = {
        "private": {
            "transport": "sse",
            "url": "https://mcp.example.test/sse",
            "headers": {
                "Authorization": "${MCP_TOKEN}",
                "X-Secret": "vault://users/local/credentials/mcp",
            },
        }
    }

    display = module.get_mcp_server_display_info(["private"], configured)
    runtime = module.build_mcp_servers_config(["private"], configured)

    assert display == [
        {
            "key": "private",
            "name": "private",
            "url": "https://mcp.example.test/sse",
            "transport": "sse",
        }
    ]
    assert runtime["private"]["headers"] == {
        "Authorization": "env-secret",
        "X-Secret": "vault-secret",
    }
    assert configured["private"]["headers"]["Authorization"] == "${MCP_TOKEN}"


def test_enabled_names_and_display_metadata_keep_user_names(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)
    configured = {
        "private": {
            "transport": "stdio",
            "command": "private-mcp",
            "name": "Private MCP",
        },
        "invalid": {"transport": "stdio"},
    }

    assert module.effective_mcp_server_names(["private", "private", "gbrain"]) == ["private", "gbrain"]
    assert list(module.build_mcp_servers_config(custom_servers=configured)) == ["private"]
    assert module.get_mcp_server_display_info(["private", "invalid"], configured) == [
        {
            "key": "private",
            "name": "Private MCP",
            "url": "",
            "transport": "stdio",
        }
    ]
