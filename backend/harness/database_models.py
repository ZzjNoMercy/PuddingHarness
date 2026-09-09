"""Harness-owned database metadata, independent of the Knowledge catalog.

The Headless log retains its legacy SQL names for explicit data import. This
metadata does not initialize or migrate a database at import time.
"""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


class Base(DeclarativeBase):
    pass


class WorkerAccessLog(Base):
    """Activity record for a local Headless Run (legacy table/column names)."""

    __tablename__ = "worker_access_logs"
    __table_args__ = (
        Index("ix_worker_access_logs_created", "created_at"),
        Index("ix_worker_access_logs_key_name", "key_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("wal"))
    key_id: Mapped[str] = mapped_column(String(120), nullable=False)
    key_name: Mapped[str] = mapped_column(String(120), nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

