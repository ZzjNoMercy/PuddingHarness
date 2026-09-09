"""Lifecycle rules for CLI-created Headless Worker Sessions.

Normal PuddingClaw chat Sessions are user-owned and never participate in this
retention policy.  A Session is eligible only when its durable metadata marks
it as a Headless Worker Session and no Run or HITL future can still mutate it.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from typing import Any

from graph.permission_resume import permission_resume_registry
from graph.kernel_fallback_resume import kernel_fallback_resume_registry
from graph.session_manager import SessionManager, session_manager
from graph.skill_plan_resume import skill_plan_resume_registry
from graph.skill_secret_resume import skill_secret_resume_registry
from graph.user_input_resume import user_input_resume_registry

logger = logging.getLogger(__name__)

DEFAULT_HEADLESS_SESSION_TTL_HOURS = 24.0
TERMINAL_RUN_STATUSES = {
    "completed",
    "cancelled",
    "failed",
    "blocked",
    "budget_exceeded",
    "verification_failed",
}

_RESUME_REGISTRIES = (
    permission_resume_registry,
    user_input_resume_registry,
    skill_plan_resume_registry,
    skill_secret_resume_registry,
    kernel_fallback_resume_registry,
)


def headless_session_ttl_seconds() -> float | None:
    """Return the configured inactivity TTL; zero disables automatic expiry."""

    raw = os.getenv("PUDDINGHARNESS_HEADLESS_SESSION_TTL_HOURS", "24").strip()
    try:
        hours = float(raw)
    except ValueError:
        logger.warning(
            "Invalid PUDDINGHARNESS_HEADLESS_SESSION_TTL_HOURS=%r; using 24 hours",
            raw,
        )
        hours = DEFAULT_HEADLESS_SESSION_TTL_HOURS
    if hours <= 0:
        return None
    return hours * 60 * 60


def headless_session_expires_at(metadata: dict[str, Any], ttl_seconds: float | None) -> float | None:
    """Calculate the inactivity expiry advertised to an external Agent."""

    if ttl_seconds is None:
        return None
    try:
        updated_at = float(metadata.get("updated_at"))
    except (TypeError, ValueError):
        return None
    return updated_at + ttl_seconds


def is_headless_session_expired(
    metadata: dict[str, Any],
    *,
    now: float | None = None,
    ttl_seconds: float | None = None,
) -> bool:
    """Return true only for an explicitly Headless Session past its TTL."""

    # ``runtime_mode`` describes the active runtime and is intentionally
    # changed to ``agent`` when DeepAgents starts. ``headless_enabled`` is the
    # durable ownership marker stamped only by the Headless Worker API.
    if metadata.get("headless_enabled") is not True:
        return False
    effective_ttl = headless_session_ttl_seconds() if ttl_seconds is None else ttl_seconds
    expires_at = headless_session_expires_at(metadata, effective_ttl)
    return expires_at is not None and expires_at <= (time.time() if now is None else now)


def headless_session_has_pending_resume(
    session_id: str,
    registries: Iterable[Any] = _RESUME_REGISTRIES,
) -> bool:
    """Return whether any in-process HITL future still owns the Session."""

    for registry in registries:
        checker = getattr(registry, "has_pending_session", None)
        if callable(checker):
            if checker(session_id):
                return True
            continue
        requests = getattr(registry, "_requests", None)
        if not isinstance(requests, dict):
            continue
        for request in requests.values():
            if not isinstance(request, dict):
                continue
            if str(request.get("session_id") or "") != session_id:
                continue
            if str(request.get("status") or "").lower() in {"pending", "waiting", "open"}:
                return True
    return False


def cleanup_stale_headless_sessions(
    *,
    manager: SessionManager = session_manager,
    now: float | None = None,
    ttl_seconds: float | None = None,
    protected_session_ids: set[str] | None = None,
    resume_registries: Iterable[Any] = _RESUME_REGISTRIES,
) -> list[str]:
    """Delete expired, idle Headless Sessions through the canonical cleanup path."""

    if not manager.is_initialized:
        return []
    effective_ttl = headless_session_ttl_seconds() if ttl_seconds is None else ttl_seconds
    if effective_ttl is None or effective_ttl <= 0:
        return []
    effective_now = time.time() if now is None else now
    protected = protected_session_ids or set()
    deleted: list[str] = []
    cutoff = effective_now - effective_ttl
    for listed in manager.list_sessions():
        session_id = str(listed.get("id") or "")
        if not session_id or session_id in protected:
            continue
        if headless_session_has_pending_resume(session_id, resume_registries):
            continue
        # The atomic delete method reads the authoritative Session JSON and
        # re-checks Headless ownership, inactivity and Run terminality under
        # the Session lock.  Do not call ``get_metadata`` here: metadata
        # projection resolves the Session's current project permission policy,
        # while retention is intentionally independent of project existence.
        # A stale Headless Session may legitimately outlive a temporary Teams
        # workspace/project and must remain cleanable in that state.
        if manager.delete_session_if_idle_headless_before(
            session_id,
            cutoff=cutoff,
            terminal_run_statuses=TERMINAL_RUN_STATUSES,
        ):
            deleted.append(session_id)
    if deleted:
        logger.info("Deleted %d expired Headless Worker Session(s)", len(deleted))
    return deleted
