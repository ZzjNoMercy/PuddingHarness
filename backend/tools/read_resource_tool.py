"""Generic Harness resource reader.

This extraction overlay keeps the small, framework-neutral part of the legacy
tool: attachment/image resources and explicitly scoped local Workspace files.
The legacy attachment and host-read modules are generic Harness contracts and
remain wired for compatibility with existing manager/middleware call sites.
An MCP Resource reader is optional and can be supplied by a target runtime.
No Platform-specific virtual directory is resolved here.
"""

from __future__ import annotations

import json
import inspect
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Awaitable
from urllib.parse import urlsplit

from langchain_core.tools import BaseTool, ToolException
from pydantic import BaseModel, Field

from graph.attachment_store import attachment_store
from graph.host_read_policy import is_sensitive_host_read_path
from graph.managed_paths import is_managed_resource_path
from graph.session_manager import session_manager

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MAX_MCP_RESOURCE_TEXT_BYTES = 8 * 1024 * 1024


def _raise_mcp_tool_error(message: str) -> None:
    """Raise a stable, parent-visible MCP error while preserving local strings."""

    raise ToolException(message if message.startswith("❌") else f"❌ {message}")

AttachmentReader = Callable[[str, str], Mapping[str, Any] | None]
PathAuthorizer = Callable[[Path, str, str], bool]
McpResourceReader = Callable[[str, str, int, int], Awaitable[Any]]


class ReadResourceInput(BaseModel):
    resource: str = Field(
        description=(
            "Attachment id (att_xxx), an absolute/home-relative image path, a /workspace image path, "
            "or an authorized MCP Resource URI. Ordinary text paths use read_file; HTTP(S) URLs use fetch_url."
        )
    )
    offset: int = Field(default=0, ge=0, description="Zero-based line/entry offset")
    limit: int = Field(default=2000, ge=1, le=2000, description="Maximum resource lines/entries to return")
    mcp_server: str | None = Field(
        default=None,
        description="Explicit enabled MCP server for a Resource read; URI-to-server discovery is disabled.",
    )


