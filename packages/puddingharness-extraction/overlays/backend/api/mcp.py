"""Generic MCP configuration and discovery API for PuddingHarness."""

from __future__ import annotations

import asyncio
import copy
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from config import _config_path, load_config, save_config

router = APIRouter()

_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}
_MASKED_SECRET = "***"


def _mask_secret(value: Any) -> Any:
    """Keep environment references readable while masking literal secrets."""

    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return value
    return _MASKED_SECRET


def _safe_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return MCP config suitable for the browser, masking secret values."""

    result = copy.deepcopy(config)
    servers = result.get("servers")
    if isinstance(servers, dict):
        for server in servers.values():
            if not isinstance(server, dict):
                continue
            for location in ("headers", "env"):
                values = server.get(location)
                if isinstance(values, dict):
                    server[location] = {
                        str(key): _mask_secret(value) for key, value in values.items()
                    }
    return result


def _mcp_credential_vault_status(config: dict[str, Any]) -> dict[str, Any]:
    """Return one fail-soft status for Vault-backed MCP secrets."""

    references: list[str] = []
    servers = config.get("servers")
    if isinstance(servers, dict):
        for server in servers.values():
            if not isinstance(server, dict):
                continue
            for location in ("headers", "env"):
                values = server.get(location)
                if isinstance(values, dict):
                    references.extend(
                        str(value)
                        for value in values.values()
                        if isinstance(value, str) and value.startswith("vault://")
                    )
    if not references:
        return {"readable": True, "error": ""}
    from provider_registry import LocalCredentialStore

    store = LocalCredentialStore()
    for reference in references:
        status = store.inspect(reference)
        if not status.get("credential_readable", True):
            return {"readable": False, "error": str(status.get("credential_error") or "")}
    return {"readable": True, "error": ""}


class McpConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


def _validate_mcp_config(raw: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Validate editable MCP config and preserve masked credentials."""

    enabled = raw.get("enabled", [])
    if not isinstance(enabled, list) or any(
        not isinstance(name, str) or not name.strip() for name in enabled
    ):
        raise ValueError("mcp.enabled must be an array of server names")
    if len(set(enabled)) != len(enabled):
        raise ValueError("mcp.enabled contains duplicate server names")

    servers = raw.get("servers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp.servers must be an object")
    clean_servers: dict[str, Any] = {}
    current_servers = current.get("servers", {}) if isinstance(current.get("servers"), dict) else {}
    credential_store = None

    def protect(server_name: str, location: str, key: str, value: str, previous: Any) -> str:
        nonlocal credential_store
        if value == _MASKED_SECRET:
            previous_value = str(previous or "")
            if previous_value.startswith(("vault://", "env://", "${")):
                return previous_value
            value = previous_value
            if not value:
                return ""
        if value.startswith(("vault://", "env://", "${")):
            return value
        if credential_store is None:
            from provider_registry import LocalCredentialStore

            credential_store = LocalCredentialStore()
        return credential_store.put(f"mcp:{server_name}:{location}:{key}", value)

    for name, value in servers.items():
        if not isinstance(name, str) or not _SERVER_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid MCP server name: {name}")
        if not isinstance(value, dict):
            raise ValueError(f"MCP server {name} must be an object")
        transport = value.get("transport")
        if transport not in _SUPPORTED_TRANSPORTS:
            raise ValueError(f"MCP server {name} has unsupported transport: {transport}")
        if transport in {"sse", "streamable-http"} and not str(value.get("url") or "").strip():
            raise ValueError(f"MCP server {name} requires url")
        if transport == "stdio" and not str(value.get("command") or "").strip():
            raise ValueError(f"MCP server {name} requires command")
        item = copy.deepcopy(value)
        previous = current_servers.get(name, {})
        previous = previous if isinstance(previous, dict) else {}
        for location in ("headers", "env"):
            values = item.get(location)
            if isinstance(values, dict):
                previous_values = previous.get(location, {})
                previous_values = previous_values if isinstance(previous_values, dict) else {}
                item[location] = {
                    str(key): protect(
                        name,
                        "header" if location == "headers" else location,
                        str(key),
                        secret,
                        previous_values.get(key),
                    )
                    for key, secret in values.items()
                    if str(key).strip() and isinstance(secret, str) and secret.strip()
                }
        clean_servers[name] = item

    return {"enabled": enabled, "servers": clean_servers}


