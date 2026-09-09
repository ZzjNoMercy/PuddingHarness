"""Versioned migrations for the standalone PuddingHarness database.

Only Harness-owned persistence is created here.  Knowledge/Analytics tables and
legacy ``core_schema_migrations`` catalogs are rejected rather than silently
being treated as a Harness database.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from harness.database_models import Base

logger = logging.getLogger(__name__)

HARNESS_PRODUCT_ID = "puddingharness"
CURRENT_SCHEMA_VERSION = 2
_VERSION_TABLE = "harness_schema_migrations"
_RUNTIME_TABLE = "core_runtime_control"
_ALLOWED_TABLES = {_VERSION_TABLE, "worker_access_logs", _RUNTIME_TABLE}
_MINIMUM_COLUMNS = {
    _VERSION_TABLE: {"product", "version", "description", "applied_at"},
    "worker_access_logs": {"id", "key_id", "key_name", "query", "created_at"},
    _RUNTIME_TABLE: {
        "id",
        "write_mode",
        "maintenance_owner",
        "lease_expires_at",
        "generation",
        "reason",
        "updated_at",
    },
}
_DISALLOWED_TABLES = {
    "core_schema_migrations",
    "knowledge_bases",
    "knowledge_documents",
    "knowledge_source_connections",
    "knowledge_source_items",
    "knowledge_sync_runs",
    "knowledge_import_jobs",
    "knowledge_import_events",
    "semantic_dimension_build_jobs",
    "semantic_dimension_build_events",
    "analytics_query_results",
    "feishu_app_credentials",
    "feishu_user_grants",
    "feishu_oauth_sessions",
    "knowledge_database_sources",
    "knowledge_table_assets",
    "read_later_items",
    "task_notifications",
}

_VERSION_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {_VERSION_TABLE} ("
    "product VARCHAR(64) NOT NULL, "
    "version INTEGER NOT NULL, "
    "description VARCHAR(200) NOT NULL, "
    "applied_at VARCHAR(40) NOT NULL, "
    "PRIMARY KEY (product, version), "
    "CHECK (product = 'puddingharness'))"
)


def _tables(conn: Connection) -> set[str]:
    return set(inspect(conn).get_table_names())


def _ensure_version_table(conn: Connection) -> None:
    conn.exec_driver_sql(_VERSION_TABLE_DDL)


def _assert_standalone_database(conn: Connection) -> None:
    tables = _tables(conn)
    forbidden = sorted(tables & _DISALLOWED_TABLES)
    if forbidden:
        raise RuntimeError(
            "PuddingHarness refuses a mixed or legacy catalog database; "
            f"found excluded tables: {', '.join(forbidden)}"
        )
    if "core_schema_migrations" in tables:
        raise RuntimeError("PuddingHarness refuses the legacy core schema migration history")
    unknown = sorted(tables - _ALLOWED_TABLES)
    if unknown:
        raise RuntimeError(
            "PuddingHarness database contains unowned tables: " + ", ".join(unknown)
        )
    if _VERSION_TABLE in tables:
        products = {
            str(row[0])
            for row in conn.exec_driver_sql(
                f"SELECT DISTINCT product FROM {_VERSION_TABLE}"
            ).all()
        }
        foreign_products = sorted(products - {HARNESS_PRODUCT_ID})
        if foreign_products:
            raise RuntimeError(
                "PuddingHarness schema history belongs to another product: "
                + ", ".join(foreign_products)
            )


def _applied_versions(conn: Connection) -> set[int]:
    rows = conn.exec_driver_sql(
        f"SELECT version FROM {_VERSION_TABLE} WHERE product = 'puddingharness'"
    ).all()
    return {int(row[0]) for row in rows}


def _assert_minimum_table_shapes(conn: Connection, tables: set[str]) -> None:
    inspector = inspect(conn)
    for table, required in _MINIMUM_COLUMNS.items():
        if table not in tables:
            continue
        columns = {str(column["name"]) for column in inspector.get_columns(table)}
        missing = sorted(required - columns)
        if missing:
            raise RuntimeError(
                f"PuddingHarness table {table} is missing required columns: {', '.join(missing)}"
            )


def validate_database_identity(conn: Connection) -> None:
    """Validate ownership and version history without creating or mutating anything."""

    _assert_standalone_database(conn)
    tables = _tables(conn)
    _assert_minimum_table_shapes(conn, tables)
    if _VERSION_TABLE not in tables:
        if _RUNTIME_TABLE in tables:
            raise RuntimeError("Harness runtime control exists without Harness schema history")
        return
    applied = _applied_versions(conn)
    if not applied and _RUNTIME_TABLE in tables:
        raise RuntimeError("Harness runtime control exists without Harness schema history")
    known = {version for version, _description, _upgrade in MIGRATIONS}
    unknown = sorted(applied - known)
    if unknown:
        raise RuntimeError(f"Harness database has future schema versions: {unknown}")
    missing_history = set(range(1, max(applied, default=0) + 1)) - applied
    if missing_history:
        raise RuntimeError(
            f"Harness schema history is non-contiguous: {sorted(missing_history)}"
        )
    if 1 in applied and "worker_access_logs" not in tables:
        raise RuntimeError("Harness schema version 1 requires worker_access_logs")
    if 2 in applied and _RUNTIME_TABLE not in tables:
        raise RuntimeError("Harness schema version 2 requires core_runtime_control")


def _record_version(conn: Connection, version: int, description: str) -> None:
    conn.execute(
        text(
            f"INSERT INTO {_VERSION_TABLE} "
            "(product, version, description, applied_at) "
            "VALUES (:product, :version, :description, :applied_at)"
        ),
        {
            "product": HARNESS_PRODUCT_ID,
            "version": version,
            "description": description,
            "applied_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def _migrate_v1_harness_base(conn: Connection) -> None:
    Base.metadata.create_all(bind=conn)


def _migrate_v2_runtime_control(conn: Connection) -> None:
    inspector = inspect(conn)
    if _RUNTIME_TABLE in inspector.get_table_names():
        return
    timestamp_type = "TIMESTAMP WITH TIME ZONE" if conn.dialect.name == "postgresql" else "DATETIME"
    conn.exec_driver_sql(
        f"CREATE TABLE {_RUNTIME_TABLE} ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "write_mode VARCHAR(20) NOT NULL DEFAULT 'normal', "
        "maintenance_owner VARCHAR(120), "
        f"lease_expires_at {timestamp_type}, "
        "generation INTEGER NOT NULL DEFAULT 0, "
        "reason TEXT NOT NULL DEFAULT '', "
        f"updated_at {timestamp_type})"
    )


MIGRATIONS: list[tuple[int, str, Callable[[Connection], None]]] = [
    (1, "standalone Harness base schema", _migrate_v1_harness_base),
    (2, "Harness runtime control lease", _migrate_v2_runtime_control),
]


def migrate_to_latest(conn: Connection) -> list[int]:
    """Create or upgrade only the standalone Harness schema transactionally."""

    validate_database_identity(conn)
    _ensure_version_table(conn)
    applied = _applied_versions(conn)

    # A worker table without Harness history is only accepted as an explicitly
    # recognizable pre-versioned Harness database. A runtime-control table by
    # itself is ambiguous legacy state and fails closed.
    done: list[int] = []
    for version, description, upgrade in MIGRATIONS:
        if version in applied:
            continue
        upgrade(conn)
        _record_version(conn, version, description)
        applied.add(version)
        done.append(version)
    return done


def current_schema_version(conn: Connection) -> int:
    """Return this product's applied schema version, or zero when uninitialized."""

    if _VERSION_TABLE not in _tables(conn):
        return 0
    row = conn.exec_driver_sql(
        f"SELECT MAX(version) FROM {_VERSION_TABLE} WHERE product = 'puddingharness'"
    ).first()
    return int(row[0]) if row and row[0] is not None else 0
