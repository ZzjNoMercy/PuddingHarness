"""Generic PuddingHarness capability probes.

The Harness health surface reports only facilities owned by the Harness
runtime.  Knowledge Platform services are discovered and health-checked by
that product (or by an explicitly configured MCP endpoint), so this module
must not import their packages or inspect their infrastructure.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from cli_runtime import current_cli_runtime_status
from config import get_database_config, load_config

logger = logging.getLogger(__name__)

_CAPABILITIES_CACHE: Capabilities | None = None
_CAPABILITIES_CACHED_AT: datetime | None = None
_CACHE_TTL = timedelta(seconds=60)
DEFAULT_POSTGRES_URL = ""


@dataclass
class CapabilityStatus:
    available: bool
    reason: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"available": self.available, "reason": self.reason}
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class Capabilities:
    core_database: CapabilityStatus
    docker: CapabilityStatus
    cli: CapabilityStatus | None = None

    def to_dict(self) -> dict[str, dict[str, Any]]:
        result = {
            "core_database": self.core_database.to_dict(),
            "docker": self.docker.to_dict(),
            # Kept for clients released before core_database was named.
            "database": self.core_database.to_dict(),
        }
        if self.cli is not None:
            result["cli"] = self.cli.to_dict()
        return result


def _check_cli() -> CapabilityStatus:
    status = current_cli_runtime_status(Path(__file__).resolve().parent)
    installed = bool(status.get("installed"))
    details = {
        "安装状态": "已安装" if installed else "未安装",
        "版本": str(status.get("version") or "未检测到"),
        "Node.js": str((status.get("node") or {}).get("version") or "未检测到"),
        "npm": str((status.get("npm") or {}).get("version") or "未检测到"),
        "安装策略": str(status.get("install_policy") or "未配置"),
    }
    message = str(status.get("install_message") or "").strip()
    if message:
        details["检测说明"] = message
    return CapabilityStatus(
        available=installed,
        reason=None if installed else (message or "PuddingHarness CLI 尚未安装"),
        details=details,
    )


def _docker_config() -> dict[str, Any]:
    config = load_config()
    docker = config.get("harness", {}).get("terminal", {}).get("docker", {})
    return dict(docker) if isinstance(docker, dict) else {}


def _check_docker_sync() -> CapabilityStatus:
    """Probe the configured Docker daemon without creating a container."""
    try:
        from harness.workspace_backends import ProjectSandboxManager

        available, detail = ProjectSandboxManager(_docker_config()).probe()
        return CapabilityStatus(
            available=available,
            reason=None if available else (detail or "Docker daemon unavailable"),
        )
    except Exception as exc:  # noqa: BLE001 - health probes are non-fatal
        return CapabilityStatus(available=False, reason=f"{type(exc).__name__}: {exc}")


async def _check_docker() -> CapabilityStatus:
    return await asyncio.to_thread(_check_docker_sync)


def _resolve_database_url(explicit_url: str | None = None) -> str:
    database_config = get_database_config()
    if explicit_url:
        return explicit_url
    configured = str(database_config.get("url") or "").strip()
    if configured:
        return configured
    if database_config.get("mode") == "sqlite":
        catalog_path = str(database_config.get("catalog_path") or "").strip()
        if catalog_path:
            return f"sqlite+aiosqlite:///{quote(catalog_path, safe='/:')}"
    return DEFAULT_POSTGRES_URL


def _normalize_async_postgres_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _is_postgres_url(url: str) -> bool:
    return url.startswith("postgresql://") or url.startswith("postgresql+")


def _is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite://") or url.startswith("sqlite+")


def _sqlite_read_only_url(url: str) -> str:
    """Convert a SQLite URL to an existing-file-only URI.

    ``mode=ro`` prevents a health check from creating a missing catalog. The
    path is decoded before re-encoding so literal ``?`` and ``#`` characters
    remain part of the filename rather than becoming URL syntax.
    """
    parsed = urlsplit(url)
    raw_path = parsed.path
    if parsed.netloc:
        raw_path = f"//{parsed.netloc}{raw_path}"
    if raw_path.startswith("/file:"):
        raw_path = raw_path.removeprefix("/file:")
    elif raw_path.startswith("file:"):
        raw_path = raw_path.removeprefix("file:")
    if raw_path.startswith("//") and not parsed.netloc:
        raw_path = "/" + raw_path.lstrip("/")
    raw_path = unquote(raw_path)
    encoded_path = quote(raw_path, safe="/:")
    driver = "sqlite+aiosqlite" if parsed.scheme == "sqlite+aiosqlite" else "sqlite"
    return f"{driver}:///file:{encoded_path}?mode=ro&uri=true"


def _normalize_sync_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    if url.startswith("sqlite+aiosqlite://"):
        return url.replace("sqlite+aiosqlite://", "sqlite://", 1)
    return url


def _asyncpg_missing_status() -> CapabilityStatus:
    return CapabilityStatus(
        available=False,
        reason="未安装 PostgreSQL 异步驱动（asyncpg）；请安装部署环境提供的 PostgreSQL 驱动依赖",
        details={"scope": "core", "driver": "missing"},
    )


async def _check_postgres(url: str | None) -> CapabilityStatus:
    """Probe the Harness Core Catalog database only."""
    try:
        target = _resolve_database_url(url)
    except ValueError as exc:
        return CapabilityStatus(
            available=False,
            reason=str(exc),
            details={"mode": "postgresql", "scope": "core", "credential_readable": False},
        )
    if _is_sqlite_url(target):
        target = _sqlite_read_only_url(target)
        engine = None
        try:
            engine = create_async_engine(target, pool_pre_ping=True)
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                await conn.execute(text("SELECT name FROM sqlite_master LIMIT 1"))
            return CapabilityStatus(
                available=True,
                reason="SQLite catalog in PuddingHarness Home",
                details={"mode": "sqlite", "scope": "core"},
            )
        except Exception as exc:  # noqa: BLE001 - health probes are non-fatal
            return CapabilityStatus(
                available=False,
                reason=f"{type(exc).__name__}: {exc}",
                details={"mode": "sqlite", "scope": "core"},
            )
        finally:
            if engine is not None:
                await engine.dispose()
    if not _is_postgres_url(target):
        return CapabilityStatus(
            available=False,
            reason="PostgreSQL URL not configured",
            details={"mode": "postgresql", "scope": "core"},
        )

    try:
        import asyncpg  # noqa: F401
    except ImportError:
        return _asyncpg_missing_status()

    engine = None
    try:
        engine = create_async_engine(_normalize_async_postgres_url(target), pool_pre_ping=True)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return CapabilityStatus(available=True, details={"mode": "postgresql", "scope": "core"})
    except Exception as exc:  # noqa: BLE001 - health probes are non-fatal
        return CapabilityStatus(
            available=False,
            reason=f"{type(exc).__name__}: {exc}",
            details={"mode": "postgresql", "scope": "core"},
        )
    finally:
        if engine is not None:
            await engine.dispose()


def _check_postgres_sync(url: str | None) -> CapabilityStatus:
    """Synchronous Core Catalog probe using a real ``SELECT 1`` connection."""
    try:
        target = _resolve_database_url(url)
    except ValueError as exc:
        return CapabilityStatus(available=False, reason=str(exc), details={"scope": "core"})
    if not (_is_postgres_url(target) or _is_sqlite_url(target)):
        return CapabilityStatus(
            available=False,
            reason="PostgreSQL URL not configured",
            details={"mode": "postgresql", "scope": "core"},
        )

    try:
        if _is_sqlite_url(target):
            target = _sqlite_read_only_url(target)
        _sync_select_one(target)
        mode = "sqlite" if _is_sqlite_url(target) else "postgresql"
        reason = "SQLite catalog in PuddingHarness Home" if mode == "sqlite" else None
        return CapabilityStatus(available=True, reason=reason, details={"mode": mode, "scope": "core"})
    except Exception as exc:  # noqa: BLE001 - health probes are non-fatal
        mode = "sqlite" if _is_sqlite_url(target) else "postgresql"
        return CapabilityStatus(
            available=False,
            reason=f"{type(exc).__name__}: {exc}",
            details={"mode": mode, "scope": "core", "verified": False},
        )


def _sync_select_one(target: str) -> None:
    """Open the configured Core DB with a synchronous driver and execute SQL."""
    engine = create_engine(_normalize_sync_url(target), pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            if target.startswith("sqlite://"):
                conn.execute(text("SELECT name FROM sqlite_master LIMIT 1"))
    finally:
        engine.dispose()


async def detect_capabilities(
    *,
    force: bool = False,
    postgres_url: str | None = None,
    **_: object,
) -> Capabilities:
    """Return generic Harness health without consulting Platform services."""
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT
    now = datetime.now(timezone.utc)
    if not force and _CAPABILITIES_CACHE is not None and _CAPABILITIES_CACHED_AT is not None:
        if now - _CAPABILITIES_CACHED_AT < _CACHE_TTL:
            return _CAPABILITIES_CACHE

    core_database, docker = await asyncio.gather(_check_postgres(postgres_url), _check_docker())
    caps = Capabilities(core_database=core_database, docker=docker, cli=_check_cli())
    _CAPABILITIES_CACHE = caps
    _CAPABILITIES_CACHED_AT = now
    logger.debug("PuddingHarness capabilities detected: %s", caps.to_dict())
    return caps


def detect_capabilities_sync(
    *,
    force: bool = False,
    postgres_url: str | None = None,
    **_: object,
) -> Capabilities:
    """Synchronous wrapper for generic Harness callers."""
    if not force and _CAPABILITIES_CACHE is not None and _CAPABILITIES_CACHED_AT is not None:
        if datetime.now(timezone.utc) - _CAPABILITIES_CACHED_AT < _CACHE_TTL:
            return _CAPABILITIES_CACHE
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(detect_capabilities(force=force, postgres_url=postgres_url))
    return _detect_capabilities_sync_fallback(force=force, postgres_url=postgres_url)


def _detect_capabilities_sync_fallback(
    *, force: bool = False, postgres_url: str | None = None, **_: object
) -> Capabilities:
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT
    now = datetime.now(timezone.utc)
    if not force and _CAPABILITIES_CACHE is not None and _CAPABILITIES_CACHED_AT is not None:
        if now - _CAPABILITIES_CACHED_AT < _CACHE_TTL:
            return _CAPABILITIES_CACHE
    caps = Capabilities(
        core_database=_check_postgres_sync(postgres_url),
        docker=_check_docker_sync(),
        cli=_check_cli(),
    )
    _CAPABILITIES_CACHE = caps
    _CAPABILITIES_CACHED_AT = now
    return caps


def invalidate_capabilities() -> None:
    """Clear the probe cache, primarily for tests and config reloads."""
    global _CAPABILITIES_CACHE, _CAPABILITIES_CACHED_AT
    _CAPABILITIES_CACHE = None
    _CAPABILITIES_CACHED_AT = None
