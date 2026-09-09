"""Async database wiring for the standalone PuddingHarness runtime.

The Harness database defaults to a local SQLite file at
``$PUDDINGHARNESS_HOME/db/catalog.sqlite3`` so desktop, local and
single-instance deployments need no external database service. PostgreSQL
remains the server-side option for multi-replica, multi-worker and
multi-tenant deployments and is enabled by configuring an explicit database
URL (see ``config.get_database_config``).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from urllib.parse import quote, unquote

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config import get_database_config
from runtime_identity.paths import PuddingClawPaths

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SQLITE_URL = ""

# The queue lease protocol relies on UPDATE ... RETURNING (SQLite 3.35.0+).
MIN_SQLITE_VERSION = (3, 35, 0)
SQLITE_BUSY_TIMEOUT_MS = 10_000

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_last_error: str | None = None
_last_schema_version: int | None = None


class DatabaseUnsupportedError(RuntimeError):
    """The configured database cannot satisfy Harness minimum requirements."""


def get_database_url() -> str:
    configured = get_database_config().get("url") or DEFAULT_SQLITE_URL
    if configured:
        return configured
    catalog_path = PuddingClawPaths.from_environment().databases() / "catalog.sqlite3"
    path_text = str(catalog_path)
    # A plain SQLAlchemy SQLite URL treats ``?``/``#`` (and literal ``%``)
    # as URL syntax.  Keep the ordinary URL stable for the capabilities
    # contract, but use a file URI when the host path needs escaping.
    if any(character in path_text for character in "?#%"):
        encoded = quote(path_text, safe="/:")
        return f"sqlite+aiosqlite:///file:{encoded}?uri=true"
    return f"sqlite+aiosqlite:///{path_text}"


def is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")


def _engine_kwargs(url: str) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "pool_pre_ping": True,
        "future": True,
    }
    if url.startswith("sqlite+"):
        kwargs["connect_args"] = {"check_same_thread": False}
    elif url.startswith("postgresql+asyncpg"):
        kwargs["poolclass"] = NullPool
    return kwargs


def _apply_sqlite_connection_pragmas(dbapi_connection, _connection_record) -> None:
    """Per-connection setup for every new SQLite connection.

    isolation_level=None puts the driver in autocommit so SQLAlchemy controls
    transaction boundaries itself (see _emit_sqlite_begin); without it the
    pysqlite legacy isolation layer implicitly COMMITs around DDL, which would
    make schema migrations non-transactional on SQLite.
    """

    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    try:
        # journal_mode persists in the database file and cannot be changed
        # inside a transaction; connect time is outside any transaction.
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous = NORMAL")
    finally:
        cursor.close()


def _emit_sqlite_begin(sync_connection) -> None:
    """Emit BEGIN explicitly; the driver is in autocommit (isolation_level=None)."""

    sync_connection.exec_driver_sql("BEGIN")


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        url = get_database_url()
        PuddingClawPaths.from_environment().databases().mkdir(parents=True, exist_ok=True)
        _engine = create_async_engine(url, **_engine_kwargs(url))
        if is_sqlite_url(url):
            event.listen(_engine.sync_engine, "connect", _apply_sqlite_connection_pragmas)
            event.listen(_engine.sync_engine, "begin", _emit_sqlite_begin)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def _verify_sqlite_runtime(engine: AsyncEngine) -> None:
    """Fail closed when SQLite cannot host a reliable Harness database."""

    async with engine.connect() as conn:
        version_text = str(await conn.scalar(text("SELECT sqlite_version()")))
        version = tuple(int(part) for part in version_text.split("."))
        if version < MIN_SQLITE_VERSION:
            required = ".".join(str(part) for part in MIN_SQLITE_VERSION)
            raise DatabaseUnsupportedError(
                f"SQLite {required}+ is required for the Harness database (the queue lease "
                f"protocol relies on UPDATE ... RETURNING, added in SQLite 3.35.0); "
                f"current SQLite is {version_text}. Upgrade SQLite (e.g. upgrade Python) "
                "or configure a supported database URL."
            )
        journal_mode = str(await conn.scalar(text("PRAGMA journal_mode"))).lower()
        if journal_mode != "wal":
            raise DatabaseUnsupportedError(
                f"Failed to enable SQLite WAL journal mode (got {journal_mode!r}). "
                "The Harness database must live on a reliable local single-writer filesystem."
            )


def _sqlite_database_path(url: str) -> Path | None:
    """Resolve the path using the same escaping semantics as SQLAlchemy's engine."""

    parsed = make_url(url)
    database = parsed.database
    if not database or database == ":memory:":
        return None
    # SQLAlchemy treats a normal SQLite URL's percent characters literally.
    # Decode only the explicit file URI form, where percent escapes are part
    # of the URI grammar; otherwise preflight must inspect the exact engine
    # path rather than a different decoded filename.
    is_file_uri = database.startswith("file:") and str(parsed.query.get("uri", "")).lower() == "true"
    if is_file_uri:
        database = database.removeprefix("file:")
        database = unquote(database)
    path = Path(database).expanduser()
    if not path.is_absolute():
        path = path.resolve(strict=False)
    return path


