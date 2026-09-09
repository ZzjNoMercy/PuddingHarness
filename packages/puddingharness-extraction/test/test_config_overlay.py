"""Behavior checks for the target-only generic configuration overlay."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


OVERLAY = Path(__file__).parents[1] / "overlays/backend/config.py"


def _load_overlay(tmp_path: Path, monkeypatch):
    name = f"harness_config_{tmp_path.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, OVERLAY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    module.CONFIG_FILE = tmp_path / "config.json"
    return module


def test_target_defaults_and_exports_exclude_business_config(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)
    import provider_registry

    class FakeRegistry:
        def display(self):
            return {"providers": [], "bindings": {}}

    monkeypatch.setattr(provider_registry, "get_provider_registry", lambda: FakeRegistry())

    assert set(module._DEFAULT_CONFIG) == {
        "database",
        "compression",
        "cache",
        "subagents",
        "harness",
        "write_middleware",
        "mcp",
    }
    for removed in (
        "get_rag_config",
        "get_rag_hybrid_config",
        "get_rag_rerank_config",
        "get_vanna_config",
        "get_database_qa_config",
        "get_knowledge_root_config",
        "get_knowledge_mineru_config",
        "get_knowledge_multimodal_index_config",
        "get_llm_wiki_compiler_agent_config",
        "get_llm_wiki_retrieval_config",
        "get_llm_wiki_gbrain_config",
        "get_tool_intent_router_config",
    ):
        assert not hasattr(module, removed), removed

    display = module.get_settings_for_display()
    assert set(display) == {"provider_registry", "database", "compression", "harness", "subagents"}


def test_roundtrip_preserves_user_named_gbrain_and_sparse_updates(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)
    monkeypatch.delenv("PUDDINGCLAW_DATABASE_URL", raising=False)
    monkeypatch.setenv("PUDDINGHARNESS_DATABASE_URL", "postgresql://harness.example/catalog")

    module.update_settings(
        {
            "mcp": {
                "enabled": ["gbrain"],
                "servers": {
                    "gbrain": {
                        "transport": "streamable-http",
                        "url": "https://mcp.example.test",
                        "headers": {"Authorization": "${MCP_TOKEN}"},
                    }
                },
            }
        }
    )
    first = module.load_config()
    assert first["mcp"]["enabled"] == ["gbrain"]
    assert first["mcp"]["servers"]["gbrain"]["url"] == "https://mcp.example.test"
    assert first["database"]["database"] == "puddingharness"
    assert module.get_database_config()["url"] == "postgresql://harness.example/catalog"

    module.update_settings({"compression": {"trigger_count": 33}})
    second = module.load_config()
    assert second["compression"]["trigger_count"] == 33
    assert second["mcp"]["enabled"] == ["gbrain"]
    assert "gbrain" in second["mcp"]["servers"]

    persisted = json.loads(module.CONFIG_FILE.read_text(encoding="utf-8"))
    assert persisted["mcp"]["enabled"] == ["gbrain"]
    assert "gbrain" in persisted["mcp"]["servers"]
    assert "PUDDINGCLAW_DATABASE_URL" not in OVERLAY.read_text(encoding="utf-8")


def test_database_credential_slot_and_sparse_database_update(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)

    class FakeCredentialStore:
        values: dict[str, str] = {}

        def put(self, slot: str, value: str) -> str:
            reference = f"vault://test/{slot}"
            self.values[reference] = value
            return reference

        def get(self, reference: str) -> str:
            return self.values[reference]

        def inspect(self, reference: str) -> dict[str, object]:
            return {
                "credential_configured": reference in self.values,
                "credential_readable": reference in self.values,
                "credential_error": "",
            }

    import provider_registry

    monkeypatch.setattr(provider_registry, "LocalCredentialStore", FakeCredentialStore)
    module.update_settings(
        {
            "database": {
                "provider": "postgresql",
                "source": "external",
                "host": "db.example",
                "password": "secret-value",
            }
        }
    )
    persisted = json.loads(module.CONFIG_FILE.read_text(encoding="utf-8"))
    assert persisted["database"]["password_ref"] == "vault://test/database-config"
    assert "password" not in persisted["database"]

    module.update_settings({"database": {"port": 6543}})
    database = module.get_database_config()
    assert database["host"] == "db.example"
    assert database["port"] == 6543
    assert database["password"] == "secret-value"


def test_business_updates_are_rejected_without_compatibility_shims(tmp_path, monkeypatch) -> None:
    module = _load_overlay(tmp_path, monkeypatch)
    for key in ("knowledge", "rag", "vanna", "analytics", "tool_intent_router"):
        try:
            module.update_settings({key: {}})
        except ValueError as exc:
            assert key in str(exc)
        else:
            raise AssertionError(f"{key} must not be accepted by target config")


def test_direct_save_cannot_persist_rejected_business_config(tmp_path, monkeypatch):
    import pytest
    module = _load_overlay(tmp_path, monkeypatch)
    module.save_config(module.load_config())
    original = module.CONFIG_FILE.read_bytes()
    with pytest.raises(ValueError, match='Unknown settings'):
        module.save_config({'knowledge': {'enabled': True}})
    assert module.CONFIG_FILE.read_bytes() == original
    assert 'knowledge' not in module.load_config()


def test_api_registry_and_config_share_real_mcp_roundtrip(tmp_path, monkeypatch):
    import asyncio

    config = _load_overlay(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, 'config', config)
    root = OVERLAY.parent

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    registry = load('mcp_clients.servers', root / 'mcp_clients/servers.py')
    load('mcp_clients', root / 'mcp_clients/__init__.py')
    api = load('target_config_mcp_api', root / 'api/mcp.py')
    submitted = {'enabled': ['gbrain'], 'servers': {'gbrain': {
        'transport': 'streamable-http', 'url': 'https://mcp.example.test',
        'headers': {'Authorization': '${EXTERNAL_MCP_TOKEN}'}}}}
    monkeypatch.setenv('EXTERNAL_MCP_TOKEN', 'test-only-token')
    result = asyncio.run(api.put_mcp_config(api.McpConfigRequest(config=submitted)))
    assert result['status'] == 'saved'
    assert config.load_config()['mcp'] == submitted
    discovered = asyncio.run(api.list_mcp_servers(probe=False))
    assert discovered['catalog'][0]['key'] == 'gbrain'
    assert discovered['catalog'][0]['managed_by'] == 'mcp'
    assert registry.build_mcp_servers_config(['gbrain'])['gbrain']['headers'] == {
        'Authorization': 'test-only-token'}
    assert 'test-only-token' not in config.CONFIG_FILE.read_text()