class ReadResourceTool(BaseTool):
    """Read an explicitly scoped local or externally discovered resource.

    Adapter arguments are intentionally explicit.  The target Harness can
    bind its own MCP client and attachment store without importing legacy
    runtime singletons.
    """

    name: str = "read_resource"
    description: str = (
        "Open an attachment, a local image in the current Workspace, or a resource URI "
        "provided by an authorized MCP Resource adapter. This is not a general filesystem reader."
    )
    args_schema: type[BaseModel] = ReadResourceInput
    risk_level: str = "moderate"
    # MCP failures must be surfaced as an error ToolMessage to the parent
    # graph; local attachment/image compatibility paths keep their historical
    # string responses.
    handle_tool_error: bool = True
    session_id: str = ""
    run_id: str = ""
    workspace_path: str = ""
    backend_mode: str = "kernel"
    approval_mode: str = "strict"
    allowed_attachment_ids: list[str] = Field(default_factory=list, exclude=True)
    enforce_attachment_allowlist: bool = Field(default=False, exclude=True)
    attachment_reader: AttachmentReader | None = Field(default=None, exclude=True, repr=False)
    path_authorizer: PathAuthorizer | None = Field(default=None, exclude=True, repr=False)
    resource_reader: McpResourceReader | None = Field(default=None, exclude=True, repr=False)

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _read_text_page(path: Path, *, offset: int, limit: int) -> str:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset : offset + limit]
        content = "\n".join(selected)
        if offset + limit < len(lines):
            content += (
                f"\n\n[Showing lines {offset + 1}-{offset + len(selected)} of {len(lines)}. "
                f"Continue with offset={offset + limit}.]"
            )
        return content

    def _read_attachment(self, attachment_id: str, *, offset: int = 0, limit: int = 2000) -> str:
        try:
            item = (
                self.attachment_reader(self.session_id, attachment_id)
                if self.attachment_reader is not None
                else attachment_store.get(self.session_id, attachment_id)
            )
        except Exception as exc:
            return f"❌ Attachment read failed: {exc}"
        if not item:
            return f"❌ Attachment not found: {attachment_id}"

        if self.enforce_attachment_allowlist:
            explicitly_allowed = attachment_id in set(self.allowed_attachment_ids)
            generated_by_current_run = (
                str(item.get("source") or "") == "generated"
                and bool(self.run_id)
                and str(item.get("created_by_run_id") or "") == self.run_id
            )
            if not explicitly_allowed and not generated_by_current_run:
                return "❌ Attachment is outside this image-analysis delegation."

        attachment_type = str(item.get("type") or "file")
        if attachment_type == "image":
            return (
                f"Attachment: {item.get('name')}\n"
                "Type: image\n"
                f"Size: {item.get('size')} bytes\n"
                f"PuddingClaw-Resource-Image: {attachment_id}\n\n"
                "The image resource has been opened for the image_analyzer subagent. Continue with visual analysis."
            )
        if attachment_type in {"pdf", "document"}:
            return (
                f"Attachment {attachment_id} is {attachment_type}. Text extraction for this type is not enabled. "
                "Treat it as a user-facing output attachment."
            )

        path = Path(str(item.get("path") or ""))
        if not path.is_file():
            return f"❌ Attachment file missing: {attachment_id}"
        try:
            content = self._read_text_page(path, offset=offset, limit=limit)
        except Exception as exc:
            return f"❌ Error reading attachment: {exc}"
        return (
            f"Attachment: {item.get('name')}\n"
            f"Type: {attachment_type}\n"
            f"Size: {item.get('size')} bytes\n\n"
            f"{content}"
        )

    def _workspace_image_path(self, value: str) -> Path | None:
        if not value.startswith("/workspace/") or not self.workspace_path:
            return None
        workspace = Path(self.workspace_path).expanduser().resolve()
        candidate = (workspace / value.removeprefix("/workspace/").lstrip("/")).resolve()
        return candidate if self._is_relative_to(candidate, workspace) else None

    def _path_is_allowed(self, path: Path) -> bool:
        workspace = Path(self.workspace_path).expanduser().resolve() if self.workspace_path else None
        if workspace is not None and self._is_relative_to(path, workspace):
            return True
        if self.path_authorizer is not None:
            try:
                return bool(self.path_authorizer(path, self.session_id, self.run_id))
            except Exception:
                return False

        smart_ordinary_read = (
            self.approval_mode == "smart"
            and self.backend_mode in {"spawn", "kernel"}
            and not is_sensitive_host_read_path(path)
        )
        unrestricted_spawn_read = self.backend_mode == "spawn" and self.approval_mode != "smart"
        return (
            unrestricted_spawn_read
            or smart_ordinary_read
            or is_managed_resource_path(path, Path(__file__).resolve().parent.parent)
            or (
                not is_managed_resource_path(path, Path(__file__).resolve().parent.parent)
                and session_manager.has_external_path_read_permission(
                    self.session_id,
                    path,
                    run_id=self.run_id,
                )
            )
        )

    def _read_image_path_marker(self, value: str) -> str:
        raw_path = self._workspace_image_path(value)
        path = raw_path or Path(value).expanduser().resolve()
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            return "❌ read_resource only accepts image resources and attachment refs (`att_xxx`)."
        if not path.exists():
            return f"❌ File not found: {path}"
        if not path.is_file():
            return f"❌ Not a file: {path}"
        if not self._path_is_allowed(path):
            return "🔒 Permission required: this file is outside the current workspace.\n" f"Path: {path}"
        return (
            f"Local resource: {path}\n"
            "Type: image\n"
            f"Size: {path.stat().st_size} bytes\n"
            f"PuddingClaw-Resource-Image-Path: {path}\n\n"
            "The image resource has been opened for the image_analyzer subagent. Continue with visual analysis."
        )

    @staticmethod
    def _resource_text(payload: Any) -> str:
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            return payload
        if isinstance(payload, Mapping):
            contents = payload.get("contents")
            if isinstance(contents, Sequence) and not isinstance(contents, (str, bytes, bytearray)):
                parts: list[str] = []
                for item in contents:
                    if isinstance(item, Mapping):
                        if "text" in item:
                            parts.append(str(item["text"]))
                        elif "blob" in item:
                            parts.append(str(item["blob"]))
                if parts:
                    return "\n".join(parts)
            return json.dumps(dict(payload), ensure_ascii=False, default=str)
        return str(payload)

    @staticmethod
    def _is_async_reader(reader: Any) -> bool:
        return inspect.iscoroutinefunction(reader) or inspect.iscoroutinefunction(getattr(reader, "__call__", None))

    @staticmethod
    def _paginate_resource_text(content: str, *, offset: int, limit: int) -> str:
        # MCP content is text or a base64 blob.  Paging is applied to textual
        # entries while the reader itself enforces the total byte limit.
        lines = content.splitlines()
        if not lines:
            return content
        selected = lines[offset : offset + limit]
        result = "\n".join(selected)
        if offset + limit < len(lines):
            result += (
                f"\n\n[Showing lines {offset + 1}-{offset + len(selected)} of {len(lines)}. "
                f"Continue with offset={offset + limit}.]"
            )
        return result

    async def _read_mcp_resource_async(
        self,
        server_name: str,
        uri: str,
        *,
        offset: int,
        limit: int,
    ) -> str:
        if self.resource_reader is None:
            _raise_mcp_tool_error("MCP Resource reader unavailable in this Harness runtime.")
        try:
            payload = await self.resource_reader(server_name, uri, offset, limit)
        except ToolException:
            raise
        except Exception as exc:
            _raise_mcp_tool_error(f"MCP Resource read failed: {exc}")
        if payload is None:
            _raise_mcp_tool_error(f"MCP Resource not found: {uri}")
        if isinstance(payload, Mapping):
            structured = payload.get("structuredContent")
            if isinstance(structured, Mapping) and structured.get("status") == "error":
                _raise_mcp_tool_error(
                    "MCP Resource read rejected: "
                    + json.dumps(dict(structured), ensure_ascii=False, sort_keys=True, default=str)
                )
            if "contents" in payload:
                contents = payload.get("contents")
                if not isinstance(contents, Sequence) or isinstance(contents, (str, bytes, bytearray)) or not contents:
                    _raise_mcp_tool_error(f"MCP Resource returned no content: {uri}")
        content = self._resource_text(payload)
        if not content:
            _raise_mcp_tool_error(f"MCP Resource returned no content: {uri}")
        if len(content.encode("utf-8")) > MAX_MCP_RESOURCE_TEXT_BYTES:
            _raise_mcp_tool_error(f"MCP Resource exceeds the local size limit: {uri}")
        return f"MCP Resource: {uri}\n{self._paginate_resource_text(content, offset=offset, limit=limit)}"

    async def _arun(
        self,
        resource: str,
        offset: int = 0,
        limit: int = 2000,
        mcp_server: str | None = None,
    ) -> str:
        value = resource.strip()
        if mcp_server is not None:
            if self.resource_reader is None:
                _raise_mcp_tool_error("MCP Resource reader unavailable in this Harness runtime.")
            if not self._is_async_reader(self.resource_reader):
                _raise_mcp_tool_error("MCP Resource reader is async; use ainvoke/astream for MCP Resources.")
            try:
                parsed = urlsplit(value)
            except ValueError:
                parsed = None
            if parsed is None or not parsed.scheme:
                _raise_mcp_tool_error("MCP Resource URI must include an absolute URI scheme.")
            return await self._read_mcp_resource_async(
                mcp_server, value, offset=offset, limit=limit
            )

        # Keep normal local image and attachment behavior on the async path.
        return self._run(resource, offset=offset, limit=limit, mcp_server=None)

    def _run(
        self,
        resource: str,
        offset: int = 0,
        limit: int = 2000,
        mcp_server: str | None = None,
    ) -> str:
        value = resource.strip()
        if not value:
            return "❌ Missing resource."
        try:
            parsed = urlsplit(value)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.scheme.lower() in {"http", "https"} and parsed.netloc:
            if mcp_server is None:
                return "❌ Web URL supplied to read_resource; use fetch_url for HTTP(S) resources."
            if self.resource_reader is None:
                _raise_mcp_tool_error("MCP Resource reader unavailable in this Harness runtime.")
            if not self._is_async_reader(self.resource_reader):
                _raise_mcp_tool_error("MCP Resource reader is async; use ainvoke/astream for MCP Resources.")
            _raise_mcp_tool_error("MCP Resource requires async invocation; use ainvoke/astream.")
        if value.startswith("att_"):
            if mcp_server is not None:
                _raise_mcp_tool_error("MCP server bindings apply only to MCP Resource URIs.")
            return self._read_attachment(value, offset=offset, limit=limit)
        if parsed is not None and parsed.scheme and not Path(value).is_absolute():
            if mcp_server is None:
                _raise_mcp_tool_error("MCP Resource requires an explicit mcp_server binding.")
            if self.resource_reader is None:
                _raise_mcp_tool_error("MCP Resource reader unavailable in this Harness runtime.")
            if not self._is_async_reader(self.resource_reader):
                _raise_mcp_tool_error("MCP Resource reader is async; use ainvoke/astream for MCP Resources.")
            _raise_mcp_tool_error("MCP Resource requires async invocation; use ainvoke/astream.")
        if mcp_server is not None:
            _raise_mcp_tool_error("MCP server bindings apply only to MCP Resource URIs.")
        return self._read_image_path_marker(value)


def create_read_resource_tool(base_dir: Path) -> ReadResourceTool:
    """Create an unbound tool; runtime adapters are supplied by the caller."""

    del base_dir
    return ReadResourceTool()


def bind_mcp_resource_reader(tool: ReadResourceTool, enabled_names: Sequence[str]) -> ReadResourceTool:
    """Return a bound copy without mutating a factory-cached tool instance."""

    if not isinstance(tool, ReadResourceTool):
        raise TypeError("tool must be a ReadResourceTool")
    if not tuple(enabled_names):
        return tool.model_copy()
    from mcp_clients.resources import McpResourceReader

    reader = McpResourceReader(enabled_names)
    return tool.model_copy(update={"resource_reader": reader.read})


__all__ = [
    "ReadResourceInput",
    "ReadResourceTool",
    "bind_mcp_resource_reader",
    "create_read_resource_tool",
]