def _sqlite_read_only_url(url: str) -> str | None:
    """Build an existing-file-only URL for the preflight identity check."""

    path = _sqlite_database_path(url)
    if path is None:
        return None
    encoded = quote(str(path), safe="/:")
    return f"sqlite:///file:{encoded}?mode=ro&uri=true"


def _preflight_sqlite_identity(url: str) -> None:
    """Reject foreign/legacy schemas before the writable engine can touch SQLite."""

    read_only_url = _sqlite_read_only_url(url)
    if read_only_url is None:
        return
    database = _sqlite_database_path(url)
    assert database is not None
    if not database.exists():
        return
    from schema_migrations import validate_database_identity

    engine = create_engine(read_only_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            validate_database_identity(conn)
    finally:
        engine.dispose()


async def init_database() -> bool:
    """Verify, migrate and maintain the standalone Harness database.

    Schema migrations run transactionally, so a failed or interrupted
    migration leaves the previous database recoverable instead of serving a
    half-migrated schema. Returns False (degraded mode) when the database is
    unusable; ``get_database_status()["last_error"]`` carries the diagnosis.
    """

    global _last_error, _last_schema_version
    try:
        database_url = get_database_url()
        if is_sqlite_url(database_url):
            _preflight_sqlite_identity(database_url)
        engine = get_engine()
        if is_sqlite_url(database_url):
            await _verify_sqlite_runtime(engine)
        from schema_migrations import CURRENT_SCHEMA_VERSION, migrate_to_latest

        async with engine.begin() as conn:
            applied = await conn.run_sync(migrate_to_latest)
        if applied:
            logger.info("[db] Harness schema migrations applied: %s", applied)
        _last_schema_version = CURRENT_SCHEMA_VERSION
        _last_error = None
        return True
    except Exception as exc:
        _last_error = str(exc)
        logger.warning("[db] Harness database init failed: %s", exc)
        return False


async def close_database() -> None:
    """Dispose the Harness engine and release SQLite/PostgreSQL resources."""

    global _engine, _sessionmaker
    engine = _engine
    _engine = None
    _sessionmaker = None
    if engine is not None:
        await engine.dispose()


def get_database_status() -> dict[str, object]:
    """Return secret-free control-plane status without decrypting credentials."""

    config = get_database_config(resolve_password=False)
    provider = str(config.get("provider") or "unknown")
    credential_readable = bool(config.get("password_readable", True))
    configured = provider == "sqlite" or bool(
        config.get("configured_url")
        or (
            config.get("host")
            and config.get("database")
            and config.get("password_configured")
        )
    )
    if provider == "sqlite":
        safe_url = f"sqlite+aiosqlite:///{PuddingClawPaths.from_environment().databases() / 'catalog.sqlite3'}"
    else:
        safe_url = str(config.get("configured_url") or "")
        if not safe_url and provider == "postgresql":
            safe_url = (
                "postgresql+asyncpg://***@"
                f"{config.get('host') or '127.0.0.1'}:{config.get('port') or 5432}/"
                f"{config.get('database') or 'puddingharness'}"
            )
    if "@" in safe_url and "://" in safe_url:
        scheme, rest = safe_url.split("://", 1)
        safe_url = f"{scheme}://***@{rest.split('@', 1)[1]}"
    credential_error = str(config.get("password_error") or "")
    return {
        "configured": configured,
        "provider": provider,
        "url": safe_url,
        "configured_by": config.get("configured_by"),
        "environment_override": config.get("environment_override"),
        "mode": config.get("mode"),
        "credential_readable": credential_readable,
        "credential_error": credential_error,
        "schema_version": _last_schema_version,
        "configuration_hint": (
            credential_error
            if not credential_readable
            else ""
            if configured
            else "数据库未配置；请前往 Settings -> 数据库配置数据库连接。"
        ),
        "last_error": credential_error or _last_error,
        "healthy": configured and credential_readable and _last_error is None,
    }


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session
