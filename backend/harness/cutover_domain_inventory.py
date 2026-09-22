"""Produce session_harness target inventory from verified inactive staging."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat

from harness import home_import, session_import


FORMAT = "puddingharness-cutover-domain-inventory/v1"
MAX_MANIFEST = 32 * 1024 * 1024


class DomainInventoryError(ValueError):
    pass


def _encode(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _path(value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise DomainInventoryError("inventory paths must be absolute and unlinked")
    return path.resolve()


def _private(info: os.stat_result, *, directory: bool = False) -> None:
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.getuid() or info.st_mode & 0o077 or (not directory and info.st_nlink != 1):
        raise DomainInventoryError("inventory evidence must be private and owned")


def _read(path: Path, limit: int = MAX_MANIFEST) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        _private(before)
        if before.st_size > limit:
            raise DomainInventoryError("inventory evidence exceeds its byte budget")
        chunks, size = [], 0
        while chunk := os.read(descriptor, min(65536, limit + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise DomainInventoryError("inventory evidence exceeds its byte budget")
        after = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size) or (after.st_dev, after.st_ino, after.st_size) != (current.st_dev, current.st_ino, current.st_size):
            raise DomainInventoryError("inventory evidence changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise DomainInventoryError("inventory manifest contains a duplicate key")
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DomainInventoryError("inventory manifest is invalid JSON") from error
    if not isinstance(value, dict):
        raise DomainInventoryError("inventory manifest must be an object")
    return value


def _publish(path_value: Path | str, value: dict) -> None:
    path = _path(path_value)
    if not path.parent.is_dir():
        raise DomainInventoryError("inventory output parent is unavailable")
    _private(path.parent.stat(), directory=True)
    raw = _encode(value)
    if path.exists():
        if _read(path) != raw:
            raise DomainInventoryError("existing inventory receipt disagrees")
        return
    part = _path(str(path) + ".part")
    if part.exists():
        if _read(part) != raw:
            raise DomainInventoryError("interrupted inventory publication disagrees")
    else:
        descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    os.link(part, path)
    part.unlink()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def produce_inventory(*, staging: Path | str, output: Path | str) -> dict:
    root = _path(staging)
    _private(root.stat(), directory=True)
    manifest_raw = _read(root / "manifest.json")
    manifest = _json(manifest_raw)
    expected = {
        "format", "plan_digest", "plan", "state", "activation_allowed",
        "writer_fence_verified", "credential_rebind_required",
    }
    if (
        set(manifest) != expected
        or manifest.get("format") != home_import.FORMAT
        or manifest.get("state") != "verified_inactive"
        or manifest.get("activation_allowed") is not False
        or manifest.get("writer_fence_verified") is not False
        or manifest.get("credential_rebind_required") is not True
    ):
        raise DomainInventoryError("session staging is not verified inactive")
    plan = manifest.get("plan")
    if (
        not isinstance(plan, dict)
        or set(plan) != {"format", "source_identity", "snapshot", "files"}
        or plan.get("format") != home_import.FORMAT
    ):
        raise DomainInventoryError("session staging plan is invalid")
    if manifest.get("plan_digest") != session_import.digest(session_import.encoded(plan)):
        raise DomainInventoryError("session staging plan digest disagrees")
    files = plan.get("files")
    snapshot = plan.get("snapshot")
    if (
        not isinstance(files, dict)
        or not isinstance(snapshot, dict)
        or set(snapshot) != {
            "source_config_digest", "session_files", "settings_sections",
            "untransferred_section_count",
        }
        or not isinstance(snapshot.get("session_files"), dict)
    ):
        raise DomainInventoryError("session staging has no owned objects")
    session_files = snapshot["session_files"]
    if set(files) != {*session_files, "config.json"}:
        raise DomainInventoryError("session staging plan has unexpected objects")
    home_import._check_stage(root, files)
    for relative, fact in files.items():
        data = session_import.read_file(root / "payload" / relative)
        if fact != {"digest": session_import.digest(data), "size": len(data)}:
            raise DomainInventoryError("session staging payload disagrees with its manifest")
    actual = session_import.inventory(root / "payload", require_sessions=False)
    if actual != session_files:
        raise DomainInventoryError("session staging payload disagrees with its manifest")
    inventory = sorted(
        "file:" + hashlib.sha256(_encode({"path": relative, "sha256": fact["digest"].removeprefix("sha256:"), "size": fact["size"]})).hexdigest()
        for relative, fact in session_files.items()
    )
    target_artifact = _digest(_encode({"manifest_sha256": _digest(manifest_raw), "files": files}))
    receipt = {"format": FORMAT, "producer": "puddingharness", "target_artifact_sha256": target_artifact,
               "inventory": inventory, "inventory_sha256": _digest(_encode(inventory))}
    _publish(output, receipt)
    if (
        session_import.inventory(root / "payload", require_sessions=False) != session_files
        or _read(root / "manifest.json") != manifest_raw
    ):
        raise DomainInventoryError("session staging changed during inventory production")
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = produce_inventory(staging=args.staging, output=args.output)
    except Exception:
        print(json.dumps({"format": FORMAT, "status": "error", "error_code": "domain_inventory_rejected"}, sort_keys=True))
        return 1
    print(json.dumps({"format": FORMAT, "inventory_sha256": result["inventory_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
