"""Tests for the target-only MCP Resource client boundary."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from langchain_mcp_adapters.client import MultiServerMCPClient


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
OVERLAY = REPOSITORY_ROOT / (
    "packages/puddingharness-extraction/overlays/backend/mcp_clients/resources.py"
)


def _load_overlay(monkeypatch):
    """Load the target module without replacing the rest of the mcp_clients package."""

    package = types.ModuleType("mcp_clients")
    package.__path__ = []
    package.create_mcp_client = lambda _names: None
    servers = types.ModuleType("mcp_clients.servers")
    servers.build_mcp_servers_config = lambda names: {
        name: {"transport": "streamable-http", "url": "http://mcp.test/mcp"}
        for name in names
    }
    package.servers = servers
    monkeypatch.setitem(sys.modules, "mcp_clients", package)
    monkeypatch.setitem(sys.modules, "mcp_clients.servers", servers)

    spec = importlib.util.spec_from_file_location("mcp_clients.resources", OVERLAY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


class _Mcp:
    async def read_resource(self, *, resource_uri, principal, correlation, start, end):
        del principal, correlation, start, end
        if resource_uri == "platform://resource/ok":
            return {
                "contents": [
                    {
                        "uri": resource_uri,
                        "mimeType": "text/plain",
                        "text": "line one\nline two",
                    }
                ]
            }
        return {
            "contents": [],
            "structuredContent": {
                "status": "error",
                "error": {"code": "not_found", "message": "unknown URI"},
            },
        }


def _platform_app() -> FastAPI:
    # Platform is deliberately imported only by this test fixture.  The target
    # runtime module above must remain independent of Knowledge Platform code.
    from knowledge_contracts import Correlation, Principal
    from knowledge_platform.transport import create_mcp_router

    app = FastAPI()
    app.include_router(
        create_mcp_router(
            _Mcp(),
            principal_provider=lambda: Principal("harness-test", ("mcp:read",)),
            correlation_provider=lambda: Correlation("harness-mcp"),
        )
    )
    return app


def _sdk_factory(app: FastAPI):
    def httpx_client_factory(*, headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://mcp.test",
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    def factory(_names):
        return MultiServerMCPClient(
            {
                "platform": {
                    "transport": "streamable-http",
                    "url": "http://mcp.test/mcp",
                    "httpx_client_factory": httpx_client_factory,
                }
            }
        )

    return factory


@pytest.mark.asyncio
async def test_real_sdk_asgi_resource_read_and_structured_error(monkeypatch) -> None:
    module = _load_overlay(monkeypatch)
    reader = module.McpResourceReader(
        ["platform"], client_factory=_sdk_factory(_platform_app())
    )

    result = await reader.read("platform", "platform://resource/ok", 0, 2000)
    assert result["contents"][0]["text"] == "line one\nline two"

    with pytest.raises(module.McpResourceReadError, match="not_found"):
        await reader.read("platform", "platform://resource/missing", 0, 2000)


@pytest.mark.asyncio
async def test_reader_requires_explicit_enabled_server_and_does_not_broadcast(monkeypatch) -> None:
    module = _load_overlay(monkeypatch)
    calls: list[list[str]] = []

    def factory(names):
        calls.append(list(names))
        return object()

    reader = module.McpResourceReader(["platform"], client_factory=factory)
    with pytest.raises(module.McpResourceReadError, match="not explicitly enabled"):
        await reader.read("other", "platform://resource/ok", 0, 2000)
    assert calls == [["platform"]]


def test_binding_validation_does_not_retain_secret_config(monkeypatch) -> None:
    module = _load_overlay(monkeypatch)
    secret = "do-not-retain-this-token"
    monkeypatch.setattr(
        module,
        "build_mcp_servers_config",
        lambda _names: {
            "platform": {
                "transport": "streamable-http",
                "headers": {"Authorization": f"Bearer {secret}"},
            }
        },
    )
    reader = module.McpResourceReader(["platform"], client_factory=lambda _snapshot: object())
    assert reader.enabled_names == frozenset({"platform"})
    assert secret not in repr(reader)


def test_binding_deep_copies_nested_config_snapshot(monkeypatch) -> None:
    module = _load_overlay(monkeypatch)
    source = {
        "platform": {
            "transport": "streamable-http",
            "headers": {"Authorization": "Bearer original"},
            "options": {"nested": ["original"]},
        }
    }
    monkeypatch.setattr(module, "build_mcp_servers_config", lambda _names: source)
    captured = {}

    def factory(snapshot):
        captured["snapshot"] = snapshot
        return object()

    reader = module.McpResourceReader(
        ["platform"], client_factory=factory
    )

    source["platform"]["headers"]["Authorization"] = "Bearer mutated"
    source["platform"]["options"]["nested"][0] = "mutated"

    assert captured["snapshot"]["platform"]["headers"]["Authorization"] == "Bearer original"
    assert captured["snapshot"]["platform"]["options"]["nested"] == ["original"]
    assert reader.enabled_names == frozenset({"platform"})


@pytest.mark.asyncio
async def test_empty_and_oversized_content_fail_closed(monkeypatch) -> None:
    module = _load_overlay(monkeypatch)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def read_resource(self, _uri):
            return {"contents": []}

    class Client:
        def session(self, _name):
            return Session()

    reader = module.McpResourceReader(["platform"], client_factory=lambda _names: Client())
    with pytest.raises(module.McpResourceReadError, match="no content"):
        await reader.read("platform", "platform://resource/empty", 0, 2000)
