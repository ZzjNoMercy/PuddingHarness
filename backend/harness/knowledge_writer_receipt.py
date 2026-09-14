"""Validate receipts emitted by the independent Knowledge writer authority.

This module deliberately contains no Knowledge imports.  It validates the
published binding, authority journal, and (for suspension) the durable marker
using only the shared Harness file primitives and the documented protocol.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from harness.installation_authority import _path, digest, identity, read


BINDING = ".workspace-authority-v1.json"
FORMAT = "puddingknowledge-writer-authority/v1"
FREEZE_FORMAT = "puddingknowledge-workspace-freeze/v1"
FREEZE_NAME = ".workspace-freeze-v1.json"
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")


def _fail(message: str) -> None:
    raise ValueError(message)


def _private_bytes(path: Path, limit: int = 1024 * 1024) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit):
            _fail("private bounded regular file required")
        raw = bytearray()
        while len(raw) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > limit:
            _fail("file exceeds size budget")
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_size) != (info.st_dev, info.st_ino, len(raw)):
            _fail("file changed during inspection")
        return bytes(raw)
    finally:
        os.close(fd)


def _parse_json(raw: bytes, *, canonical: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=lambda pairs: _unique(pairs))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("receipt is not JSON") from exc
    if not isinstance(value, dict):
        _fail("receipt must be an object")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
    if canonical and encoded != raw:
        _fail("receipt JSON must be canonical")
    return value


def _unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            _fail("duplicate JSON key")
        result[key] = value
    return result


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _operation(value: Any) -> None:
    if not isinstance(value, str) or not _OPERATION.fullmatch(value):
        _fail("invalid authority operation")


def _validate_catalog(workspace: Path) -> None:
    path = workspace / "catalog.sqlite3"
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        _fail("Catalog must be private, owned and unlinked")


def inspect_binding(workspace: Path) -> dict[str, Any]:
    """Read and strictly validate the persisted Knowledge workspace binding."""
    root = _path(workspace)
    if not root.is_dir():
        _fail("workspace must be an existing directory")
    part = root / (BINDING + ".part")
    if part.exists() or part.is_symlink():
        _fail("authority enrollment is incomplete")
    binding = read(root / BINDING)
    if (set(binding) != {"format", "workspace", "authority", "enrollment_id", "workspace_manifest_sha256"}
            or binding["format"] != FORMAT or binding["workspace"] != identity(root)):
        _fail("Knowledge workspace binding changed")
    _operation(binding["enrollment_id"])
    _validate_catalog(root)
    manifest = _private_bytes(root / "workspace.json")
    if binding["workspace_manifest_sha256"] != _sha(manifest):
        _fail("workspace manifest changed")
    authority = _path(binding["authority"]["path"])
    if binding["authority"] != identity(authority):
        _fail("authority directory changed")
    return binding


def _journal(binding: dict[str, Any]) -> dict[str, Any]:
    authority = _path(binding["authority"]["path"])
    value = read(authority / "journal.json")
    if (set(value) != {"format", "binding_sha256", "events"}
            or value["format"] != FORMAT or value["binding_sha256"] != digest(binding)):
        _fail("authority journal binding mismatch")
    events = value["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= 2:
        _fail("invalid authority history")
    previous = None
    for number, event in enumerate(events):
        expected = {"revision", "previous", "operation_id", "state", "writers", "freeze_receipt_sha256", "sha256"}
        if (not isinstance(event, dict) or set(event) != expected or type(event["revision"]) is not int
                or event["revision"] != number or event["previous"] != previous):
            _fail("invalid authority revision chain")
        payload = {key: item for key, item in event.items() if key != "sha256"}
        if event["sha256"] != digest(payload):
            _fail("authority revision digest mismatch")
        _operation(event["operation_id"])
        if number == 0:
            if (event["state"] != "existing_writer"
                    or event["writers"] != {"knowledge_catalog": "puddingknowledge", "connector_jobs": "puddingknowledge"}
                    or event["freeze_receipt_sha256"] is not None
                    or event["operation_id"] != binding["enrollment_id"]):
                _fail("invalid existing writer enrollment")
        elif (event["state"] != "suspended"
              or event["writers"] != {"knowledge_catalog": None, "connector_jobs": None}
              or not isinstance(event["freeze_receipt_sha256"], str)
              or not re.fullmatch(r"[0-9a-f]{64}", event["freeze_receipt_sha256"])):
            _fail("invalid suspended authority")
        previous = event["sha256"]
    return value


def _validate_freeze(workspace: Path, binding: dict[str, Any], event: dict[str, Any]) -> None:
    part = workspace / (FREEZE_NAME + ".part")
    if part.exists() or part.is_symlink():
        _fail("freeze publication is incomplete")
    raw = _private_bytes(workspace / FREEZE_NAME, 4096)
    if _sha(raw) != event["freeze_receipt_sha256"]:
        _fail("freeze receipt commitment changed")
    marker = _parse_json(raw, canonical=True)
    expected = {"activation_allowed", "directory_identity", "format", "operation_id",
                "workspace_manifest_sha256", "root_path_sha256", "state"}
    if set(marker) != expected or marker["format"] != FREEZE_FORMAT or marker["state"] != "workspace_frozen":
        _fail("unsupported Knowledge freeze marker")
    if marker["activation_allowed"] is not False or marker["operation_id"] != event["operation_id"]:
        _fail("freeze marker operation or authority changed")
    if marker["root_path_sha256"] != _sha(str(workspace).encode()) or marker["workspace_manifest_sha256"] != binding["workspace_manifest_sha256"]:
        _fail("freeze marker target changed")
    info = workspace.lstat()
    if marker["directory_identity"] != {"device": info.st_dev, "inode": info.st_ino}:
        _fail("freeze marker directory changed")


def validate_receipt(raw: bytes, workspace: Path, binding: dict[str, Any], operation_id: str | None = None) -> dict[str, Any]:
    """Validate one successful status/suspend CLI receipt and return its journal."""
    if not isinstance(raw, bytes):
        _fail("receipt must be bytes")
    value = _parse_json(raw)
    if (set(value) != {"format", "status", "journal", "installation_cutover_performed"}
            or value["format"] != FORMAT or value["status"] != "ok"
            or value["installation_cutover_performed"] is not False):
        _fail("unsupported writer authority receipt")
    actual_binding = inspect_binding(workspace)
    if binding != actual_binding:
        _fail("supplied binding does not match persisted binding")
    actual = _journal(actual_binding)
    if value["journal"] != actual:
        _fail("receipt journal does not match persisted journal")
    events = actual["events"]
    if operation_id is not None:
        _operation(operation_id)
        if len(events) != 2 or events[-1]["state"] != "suspended" or events[-1]["operation_id"] != operation_id:
            _fail("operation requires exact suspended revision 1")
    if len(events) == 2:
        _validate_freeze(_path(workspace), actual_binding, events[-1])
    return actual

