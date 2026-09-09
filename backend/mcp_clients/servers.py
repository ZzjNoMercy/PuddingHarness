"""Generic MCP server registry for the PuddingHarness extraction target.

The legacy PuddingClaw module owns a few product-specific servers and their
runtime policy.  This target overlay keeps only the user MCP configuration
boundary: validation, environment/Vault reference resolution, display data,
enabled-name normalization, and unfiltered discovery results.

This is an extraction overlay.  It does not modify PuddingClaw source or
assert that the target repository is ready to build or release.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any


def _get_env(name: str, default: str = "") -> str:
    """Read an environment variable without retaining a process secret."""

    return os.getenv(name, default)


# The target has no code-owned server registry.  Every server comes from the
# caller's MCP configuration, so a user may use any valid name, including a
# name that was special-cased by the legacy product.
_REGISTRY: dict[str, Any] = {}
_SERVER_DISPLAY_NAMES: dict[str, str] = {}

_CUSTOM_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SUPPORTED_TRANSPORTS = {"stdio", "sse", "streamable-http"}


def _configured_servers() -> dict[str, Any]:
    """Load user-defined servers without importing config at module import."""

    try:
        from config import load_config

        servers = load_config().get("mcp", {}).get("servers", {})
    except Exception:
        return {}
    return servers if isinstance(servers, dict) else {}


def _server_registry(
    custom_servers: dict[str, Any] | None = None,
    *,
    resolve_secrets: bool = True,
) -> dict[str, Any]:
    """Return validated user MCP descriptors, optionally resolving secrets."""

    registry: dict[str, Any] = {}
    for name, value in (custom_servers if custom_servers is not None else _configured_servers()).items():
        if not isinstance(name, str) or not _CUSTOM_SERVER_NAME_RE.fullmatch(name):
            continue
        if not isinstance(value, dict) or value.get("transport") not in _SUPPORTED_TRANSPORTS:
            continue
        transport = value.get("transport")
        if transport in {"sse", "streamable-http"} and not str(value.get("url") or "").strip():
            continue
        if transport == "stdio" and not str(value.get("command") or "").strip():
            continue
        registry[name] = (
            _resolve_environment_values(copy.deepcopy(value))
            if resolve_secrets
            else copy.deepcopy(value)
        )
    return registry


def _resolve_environment_values(value: Any) -> Any:
    """Resolve recursive ``${ENV_NAME}`` and ``vault://`` references."""

    if isinstance(value, dict):
        return {key: _resolve_environment_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_environment_values(item) for item in value]
    if isinstance(value, str) and value.startswith("vault://"):
        from provider_registry import LocalCredentialStore

        return LocalCredentialStore().get(value)
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return _get_env(value[2:-1])
    return value


def get_mcp_server_display_info(
    enabled_names: list[str],
    custom_servers: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return secret-free display metadata for known enabled servers."""

    result: list[dict[str, str]] = []
    registry = _server_registry(custom_servers, resolve_secrets=False)
    for name in enabled_names:
        cfg = registry.get(name)
        if not cfg:
            continue
        result.append(
            {
                "key": name,
                "name": cfg.get("name") or _SERVER_DISPLAY_NAMES.get(name, name),
                "url": cfg.get("url", ""),
                "transport": cfg.get("transport", ""),
            }
        )
    return result


def effective_mcp_server_names(enabled_names: list[str] | None) -> list[str]:
    """Normalize the caller's enabled list without automatic server injection."""
    return list(
        dict.fromkeys(
            str(name)
            for name in (enabled_names or [])
            if str(name)
        )
    )


def build_mcp_servers_config(
    enabled_names: list[str] | None = None,
    custom_servers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build validated MCP client descriptors for all or selected servers."""

    registry = _server_registry(custom_servers)
    if enabled_names is not None:
        enabled = set(enabled_names)
        registry = {name: cfg for name, cfg in registry.items() if name in enabled}

    return {
        name: {field: value for field, value in cfg.items() if field != "name"}
        for name, cfg in registry.items()
    }


def allowed_mcp_tool_names(server_name: str) -> frozenset[str] | None:
    """Return no local tool filter; authorization belongs to the MCP server."""

    del server_name
    return None


def filter_mcp_tools(server_name: str, tools: list[Any]) -> list[Any]:
    """Preserve every tool returned by an explicitly configured MCP server."""

    del server_name
    return list(tools)


__all__ = [
    "allowed_mcp_tool_names",
    "build_mcp_servers_config",
    "effective_mcp_server_names",
    "filter_mcp_tools",
    "get_mcp_server_display_info",
]
