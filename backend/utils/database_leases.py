"""Domain-independent database clock and lease parameter expressions.

Keep lease clocks inside the database so independent runtimes do not depend on
worker clock skew. This module has no queue, Catalog or application imports.
"""
from __future__ import annotations

from typing import Any


def db_now_expr(dialect: str) -> str:
    return "clock_timestamp()" if dialect == "postgresql" else "CURRENT_TIMESTAMP"


def db_lease_expiry_expr(dialect: str) -> str:
    if dialect == "postgresql":
        return "clock_timestamp() + make_interval(secs => :lease_seconds)"
    return "datetime('now', :lease_modifier)"


def lease_bind_params(dialect: str, lease_seconds: int) -> dict[str, Any]:
    if dialect == "postgresql":
        return {"lease_seconds": int(lease_seconds)}
    return {"lease_modifier": f"+{int(lease_seconds)} seconds"}
