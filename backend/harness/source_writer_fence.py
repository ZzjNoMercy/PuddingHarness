"""Validate the persistent legacy PuddingClaw writer freeze at CUTOVER.

The final integrated PuddingClaw release publishes this marker while holding
its installation admission lock. Every participating legacy writer refuses to
start while either the final marker or its publication part exists. Harness
does not import PuddingClaw code; it validates the versioned bytes directly.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

try:
    import fcntl
except ImportError:  # pragma: no cover - the release target is POSIX
    fcntl = None


FORMAT = "puddingclaw-source-freeze/v1"
MARKER_NAME = ".installation-freeze-v1.json"
PART_NAME = MARKER_NAME + ".part"
LOCK_NAME = ".installation-gate-v1.lock"
CAPABILITY_NAME = ".installation-admission-capability-v1.json"
CAPABILITY_PART_NAME = CAPABILITY_NAME + ".part"
CAPABILITY_FORMAT = "puddingclaw-installation-admission/v1"
MAX_BYTES = 4096
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")


def _encoded(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _root(value) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Source Home must be absolute and unlinked")
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Source Home must be private and owned")
    return resolved


def _read(path: Path, *, links: int = 1) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != links or before.st_mode & 0o077 or before.st_size > MAX_BYTES):
            raise ValueError("Source freeze marker must be private, single-linked, and bounded")
        raw = os.read(fd, MAX_BYTES + 1)
        after = path.stat(follow_symlinks=False)
        if (len(raw) > MAX_BYTES
                or (before.st_dev, before.st_ino, before.st_size)
                != (after.st_dev, after.st_ino, after.st_size)):
            raise ValueError("Source freeze marker changed")
        return raw
    finally:
        os.close(fd)


def _sync_directory(root: Path) -> None:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_capability(root: Path, lock_info=None) -> tuple[str, os.stat_result]:
    if (root / CAPABILITY_PART_NAME).exists() or (root / CAPABILITY_PART_NAME).is_symlink():
        raise ValueError("Source admission capability publication is incomplete")
    lock_path = root / LOCK_NAME
    if lock_info is None:
        try:
            lock_info = lock_path.stat(follow_symlinks=False)
        except FileNotFoundError as error:
            raise ValueError("Source admission capability is missing") from error
    if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
            or lock_info.st_nlink != 1 or lock_info.st_mode & 0o077):
        raise ValueError("Source admission lock is invalid")
    try:
        raw = _read(root / CAPABILITY_NAME)
    except FileNotFoundError as error:
        raise ValueError("Source admission capability is missing") from error
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Source admission capability is invalid") from error
    directory = root.stat()
    expected = {
        "format", "home_identity", "directory_identity", "lock_identity",
        "participating_process_admission", "persistent_source_freeze",
    }
    if (not isinstance(value, dict) or set(value) != expected or raw != _encoded(value)
            or value["format"] != CAPABILITY_FORMAT
            or value["home_identity"] != hashlib.sha256(str(root).encode()).hexdigest()
            or value["directory_identity"] != {"device": directory.st_dev, "inode": directory.st_ino}
            or value["lock_identity"] != {"device": lock_info.st_dev, "inode": lock_info.st_ino}
            or value["participating_process_admission"] is not True
            or value["persistent_source_freeze"] is not True):
        raise ValueError("Source admission capability does not bind this Home and lock")
    return hashlib.sha256(raw).hexdigest(), lock_info


def publish_source_fence(source_home, operation_id: str) -> dict:
    """Freeze a cooperative legacy Home under its shared admission lock.

    The protocol is intentionally byte-compatible with PuddingClaw's final
    integrated release. Existing participating writers hold a shared lease on
    the same inode, so this exclusive non-blocking lease refuses publication
    until they have stopped. The marker then makes later starts fail closed.
    """
    if not isinstance(operation_id, str) or _OPERATION.fullmatch(operation_id) is None:
        raise ValueError("Invalid source freeze operation ID")
    if fcntl is None:
        raise ValueError("Source freeze requires POSIX flock")
    root = _root(source_home)
    lock_path = root / LOCK_NAME
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        lock_info = os.fstat(lock_fd)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                or lock_info.st_nlink != 1 or lock_info.st_mode & 0o077):
            raise ValueError("Source admission lock must be a private owned regular file")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Source installation still has participating writers") from error
        current = lock_path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (lock_info.st_dev, lock_info.st_ino):
            raise ValueError("Source admission lock changed")
        capability_sha256, _ = _verify_capability(root, lock_info)
        info = root.stat()
        value = {
            "format": FORMAT,
            "operation_id": operation_id,
            "state": "source_frozen",
            "home_identity": hashlib.sha256(str(root).encode()).hexdigest(),
            "directory_identity": {"device": info.st_dev, "inode": info.st_ino},
            "admission_capability_sha256": capability_sha256,
            "installation_cutover_performed": False,
            "external_writers_fenced": False,
        }
        raw = _encoded(value)
        marker, part = root / MARKER_NAME, root / PART_NAME
        if marker.exists() or marker.is_symlink():
            has_part = part.exists() or part.is_symlink()
            recorded = _read(marker, links=2 if has_part else 1)
            if recorded != raw:
                raise ValueError("Source freeze operation or Home identity changed")
            if has_part:
                staged = _read(part, links=2)
                marker_info = marker.stat(follow_symlinks=False)
                part_info = part.stat(follow_symlinks=False)
                if (staged != raw or (marker_info.st_dev, marker_info.st_ino)
                        != (part_info.st_dev, part_info.st_ino)):
                    raise ValueError("Source freeze publication part mismatch")
                part.unlink()
        else:
            if part.exists() or part.is_symlink():
                if _read(part) != raw:
                    raise ValueError("Incomplete or conflicting source freeze publication")
            else:
                fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    offset = 0
                    while offset < len(raw):
                        offset += os.write(fd, raw[offset:])
                    os.fsync(fd)
                finally:
                    os.close(fd)
            _sync_directory(root)
            os.link(part, marker)
            _sync_directory(root)
            part.unlink()
        _sync_directory(root)
        current = lock_path.stat(follow_symlinks=False)
        if ((current.st_dev, current.st_ino) != (lock_info.st_dev, lock_info.st_ino)
                or _read(marker) != raw):
            raise ValueError("Source freeze publication changed")
        return verify_source_fence(root, operation_id)
    finally:
        os.close(lock_fd)


def verify_source_fence(source_home, operation_id: str) -> dict:
    """Return a path-free receipt for one exact live legacy freeze marker."""
    root = _root(source_home)
    capability_sha256, _ = _verify_capability(root)
    if (root / PART_NAME).exists() or (root / PART_NAME).is_symlink():
        raise ValueError("Source freeze publication is incomplete")
    marker = root / MARKER_NAME
    if not marker.exists() or marker.is_symlink():
        raise ValueError("Source installation is not persistently frozen")
    raw = _read(marker)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Source freeze marker is invalid") from error
    expected_keys = {
        "format", "operation_id", "state", "home_identity", "directory_identity",
        "admission_capability_sha256",
        "installation_cutover_performed", "external_writers_fenced",
    }
    info = root.stat()
    if (not isinstance(value, dict) or set(value) != expected_keys
            or raw != _encoded(value) or value["format"] != FORMAT
            or value["operation_id"] != operation_id or value["state"] != "source_frozen"
            or value["home_identity"] != hashlib.sha256(str(root).encode()).hexdigest()
            or value["directory_identity"] != {"device": info.st_dev, "inode": info.st_ino}
            or value["admission_capability_sha256"] != capability_sha256
            or value["installation_cutover_performed"] is not False
            or value["external_writers_fenced"] is not False):
        raise ValueError("Source freeze marker does not bind this operation and Home")
    return {
        "format": FORMAT,
        "operation_id": operation_id,
        "state": "source_frozen",
        "source_home_identity": value["home_identity"],
        "source_freeze_receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "admission_capability_sha256": capability_sha256,
        "legacy_writer_fenced": True,
    }
