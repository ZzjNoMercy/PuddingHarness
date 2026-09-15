"""Offline Harness/Knowledge migration orchestration boundary.

This module deliberately treats the Knowledge request and receipt as opaque
bytes/JSON.  Knowledge validation remains in the delegated Platform process.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import re
import selectors
import time
import stat
import subprocess

from harness.home_import import prepare_home_import

FORMAT = "puddingknowledge-migrate-from-claw-receipt/v1"
_MAX_JSON = 1024 * 1024
_MAX_ARTIFACT = 64 * 1024 * 1024
_MAX_TOTAL = 256 * 1024 * 1024
_HEX = "0123456789abcdef"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("JSON must be an object")
    return value


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _path(value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("migration paths must be absolute")
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("migration path contains a symlink")
    return path


def _private_directory(path: Path) -> None:
    if not path.is_dir() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("migration directory must be private")


def _read_private(path: Path, limit: int = _MAX_JSON) -> bytes:
    path = _path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit:
            raise ValueError("migration file is not private and bounded")
        chunks = []; size = 0
        while size <= limit:
            chunk = os.read(fd, min(65536, limit+1-size))
            if not chunk: break
            chunks.append(chunk); size += len(chunk)
        data = b''.join(chunks)
        if len(data) > limit:
            raise ValueError("migration file is too large")
        return data
    finally:
        os.close(fd)


def _write_private(path: Path, data: bytes) -> None:
    path = _path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_private(path: Path, data: bytes) -> None:
    path = _path(path)
    if path.exists():
        _read_private(path)
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    if temporary.exists():
        raise ValueError("stale private migration temporary exists")
    _write_private(temporary, data)
    os.replace(temporary, path)
    _sync_parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(_sync_parent)
    finally:
        os.close(_sync_parent)


def _check_stage(stage: Path) -> None:
    allowed = {".orchestrator.lock", "plan.json", "checkpoint.json", "knowledge-request.json", "harness", "knowledge"}
    for entry in stage.iterdir():
        temporary = re.fullmatch(r"\.(?:plan.json|checkpoint.json|knowledge-request.json)\.tmp-[0-9a-f]{16}", entry.name)
        if temporary:
            _read_private(entry)
        if entry.name not in allowed and not temporary:
            raise ValueError("unknown migration staging entry")
        if entry.is_symlink():
            raise ValueError("migration staging entry is symlinked")
    for name in ("plan.json", "checkpoint.json", "knowledge-request.json"):
        item = stage / name
        if item.exists():
            _read_private(item)
    for name in ("harness", "knowledge"):
        item = stage / name
        if item.exists():
            _private_directory(item)


def _validate_executable(value: Path | str) -> Path:
    requested = Path(value).expanduser()
    if not requested.is_absolute() or ".." in requested.parts:
        raise ValueError("knowledge_python must be absolute")
    if any(item.is_symlink() for item in requested.parents):
        raise ValueError("knowledge_python resolves through a symlinked path")
    resolved = requested.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(requested, os.X_OK):
        raise ValueError("knowledge_python must be a private executable")
    return requested


def _receipt(raw: bytes, output: Path, request_digest: str, snapshot_identity: str) -> dict:
    value = _json(raw)
    required = {"format", "request_digest", "source_snapshot_identity", "state", "artifacts", "covered_domains", "pending_domains",
                "activation_allowed", "installation_prepared", "writer_fence_verified", "credential_rebind_required"}
    if set(value) != required or value["format"] != FORMAT or value["request_digest"] != request_digest or value["source_snapshot_identity"] != snapshot_identity:
        raise ValueError("Knowledge receipt shape or request binding is invalid")
    covered, pending = value["covered_domains"], value["pending_domains"]
    if value["state"] != "verified_inactive_partial" or not _labels(covered) or not _labels(pending) or set(covered) & set(pending):
        raise ValueError("Knowledge receipt domains or state are invalid")
    if any(type(value[key]) is not bool or value[key] is not expected for key, expected in (
        ("activation_allowed", False), ("installation_prepared", False),
        ("writer_fence_verified", False), ("credential_rebind_required", True))):
        raise ValueError("Knowledge receipt safety flags are invalid")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, dict) or not 1 <= len(artifacts) <= 10000:
        raise ValueError("Knowledge receipt artifacts are invalid")
    total = 0
    for relative, expected in artifacts.items():
        if not isinstance(relative, str) or not relative or Path(relative).as_posix() != relative or Path(relative).is_absolute() or any(part in {"", ".", ".."} for part in Path(relative).parts) or relative.startswith("/"):
            raise ValueError("Knowledge artifact path is not portable")
        if not isinstance(expected, str) or len(expected) != 71 or not expected.startswith("sha256:") or any(c not in _HEX for c in expected[7:]):
            raise ValueError("Knowledge artifact digest is invalid")
        target = output / relative
        data = _read_private(target, _MAX_ARTIFACT)
        total += len(data)
        if total > _MAX_TOTAL or _digest(data) != expected:
            raise ValueError("Knowledge artifact changed or exceeds budget")
    return value


def _labels(value):
    return isinstance(value, list) and bool(value) and len(value) <= 64 and all(
        isinstance(item, str) and 1 <= len(item) <= 80 and item.replace("_", "").isalnum() for item in value
    ) and len(set(value)) == len(value)


def _delegate(command, stage, lock_fd, timeout_seconds, additional_fds=()):
    """Bound child output while retaining the shared lock across parent death."""
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, pass_fds=(lock_fd, *additional_fds), cwd=str(stage),
        env={"PATH": os.defpath, "HOME": str(stage)})
    deadline = time.monotonic()+timeout_seconds
    data = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline-time.monotonic()
                if remaining <= 0: raise ValueError("Knowledge migration timed out")
                if not selector.select(remaining): raise ValueError("Knowledge migration timed out")
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk: break
                data.extend(chunk)
                if len(data) > _MAX_JSON: raise ValueError("Knowledge receipt exceeds budget")
        if child.wait(timeout=max(.01, deadline-time.monotonic())) != 0:
            raise ValueError("Knowledge migration process failed")
        return bytes(data)
    except subprocess.TimeoutExpired as error:
        raise ValueError("Knowledge migration timed out") from error
    finally:
        if child.poll() is None: child.kill(); child.wait(timeout=5)
        child.stdout.close()


def _installed_knowledge_identity(python, stage, lock_fd, timeout_seconds, additional_fds=()):
    raw = _delegate([str(python), '-m', 'knowledge_platform.distribution.installed_identity'],
                    stage, lock_fd, timeout_seconds, additional_fds=additional_fds)
    value = _json(raw)
    expected = {'format', 'package', 'version', 'inventory_sha256', 'file_count', 'scope', 'authenticated'}
    if (set(value) != expected or value['format'] != 'puddingknowledge-installed-identity/v1'
            or value['package'] != 'puddingknowledge-local'
            or not isinstance(value['version'], str)
            or not re.fullmatch(r'[0-9][A-Za-z0-9.!+_-]{0,79}', value['version'])
            or not isinstance(value['inventory_sha256'], str)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['inventory_sha256'])
            or type(value['file_count']) is not int or not 2 <= value['file_count'] <= 10000
            or value['scope'] != 'owned_distribution_files' or value['authenticated'] is not False):
        raise ValueError('Invalid installed Knowledge identity receipt')
    return value


def prepare_migration(source_snapshot: Path | str, knowledge_request: bytes, knowledge_python: Path | str,
                      staging: Path | str, *, timeout_seconds: int = 120, _after_checkpoint=None,
                      source_home_snapshot: Path | str | None = None) -> dict:
    if source_home_snapshot is None:
        return _prepare_migration(source_snapshot, knowledge_request, knowledge_python, staging,
            timeout_seconds=timeout_seconds, _after_checkpoint=_after_checkpoint)
    from harness.source_snapshot import VerifiedSourceSnapshot
    with VerifiedSourceSnapshot(source_home_snapshot) as snapshot:
        if _path(source_snapshot) != snapshot.payload:
            raise ValueError('Source payload does not belong to the admitted snapshot')
        stage = _path(staging)
        if stage == snapshot.root or stage.is_relative_to(snapshot.root) or snapshot.root.is_relative_to(stage):
            raise ValueError('Migration staging must be disjoint from the snapshot envelope')
        return _prepare_migration(snapshot.payload, knowledge_request, knowledge_python, staging,
            timeout_seconds=timeout_seconds, _after_checkpoint=_after_checkpoint, snapshot_guard=snapshot)


def _prepare_migration(source_snapshot: Path | str, knowledge_request: bytes, knowledge_python: Path | str,
                      staging: Path | str, *, timeout_seconds: int = 120, _after_checkpoint=None,
                      snapshot_guard=None) -> dict:
    if not isinstance(knowledge_request, bytes) or not knowledge_request or len(knowledge_request) > _MAX_JSON:
        raise ValueError("Knowledge request must be non-empty bounded bytes")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 3600:
        raise ValueError("timeout_seconds is invalid")
    source, stage, python = _path(source_snapshot), _path(staging), _validate_executable(knowledge_python)
    if source == stage or source.is_relative_to(stage) or stage.is_relative_to(source) or not source.is_dir():
        raise ValueError("source and staging must be distinct and disjoint directories")
    if not stage.exists():
        stage.mkdir(mode=0o700, parents=True)
    _private_directory(stage)
    _check_stage(stage)
    lock_path = stage / ".orchestrator.lock"
    if lock_path.exists(): _read_private(lock_path)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError("orchestrator lock is hard-linked")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _check_stage(stage)
        for entry in stage.iterdir():
            if re.fullmatch(r"\.(?:plan.json|checkpoint.json|knowledge-request.json)\.tmp-[0-9a-f]{16}", entry.name):
                _read_private(entry); entry.unlink()
        request_digest = _digest(knowledge_request)
        request_path = stage / "knowledge-request.json"
        if request_path.exists() and _read_private(request_path) != knowledge_request:
            raise ValueError("Knowledge request changed")
        if not request_path.exists():
            _replace_private(request_path, knowledge_request)
        from harness.target_freeze import _executable_identity
        executable_identity = _executable_identity(python)
        identity_fds = (snapshot_guard.fd,) if snapshot_guard is not None else ()
        release_identity = _installed_knowledge_identity(python, stage, fd, timeout_seconds, identity_fds)
        if _executable_identity(python) != executable_identity:
            raise ValueError('Knowledge executable changed during identity inspection')
        plan = {"format": "puddingharness-migration-orchestrator/v1", "request_digest": request_digest,
                "source_identity": _digest(str(source).encode()), "knowledge_python_identity": _digest(str(python).encode()),
                "knowledge_executable": executable_identity, "knowledge_release_identity": release_identity}
        if snapshot_guard is not None:
            plan['source_snapshot_commitment'] = snapshot_guard.commitment
        plan_digest = _digest(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode())
        plan_path = stage / "plan.json"
        if plan_path.exists() and _json(_read_private(plan_path)) != {**plan, "plan_digest": plan_digest}:
            raise ValueError("migration plan changed")
        if not plan_path.exists():
            _replace_private(plan_path, json.dumps({**plan, "plan_digest": plan_digest}, sort_keys=True).encode())
        checkpoint_path = stage / "checkpoint.json"
        checkpoint = _json(_read_private(checkpoint_path)) if checkpoint_path.exists() else {"state": "preparing", "plan_digest": plan_digest}
        original_checkpoint = dict(checkpoint)
        expected_checkpoint = {"state", "plan_digest"}
        if checkpoint.get("state") in {"harness_verified", "verified_inactive_partial"}:
            expected_checkpoint.add("harness_plan_digest")
        if checkpoint.get("state") == "verified_inactive_partial":
            expected_checkpoint.add("receipt_digest")
        if set(checkpoint) != expected_checkpoint or checkpoint.get("plan_digest") != plan_digest:
            raise ValueError("migration checkpoint is invalid")
        if checkpoint.get("state") not in {"preparing", "harness_verified", "verified_inactive_partial"}:
            raise ValueError("migration checkpoint state is invalid")
        if not checkpoint_path.exists():
            _replace_private(checkpoint_path, json.dumps(checkpoint, sort_keys=True).encode())
        if _after_checkpoint:
            _after_checkpoint("preparing")
        harness = stage / "harness"
        home_result = prepare_home_import(source, harness)
        checkpoint = {"state": "harness_verified", "plan_digest": plan_digest,
                      "harness_plan_digest": home_result["plan_digest"]}
        if original_checkpoint.get("harness_plan_digest") is not None and original_checkpoint["harness_plan_digest"] != home_result["plan_digest"]:
            raise ValueError("Harness checkpoint changed during resume")
        if original_checkpoint['state'] != 'verified_inactive_partial':
            _replace_private(checkpoint_path, json.dumps(checkpoint, sort_keys=True).encode())
        if _after_checkpoint:
            _after_checkpoint("harness_verified")
        knowledge = stage / "knowledge"
        if knowledge.exists():
            _private_directory(knowledge)
        else:
            knowledge.mkdir(mode=0o700)
        command = [str(python), "-m", "knowledge_platform.distribution.migrate_from_claw", "--source-snapshot", str(source), "--request", str(request_path), "--output", str(knowledge)]
        if snapshot_guard is None:
            raw_receipt = _delegate(command, stage, fd, timeout_seconds)
        else:
            raw_receipt = _delegate(command, stage, fd, timeout_seconds, additional_fds=(snapshot_guard.fd,))
        if (_executable_identity(python) != executable_identity
                or _installed_knowledge_identity(python, stage, fd, timeout_seconds, identity_fds) != release_identity):
            raise ValueError('Installed Knowledge release changed during migration')
        result = _receipt(raw_receipt, knowledge, request_digest, plan["source_identity"])
        if _read_private(request_path) != knowledge_request:
            raise ValueError("Staged request changed during delegation")
        refreshed_home = prepare_home_import(source, harness)
        if refreshed_home["plan_digest"] != home_result["plan_digest"]:
            raise ValueError("Harness source changed during Knowledge migration")
        receipt_digest = _digest(json.dumps(result, sort_keys=True, separators=(",", ":")).encode())
        prior_receipt = original_checkpoint.get("receipt_digest")
        if original_checkpoint.get("harness_plan_digest") is not None and original_checkpoint["harness_plan_digest"] != home_result["plan_digest"]:
            raise ValueError("Harness checkpoint changed during resume")
        if prior_receipt is not None and prior_receipt != receipt_digest:
            raise ValueError("Knowledge receipt changed during resume")
        if snapshot_guard is not None:
            snapshot_guard.verify()
        _replace_private(checkpoint_path, json.dumps({"state": "verified_inactive_partial", "plan_digest": plan_digest, "receipt_digest": receipt_digest, "harness_plan_digest": home_result["plan_digest"]}, sort_keys=True).encode())
        if _after_checkpoint:
            _after_checkpoint("verified_inactive_partial")
        return {"format": "puddingharness-migration-orchestrator/v1", "state": "verified_inactive_partial",
                "plan_digest": plan_digest, "harness_plan_digest": home_result["plan_digest"], "knowledge_receipt": result,
                **({'source_snapshot_commitment': snapshot_guard.commitment} if snapshot_guard is not None else {}),
                "activation_allowed": False, "installation_prepared": False,
                "writer_fence_verified": False, "credential_rebind_required": True}
    finally:
        os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser()
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--source-snapshot", type=Path, help="Explicit offline payload without raw-snapshot envelope admission")
    sources.add_argument("--source-home-snapshot", type=Path, help="Verify and bind a raw Claw Home snapshot envelope")
    parser.add_argument("--knowledge-request", type=Path, required=True)
    parser.add_argument("--knowledge-python", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    args = parser.parse_args(argv)
    try:
        request = _read_private(args.knowledge_request)
        result = prepare_migration(args.source_home_snapshot / 'payload' if args.source_home_snapshot else args.source_snapshot,
            request, args.knowledge_python, args.staging, timeout_seconds=args.timeout_seconds,
            source_home_snapshot=args.source_home_snapshot)
    except Exception:
        print(json.dumps({"format": "puddingharness-migration-orchestrator/v1", "status": "error", "error_code": "migration_rejected", "activation_allowed": False}, separators=(",", ":")))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
