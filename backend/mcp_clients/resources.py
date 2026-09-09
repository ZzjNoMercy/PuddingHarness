"""Async, explicitly bound MCP Resource reads for PuddingHarness.

This module owns only the generic MCP client boundary.  It never resolves a
URI to a server implicitly and it does not retain the resolved MCP headers or
other credential material in the binding.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from langchain_mcp_adapters.client import MultiServerMCPClient

from mcp_clients.servers import build_mcp_servers_config

MAX_MCP_RESOURCE_BYTES = 8 * 1024 * 1024
DEFAULT_MCP_RESOURCE_TIMEOUT_SECONDS = 15.0


class McpResourceReadError(RuntimeError):
    """A bounded MCP Resource read failed or returned an invalid result."""


McpClientFactory = Callable[[Mapping[str, Any]], MultiServerMCPClient]


def _enabled_server_names(enabled_names: Sequence[str]) -> tuple[str, ...]:
    if isinstance(enabled_names, (str, bytes)):
        raise TypeError("enabled MCP server names must be a sequence")
    normalized = tuple(dict.fromkeys(str(name).strip() for name in enabled_names if str(name).strip()))
    return normalized


def _content_item(item: object) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    result: dict[str, Any] = {}
    for field in ("uri", "mimeType", "text", "blob"):
        value = getattr(item, field, None)
        if value is not None:
            result[field] = value
    return result


def _normalize_result(result: object, *, uri: str) -> dict[str, Any]:
    if isinstance(result, Mapping):
        contents = result.get("contents")
        structured = result.get("structuredContent")
    else:
        contents = getattr(result, "contents", None)
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, Mapping) and structured.get("status") == "error":
        raise McpResourceReadError(
            "MCP Resource server rejected the read: "
            + json.dumps(dict(structured), ensure_ascii=False, sort_keys=True, default=str)
        )
    if not isinstance(contents, Sequence) or isinstance(contents, (str, bytes, bytearray)) or not contents:
        raise McpResourceReadError("MCP Resource returned no content")

    normalized: list[dict[str, Any]] = []
    total_bytes = 0
    for raw_item in contents:
        item = _content_item(raw_item)
        if str(item.get("uri") or "") != uri:
            raise McpResourceReadError("MCP Resource returned content for a different URI")
        if isinstance(item.get("text"), str):
            total_bytes += len(item["text"].encode("utf-8"))
        elif isinstance(item.get("blob"), str):
            # Keep the MCP blob encoded; never materialize it as a host file.
            encoded = item["blob"]
            # Reject an obviously oversized payload before base64 decoding it.
            if len(encoded) > ((MAX_MCP_RESOURCE_BYTES + 2) * 4 // 3 + 4):
                raise McpResourceReadError("MCP Resource exceeds the local size limit")
            try:
                total_bytes += len(base64.b64decode(encoded, validate=True))
            except (ValueError, TypeError):
                raise McpResourceReadError("MCP Resource returned an invalid blob") from None
        else:
            raise McpResourceReadError("MCP Resource content has no text or blob")
        if total_bytes > MAX_MCP_RESOURCE_BYTES:
            raise McpResourceReadError("MCP Resource exceeds the local size limit")
        normalized.append(item)
    if total_bytes == 0:
        raise McpResourceReadError("MCP Resource returned empty content")
    result_payload: dict[str, Any] = {"contents": normalized}
    if structured is not None:
        result_payload["structuredContent"] = structured
    return result_payload


class McpResourceReader:
    """Read resources only through an explicitly enabled MCP server."""

    def __init__(
        self,
        enabled_names: Sequence[str],
        *,
        client_factory: McpClientFactory | None = None,
        timeout_seconds: float = DEFAULT_MCP_RESOURCE_TIMEOUT_SECONDS,
    ) -> None:
        if client_factory is not None and not callable(client_factory):
            raise TypeError("MCP client factory must be callable")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise TypeError("MCP Resource timeout must be numeric")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("MCP Resource timeout is out of range")
        normalized_names = _enabled_server_names(enabled_names)
        # Snapshot only names.  The config may contain credentials and is
        # intentionally discarded after determining which names are present.
        try:
            configured = build_mcp_servers_config(list(normalized_names))
        except Exception:
            configured = {}
        self._enabled_names = frozenset(normalized_names)
        self._configured_names = frozenset(
            name for name in normalized_names if name in configured
        )
        # Construct the client at binding time.  Reads therefore use the host's
        # enabled-server snapshot and cannot silently pick up a later config
        # mutation.  The client/config stay private and are intentionally not
        # included in repr/log output because they may contain credentials.
        self._client = None
        if self._configured_names:
            try:
                # Keep exactly this host snapshot for the lifetime of the
                # binding.  The default path constructs the SDK client from it
                # directly, avoiding a second mutable config lookup.
                snapshot = copy.deepcopy(
                    {
                        name: configured[name]
                        for name in sorted(self._configured_names)
                    }
                )
                self._client = (
                    client_factory(snapshot)
                    if client_factory is not None
                    else MultiServerMCPClient(snapshot)
                )
            except Exception:
                # A stale/invalid host binding must fail closed at read time,
                # without preventing ordinary agents from starting.
                self._client = None
        self._timeout_seconds = float(timeout_seconds)

    @property
    def enabled_names(self) -> frozenset[str]:
        return self._enabled_names

    def __repr__(self) -> str:
        return (
            f"McpResourceReader(enabled_names={sorted(self._enabled_names)!r}, "
            f"timeout_seconds={self._timeout_seconds!r})"
        )

    async def read(self, server_name: str, uri: str, offset: int, limit: int) -> dict[str, Any]:
        del offset, limit
        if (
            not isinstance(server_name, str)
            or server_name not in self._enabled_names
            or server_name not in self._configured_names
            or self._client is None
        ):
            raise McpResourceReadError("MCP Resource server is not explicitly enabled")
        try:
            uri_scheme = urlsplit(uri).scheme if isinstance(uri, str) else ""
        except ValueError:
            uri_scheme = ""
        if not isinstance(uri, str) or not uri.strip() or not uri_scheme:
            raise McpResourceReadError("MCP Resource URI is invalid")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._client.session(server_name) as session:
                    result = await session.read_resource(uri)
        except McpResourceReadError:
            raise
        except TimeoutError:
            raise McpResourceReadError("MCP Resource read timed out") from None
        except Exception as exc:
            raise McpResourceReadError("MCP Resource connection failed") from exc
        return _normalize_result(result, uri=uri)


__all__ = [
    "DEFAULT_MCP_RESOURCE_TIMEOUT_SECONDS",
    "MAX_MCP_RESOURCE_BYTES",
    "McpResourceReadError",
    "McpResourceReader",
]
