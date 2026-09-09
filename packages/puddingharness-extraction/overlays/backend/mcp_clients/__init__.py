"""Generic MCP client factory and discovery cache for PuddingHarness."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from mcp_clients.servers import build_mcp_servers_config, filter_mcp_tools

_MAX_DISCOVERY_CACHE_ENTRIES = 8
_discovery_cache: OrderedDict[tuple[int, str], tuple[BaseTool, ...]] = OrderedDict()
_discovery_inflight: dict[tuple[int, int, str], asyncio.Task[tuple[BaseTool, ...]]] = {}
_discovery_cache_generation = 0
_discovery_cache_lock = threading.Lock()


def create_mcp_client(enabled_names: list[str] | None = None) -> MultiServerMCPClient:
    """Create a client for the explicitly enabled user MCP servers."""

    cfg = build_mcp_servers_config(enabled_names)
    if not cfg:
        raise ValueError("No MCP servers enabled or configured")
    return MultiServerMCPClient(cfg)


def _file_signature(path: Path) -> dict[str, Any]:
    """Return cheap metadata for an absolute local command executable."""

    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def _runtime_signatures(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Capture generic local command files that affect server discovery."""

    signatures: list[dict[str, Any]] = []
    for server_name, server in cfg.items():
        if not isinstance(server, dict):
            continue
        command = str(server.get("command") or "").strip()
        if command and Path(command).is_absolute():
            signatures.append(
                {
                    "server": server_name,
                    "kind": "command",
                    **_file_signature(Path(command)),
                }
            )
    return signatures


def _discovery_fingerprint(cfg: dict[str, Any]) -> str:
    """Hash effective connection data without retaining or logging secrets."""

    payload = {"servers": cfg, "runtime_files": _runtime_signatures(cfg)}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def invalidate_mcp_tool_cache() -> None:
    """Invalidate cached discovery metadata for subsequent agent builds."""

    global _discovery_cache_generation
    with _discovery_cache_lock:
        _discovery_cache_generation += 1
        _discovery_cache.clear()


async def _discover_filtered_tools(
    cfg: dict[str, Any],
    *,
    cache_key: tuple[int, str],
    inflight_key: tuple[int, int, str],
) -> tuple[BaseTool, ...]:
    """Run one MCP discovery and publish its immutable result to the cache."""

    current_task = asyncio.current_task()
    try:
        client = MultiServerMCPClient(cfg, tool_name_prefix=True)
        result: list[BaseTool] = []
        for server_name in cfg:
            discovered = await client.get_tools(server_name=server_name)
            result.extend(filter_mcp_tools(server_name, discovered))
        cached = tuple(result)
        with _discovery_cache_lock:
            _discovery_cache[cache_key] = cached
            _discovery_cache.move_to_end(cache_key)
            while len(_discovery_cache) > _MAX_DISCOVERY_CACHE_ENTRIES:
                _discovery_cache.popitem(last=False)
        return cached
    finally:
        with _discovery_cache_lock:
            if _discovery_inflight.get(inflight_key) is current_task:
                _discovery_inflight.pop(inflight_key, None)


async def load_filtered_mcp_tools(
    enabled_names: list[str],
    *,
    force_refresh: bool = False,
) -> list[BaseTool]:
    """Discover and cache all tools from explicitly enabled MCP servers."""

    cfg = build_mcp_servers_config(enabled_names)
    if not cfg:
        return []
    fingerprint = _discovery_fingerprint(cfg)
    loop = asyncio.get_running_loop()
    with _discovery_cache_lock:
        generation = _discovery_cache_generation
        cache_key = (generation, fingerprint)
        if not force_refresh:
            cached = _discovery_cache.get(cache_key)
            if cached is not None:
                _discovery_cache.move_to_end(cache_key)
                return list(cached)
        inflight_key = (id(loop), generation, fingerprint)
        task = _discovery_inflight.get(inflight_key)
        if task is None or task.done():
            task = loop.create_task(
                _discover_filtered_tools(
                    cfg,
                    cache_key=cache_key,
                    inflight_key=inflight_key,
                ),
                name=f"mcp-discovery-{fingerprint[:12]}",
            )
            _discovery_inflight[inflight_key] = task
    return list(await asyncio.shield(task))


__all__ = [
    "create_mcp_client",
    "invalidate_mcp_tool_cache",
    "load_filtered_mcp_tools",
]
