"""Pure planning for replacing the source snapshot's session domains.

The planner only consumes inventories.  It does not read or write the file
system, and therefore can be used before acquiring the install barrier.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from harness.session_import import MAX_FILES, MAX_TOTAL, ROOTS
from harness.source_snapshot import MAX_ENTRIES, _validate_inventory


def _hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Invalid inventory path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in (".", "..") for part in path.parts):
        raise ValueError("Invalid inventory path")
    return value


def _domain(path: str) -> str | None:
    for root in ROOTS:
        if path == root or path.startswith(root + "/"):
            return root
    return None


def _session_files(value: Any, *, name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError(f"Invalid {name} files")
    if len(value) > MAX_FILES:
        raise ValueError("Session migration budget exceeded")
    result: dict[str, dict[str, Any]] = {}
    total = 0
    for raw_path, fact in value.items():
        path = _path(raw_path)
        if _domain(path) is None or path in ROOTS:
            raise ValueError("Session file outside owned domain")
        if path.endswith((".migration-part", ".reverse-part")) or path.endswith(".lock"):
            raise ValueError("Reserved session filename")
        if not isinstance(fact, dict) or set(fact) != {"digest", "size"}:
            raise ValueError("Invalid session file commitment")
        digest, size = fact["digest"], fact["size"]
        if not (isinstance(digest, str) and digest.startswith("sha256:") and _hex(digest[7:])):
            raise ValueError("Invalid session file digest")
        if type(size) is not int or size < 0 or size > 32 * 1024 * 1024:
            raise ValueError("Invalid session file size")
        total += size
        if total > MAX_TOTAL:
            raise ValueError("Session migration budget exceeded")
        result[path] = {"digest": digest, "size": size}
    return dict(sorted(result.items()))


def _source_session_files(source: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_path, fact in source["files"].items():
        path = _path(raw_path)
        if path.endswith(".reverse-part"):
            raise ValueError("Reserved reverse filename")
        domain = _domain(path)
        if domain is None:
            continue
        if path.endswith(".migration-part"):
            raise ValueError("Reserved session filename")
        if path.endswith(".lock"):
            if fact["size"] != 0:
                raise ValueError("Non-empty session lock")
        result[path] = fact
    return result


def _source_to_session(fact: dict[str, Any]) -> dict[str, Any]:
    return {"digest": "sha256:" + fact["sha256"], "size": fact["size"]}


def plan_session_reverse(
    source_inventory: dict,
    baseline_session_files: dict,
    after_session_files: dict,
) -> dict:
    """Return a validated merged inventory and session change counts.

    ``baseline_session_files`` is an optimistic concurrency guard: it must be
    exactly the source inventory's owned, non-lock files.
    """
    if not isinstance(source_inventory, dict):
        raise ValueError("Invalid source inventory")
    _validate_inventory(source_inventory)
    baseline = _session_files(baseline_session_files, name="baseline")
    after = _session_files(after_session_files, name="after")
    source_session = _source_session_files(source_inventory)
    source_nonlocks = {
        path: _source_to_session(fact)
        for path, fact in source_session.items()
        if not path.endswith(".lock")
    }
    if baseline != source_nonlocks:
        raise ValueError("Session baseline differs from source inventory")

    source_files = source_inventory["files"]
    merged_files = {
        path: dict(fact)
        for path, fact in source_files.items()
        if _domain(path) is None
    }
    merged_files.update(
        {path: {"sha256": fact["digest"][7:], "size": fact["size"]} for path, fact in after.items()}
    )
    merged_dirs = {path for path in source_inventory["directories"] if _domain(path) is None}
    for path in after:
        parent = PurePosixPath(path).parent
        while str(parent) != ".":
            merged_dirs.add(str(parent))
            parent = parent.parent
    merged = {
        "files": dict(sorted(merged_files.items())),
        "directories": sorted(merged_dirs, key=lambda path: tuple(path.split("/"))),
        "total_bytes": sum(fact["size"] for fact in merged_files.values()),
    }
    _validate_inventory(merged)
    inserts = sum(path not in source_session for path in after)
    deletes = sum(path not in after for path in source_session)
    updates = sum(path in source_session and source_session[path]["sha256"] != fact["digest"][7:] for path, fact in after.items())
    return {"inventory": merged, "changes": {"insert": inserts, "update": updates, "delete": deletes}}