@router.get("/mcp/config")
async def get_mcp_config():
    """Return persisted MCP config and its local source path."""

    config = load_config().get("mcp", {})
    config = config if isinstance(config, dict) else {}
    return {
        "path": str(_config_path()),
        "config": _safe_mcp_config(config),
        "credential_vault": _mcp_credential_vault_status(config),
    }


@router.put("/mcp/config")
async def put_mcp_config(request: McpConfigRequest):
    """Persist MCP enablement and user-defined server definitions."""

    current_config = load_config()
    current_mcp = current_config.get("mcp", {})
    try:
        next_mcp = _validate_mcp_config(
            request.config,
            current_mcp if isinstance(current_mcp, dict) else {},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    current_config["mcp"] = next_mcp
    save_config(current_config)
    try:
        from mcp_clients import invalidate_mcp_tool_cache

        invalidate_mcp_tool_cache()
    except Exception:
        pass
    return {
        "path": str(_config_path()),
        "config": _safe_mcp_config(next_mcp),
        "credential_vault": _mcp_credential_vault_status(next_mcp),
        "status": "saved",
    }


@router.get("/mcp/servers")
async def list_mcp_servers(probe: bool = Query(False)):
    """List configured servers and optionally probe their discovered tools."""

    cfg = load_config()
    mcp_config = cfg.get("mcp", {})
    mcp_config = mcp_config if isinstance(mcp_config, dict) else {}
    custom_servers = mcp_config.get("servers", {})
    custom_servers = custom_servers if isinstance(custom_servers, dict) else {}

    try:
        from mcp_clients.servers import (
            _server_registry,
            effective_mcp_server_names,
            get_mcp_server_display_info,
        )

        enabled = effective_mcp_server_names(mcp_config.get("enabled", []))
        registry_names = list(_server_registry(custom_servers, resolve_secrets=False).keys())
        servers = get_mcp_server_display_info(enabled, custom_servers)
        catalog_names = list(dict.fromkeys([*registry_names, *enabled]))
        catalog = get_mcp_server_display_info(catalog_names, custom_servers)
        enabled_set = set(enabled)
        for item in catalog:
            item["enabled"] = item["key"] in enabled_set
            item["auto_enabled"] = False
            item["managed_by"] = "mcp"
            item["ready"] = True
            item["status"] = "ready" if item["enabled"] else "not_ready"
            item["reason"] = ""
            item["loaded"] = False
            item["tools"] = []
            item["tool_count"] = 0

        if probe:
            from mcp_clients import load_filtered_mcp_tools

            async def probe_server(item: dict[str, Any]) -> None:
                if not item["enabled"] or not item["ready"]:
                    return
                try:
                    tools = await asyncio.wait_for(
                        load_filtered_mcp_tools([item["key"]]),
                        timeout=20,
                    )
                    names = [str(getattr(tool, "name", "")) for tool in tools]
                    item["loaded"] = True
                    item["status"] = "loaded"
                    item["tools"] = names
                    item["tool_count"] = len(names)
                except Exception as exc:
                    item["status"] = "error"
                    item["reason"] = str(exc)[:500]

            await asyncio.gather(*(probe_server(item) for item in catalog))
    except Exception:
        servers = [
            {
                "key": name,
                "name": name,
                "url": "",
                "transport": "",
            }
            for name in mcp_config.get("enabled", [])
            if isinstance(name, str)
        ]
        catalog = [
            {
                **server,
                "enabled": True,
                "auto_enabled": False,
                "managed_by": "mcp",
                "ready": False,
                "loaded": False,
                "status": "error",
                "reason": "runtime status unavailable",
                "tools": [],
                "tool_count": 0,
            }
            for server in servers
        ]

    return {"servers": servers, "catalog": catalog}
