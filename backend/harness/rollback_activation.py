"""Install and activate a verified rollback candidate in a frozen legacy Home.

The rollback orchestrator's ``both_reassigned`` checkpoint is authority evidence,
not restoration completion.  This module consumes that checkpoint and performs
the remaining fail-closed work under the legacy installation gate: install the
candidate at its final path, rebind paths, rebuild SQLite indexes, verify the
pre-cutover credential inventory, atomically replace the legacy Catalog, probe
the installed data, and only then retire the source freeze marker.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

from harness import rollback_orchestrator, source_writer_fence

FORMAT = "puddingharness-rollback-activation/v1"
BASELINE_FORMAT = "puddingclaw-credential-baseline/v1"
READY_FORMAT = "puddingclaw-rollback-activation-ready/v1"
ACTIVATION_FORMAT = "puddingclaw-rollback-activation-receipt/v1"
EVIDENCE_FORMAT = "puddingknowledge-rollback-evidence/v1"
DOCUMENT_REVERSE_FORMAT = "puddingknowledge-document-reverse/v5"
LOCK_NAME = ".rollback-activation.lock"
PLAN_NAME = "plan.json"
CHECKPOINT_NAME = "checkpoint.json"
REBINDS_NAME = "installation-path-rebind.json"
INDEX_NAME = "index-rebuild.json"
CATALOG_NAME = "catalog-activation.json"
POINTERS_NAME = "active-pointer-retirement.json"
READY_NAME = "legacy-ready.json"
ACTIVATION_NAME = "legacy-activation.json"
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ORDER = ("candidate_installed", "paths_rebound", "indexes_rebuilt",
          "credentials_verified", "catalog_activated", "legacy_health_verified",
          "active_pointers_retired", "legacy_thawed", "rollback_completed")
_MAX_JSON = 32 * 1024 * 1024
_MAX_FILES = 100_000
_MAX_TOTAL = 8 * 1024 * 1024 * 1024


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _read(path, limit=_MAX_JSON):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError("Protocol file must be private, owned, single-linked, and bounded")
        raw = os.read(fd, limit + 1)
        current = path.stat(follow_symlinks=False)
        if (len(raw) > limit or (info.st_dev, info.st_ino, info.st_size)
                != (current.st_dev, current.st_ino, len(raw))):
            raise ValueError("Protocol file changed")
        return raw
    finally:
        os.close(fd)


def _json(path, limit=_MAX_JSON):
    raw = _read(path, limit)
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value, dict) or _encoded(value) != raw:
        raise ValueError("Protocol JSON must be canonical")
    return value, raw


def _sync(root):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, value):
    data = _encoded(value)
    if len(data) > _MAX_JSON:
        raise ValueError("Protocol receipt exceeds its budget")
    part = path.with_name("." + path.name + ".part")
    if part.exists() or part.is_symlink():
        if part.is_symlink() or not part.is_file():
            raise ValueError("Protocol publication part is invalid")
        part.unlink()
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(fd, data[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(part, path)
    _sync(path.parent)
    return data


def _root(value, *, existing=True):
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("Rollback activation paths must be absolute and unlinked")
    if existing:
        path = path.resolve(strict=True)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Rollback activation roots must be private owned directories")
    return path


def _inventory(root, *, selected=None):
    files, directories, total = {}, [], 0
    prefixes = tuple(selected or ())
    for base, names, filenames in os.walk(root, followlinks=False):
        current = Path(base)
        for name in sorted(names):
            path = current / name
            if path.is_symlink():
                raise ValueError("Credential or candidate tree contains a symlink")
            relative = path.relative_to(root).as_posix()
            if prefixes and not any(relative == item or relative.startswith(item + "/") for item in prefixes):
                continue
            directories.append(relative)
        for name in sorted(filenames):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if prefixes and not any(relative == item or relative.startswith(item + "/") for item in prefixes):
                continue
            if path.is_symlink():
                raise ValueError("Credential or candidate tree contains a symlink")
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise ValueError("Credential or candidate entry is not a private owned regular file")
            raw = _read(path, max(_MAX_JSON, info.st_size))
            total += len(raw)
            if len(files) >= _MAX_FILES or total > _MAX_TOTAL:
                raise ValueError("Credential or candidate inventory exceeds its budget")
            files[relative] = {"sha256": _sha(raw), "size_bytes": len(raw)}
    return {"files": dict(sorted(files.items())), "directories": sorted(set(directories))}


def _credential_inventory(home):
    return _inventory(home, selected=(".vault-keys", "users", "skill-secrets"))


def _gate(home):
    lock = home / source_writer_fence.LOCK_NAME
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        os.close(fd)
        raise ValueError("Legacy installation gate is invalid")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    current = lock.stat(follow_symlinks=False)
    if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
        os.close(fd)
        raise ValueError("Legacy installation gate changed")
    return fd


def capture_credential_baseline(source_home, output, *, operation_id):
    if not isinstance(operation_id, str) or _OPERATION.fullmatch(operation_id) is None:
        raise ValueError("Invalid credential baseline operation")
    home = _root(source_home)
    output = Path(output).expanduser()
    parent = output.parent.resolve(strict=True)
    if (not output.is_absolute() or output.is_symlink()
            or parent == home or parent.is_relative_to(home)):
        raise ValueError("Credential baseline output must be outside the legacy Home")
    fd = _gate(home)
    try:
        value = {"format": BASELINE_FORMAT, "operation_id": operation_id,
                 "source_home_identity": hashlib.sha256(str(home).encode()).hexdigest(),
                 "inventory": _credential_inventory(home)}
        existed = output.exists()
        if existed:
            current, _ = _json(output)
            if current != value:
                raise ValueError("Credential baseline changed")
        else:
            _write(output, value)
        return {**value, "credential_baseline_sha256": _sha(_read(output)), "idempotent": existed}
    finally:
        os.close(fd)


def _rollback_checkpoint(path, operation_id):
    value, raw = _json(path)
    facts = rollback_orchestrator.validate_completed_checkpoint(value, operation_id)
    return value, raw, facts


def _verify_rollback_evidence(path, checkpoint, candidate, candidate_manifest, operation_id):
    value, raw = _json(path)
    if (_sha(raw) != checkpoint["rollback_evidence_sha256"]
            or value.get("format") != EVIDENCE_FORMAT
            or value.get("state") != "verified_rollback_evidence"
            or value.get("operation_id") != operation_id):
        raise ValueError("Rollback evidence does not bind the authority checkpoint")
    for flag in ("rollback_completed", "activation_allowed",
                 "installation_cutover_performed", "indexes_rebuilt"):
        if value.get(flag) is not False:
            raise ValueError("Rollback evidence is not an inert verified envelope")
    if (candidate_manifest.get("format") != DOCUMENT_REVERSE_FORMAT
            or candidate_manifest.get("state") != "verified_inactive_documents"
            or not isinstance(candidate_manifest.get("plan"), dict)
            or not isinstance(candidate_manifest["plan"].get("output_inventory"), dict)):
        raise ValueError("Rollback candidate manifest is invalid")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 4:
        raise ValueError("Rollback evidence artifact commitments are invalid")
    documents = [item for item in artifacts if isinstance(item, dict)
                 and item.get("role") == "document_reverse"]
    if (len(documents) != 1
            or documents[0].get("state") != "verified_inactive_documents"
            or documents[0].get("sha256") != _sha(_read(candidate / "manifest.json"))):
        raise ValueError("Rollback evidence binds another document candidate")
    linkages = value.get("linkages")
    artifact_digests = value.get("artifact_digests")
    output_inventory = candidate_manifest["plan"]["output_inventory"]
    if (not isinstance(linkages, dict)
            or linkages.get("document_candidate_catalog_sha256")
                != _sha(_read(candidate / "catalog.sqlite3", _MAX_TOTAL))
            or not isinstance(artifact_digests, dict)
            or artifact_digests.get("document_bodies_inventory_sha256")
                != _sha(_encoded(output_inventory))
            or _inventory(candidate / "bodies") != output_inventory):
        raise ValueError("Rollback evidence candidate content commitment changed")
    return value, raw


def _verify_candidate_inventory(target, expected, *, catalog_may_be_rebound):
    actual = _inventory(target)
    if actual["directories"] != expected["directories"]:
        raise ValueError("Installed rollback candidate changed")
    expected_files = dict(expected["files"])
    actual_files = dict(actual["files"])
    if catalog_may_be_rebound:
        expected_files.pop("catalog.sqlite3", None)
        actual_files.pop("catalog.sqlite3", None)
    if actual_files != expected_files:
        raise ValueError("Installed rollback candidate changed")


def _copy_candidate(source, target, *, catalog_may_be_rebound=False):
    source_inventory = _inventory(source)
    required = {"manifest.json", "catalog.sqlite3", ".writer-authority.lock"}
    if not required.issubset(source_inventory["files"]) or "bodies" not in source_inventory["directories"]:
        raise ValueError("Rollback candidate is incomplete")
    part = target.with_name(target.name + ".part")
    if part.exists() or part.is_symlink():
        if part.is_symlink():
            raise ValueError("Rollback candidate publication part is symlinked")
        if not part.is_dir():
            raise ValueError("Rollback candidate publication part is invalid")
        shutil.rmtree(part)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise ValueError("Installed rollback candidate path is invalid")
        if _sha(_read(target / "manifest.json")) != _sha(_read(source / "manifest.json")):
            raise ValueError("Installed rollback candidate manifest changed")
        _verify_candidate_inventory(
            target, source_inventory, catalog_may_be_rebound=catalog_may_be_rebound)
        return source_inventory, True
    shutil.copytree(source, part, symlinks=False)
    for path in sorted(part.rglob("*")):
        if path.is_symlink():
            raise ValueError("Copied rollback candidate contains a symlink")
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    _sync(part)
    os.rename(part, target)
    _sync(target.parent)
    if _inventory(target) != source_inventory:
        raise ValueError("Installed rollback candidate changed during copy")
    return source_inventory, False


def _run_rebind(python, candidate, receipt):
    completed = subprocess.run(
        [str(python), "-m", "knowledge_platform.distribution.installation_path_rebind",
         "--candidate", str(candidate), "--manifest", str(candidate / "manifest.json"),
         "--output", str(receipt)], cwd="/private/tmp", stdin=subprocess.DEVNULL,
        capture_output=True, timeout=300, check=False,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    if completed.returncode != 0:
        raise ValueError("Knowledge installation path rebind rejected")
    try:
        result = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Knowledge installation path rebind returned invalid output") from error
    return result


def _validate_rebind(receipt, candidate, *, catalog_may_have_rebuilt_indexes=False):
    value, raw = _json(receipt)
    if (value.get("format") != "puddingknowledge-installation-path-rebind/v1"
            or value.get("state") != "verified_installation_path_rebound"
            or value.get("candidate") != str(candidate)
            or value.get("manifest_sha256") != _sha(_read(candidate / "manifest.json"))
            or value.get("installation_path_rebound") is not True
            or value.get("indexes_rebuilt") is not False
            or value.get("rollback_completed") is not False
            or value.get("activation_allowed") is not False
            or not isinstance(value.get("catalog_sha256_after"), str)
            or _HEX64.fullmatch(value["catalog_sha256_after"]) is None):
        raise ValueError("Installation path rebind receipt is invalid")
    if (not catalog_may_have_rebuilt_indexes
            and _sha(_read(candidate / "catalog.sqlite3", _MAX_TOTAL))
                != value["catalog_sha256_after"]):
        raise ValueError("Installation path rebind receipt is invalid")
    return value, raw


def _rebuild_indexes(catalog, receipt):
    if receipt.exists():
        value, raw = _json(receipt)
        if (value.get("format") != "puddingclaw-legacy-index-rebuild/v1"
                or value.get("indexes_rebuilt") is not True
                or value.get("catalog_sha256_after") != _sha(_read(catalog, _MAX_TOTAL))):
            raise ValueError("Index rebuild receipt changed")
        return value, raw, True
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(catalog) + suffix).exists():
            raise ValueError("Candidate Catalog is not quiescent")
    before = _sha(_read(catalog, _MAX_TOTAL))
    connection = sqlite3.connect(catalog)
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("REINDEX")
        connection.commit()
        quick = connection.execute("PRAGMA quick_check").fetchall()
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        connection.close()
    if quick != [("ok",)] or foreign:
        raise ValueError("Rebuilt legacy Catalog failed integrity checks")
    after = _sha(_read(catalog, _MAX_TOTAL))
    value = {"format": "puddingclaw-legacy-index-rebuild/v1", "state": "verified",
             "catalog_sha256_before": before, "catalog_sha256_after": after,
             "quick_check": "ok", "foreign_key_violations": 0,
             "indexes_rebuilt": True, "activation_allowed": False,
             "rollback_completed": False}
    raw = _write(receipt, value)
    return value, raw, False


def _verify_credentials(home, baseline):
    value, raw = _json(baseline)
    if (value.get("format") != BASELINE_FORMAT
            or value.get("source_home_identity") != hashlib.sha256(str(home).encode()).hexdigest()
            or value.get("inventory") != _credential_inventory(home)):
        raise ValueError("Legacy credential continuity is not verified")
    return {"format": "puddingclaw-rollback-credential-continuity/v1",
            "state": "verified", "baseline_sha256": _sha(raw),
            "credential_continuity_verified": True,
            "activation_allowed": False, "rollback_completed": False}


def _activate_catalog(home, catalog, stage):
    receipt = stage / CATALOG_NAME
    target_dir = home / "db"
    target_dir.mkdir(mode=0o700, exist_ok=True)
    # Older PuddingClaw homes may have created ``db`` under the process
    # umask (commonly 0755).  The Home itself is already private and the
    # installation gate is held exclusively, so harden this owned directory
    # before applying the private-root invariant instead of making rollback
    # impossible for those otherwise valid installations.
    if target_dir.is_symlink():
        raise ValueError("Legacy Catalog directory is symlinked")
    target_info = target_dir.stat(follow_symlinks=False)
    if not stat.S_ISDIR(target_info.st_mode) or target_info.st_uid != os.getuid():
        raise ValueError("Legacy Catalog directory is invalid")
    if target_info.st_mode & 0o077:
        os.chmod(target_dir, 0o700)
        _sync(home)
    target_dir = _root(target_dir)
    target = target_dir / "catalog.sqlite3"
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            raise ValueError("Legacy Catalog is symlinked")
        target_info = target.stat(follow_symlinks=False)
        if (not stat.S_ISREG(target_info.st_mode)
                or target_info.st_uid != os.getuid() or target_info.st_nlink != 1):
            raise ValueError("Legacy Catalog is invalid")
        if target_info.st_mode & 0o077:
            os.chmod(target, 0o600)
            _sync(target_dir)
    desired = _sha(_read(catalog, _MAX_TOTAL))
    if receipt.exists():
        value, raw = _json(receipt)
        if value.get("catalog_sha256_after") != desired or _sha(_read(target, _MAX_TOTAL)) != desired:
            raise ValueError("Activated legacy Catalog changed")
        return value, raw, True
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(target) + suffix).exists():
            raise ValueError("Legacy Catalog is not quiescent")
    backup = stage / "legacy-catalog-before.sqlite3"
    before = _sha(_read(target, _MAX_TOTAL)) if target.exists() else None
    if backup.exists() or backup.is_symlink():
        backup_sha = None if backup.is_symlink() else _sha(_read(backup, _MAX_TOTAL))
        # If the target already has the desired bytes, the prior process may
        # have crashed after the atomic replace but before publishing the
        # activation receipt.  In that one window the backup correctly differs
        # from the now-active target and remains the preserved pre-state.
        if (backup.is_symlink() or before is None
                or (before != desired and backup_sha != before)):
            raise ValueError("Legacy Catalog backup changed")
    elif target.exists():
        shutil.copyfile(target, backup, follow_symlinks=False)
        os.chmod(backup, 0o600)
        with open(backup, "rb") as stream:
            os.fsync(stream.fileno())
        _sync(stage)
    part = target_dir / (".catalog.sqlite3.rollback-" + desired[:16] + ".part")
    if part.is_symlink():
        raise ValueError("Legacy Catalog activation part is symlinked")
    if target.exists() and _sha(_read(target, _MAX_TOTAL)) == desired:
        if part.exists():
            if not part.is_file():
                raise ValueError("Legacy Catalog activation part is invalid")
            part.unlink()
    else:
        if part.exists():
            if not part.is_file():
                raise ValueError("Legacy Catalog activation part is invalid")
            part.unlink()
        shutil.copyfile(catalog, part)
        os.chmod(part, 0o600)
        with open(part, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(part, target)
        _sync(target_dir)
    if _sha(_read(target, _MAX_TOTAL)) != desired:
        raise ValueError("Legacy Catalog activation changed bytes")
    value = {"format": "puddingclaw-legacy-catalog-activation/v1", "state": "installed_frozen",
             "catalog_sha256_before": before, "catalog_sha256_after": desired,
             "activation_allowed": False, "rollback_completed": False}
    raw = _write(receipt, value)
    return value, raw, False


def _health(home, installed_candidate):
    catalog = home / "db/catalog.sqlite3"
    connection = sqlite3.connect("file:" + str(catalog) + "?mode=ro", uri=True)
    try:
        quick = connection.execute("PRAGMA quick_check").fetchall()
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        count = connection.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0]
        paths = [row[0] for row in connection.execute(
            "SELECT storage_path FROM knowledge_documents WHERE storage_path IS NOT NULL AND storage_path != ''")]
    finally:
        connection.close()
    if quick != [("ok",)] or foreign:
        raise ValueError("Activated legacy Catalog failed health checks")
    bodies = installed_candidate / "bodies"
    for value in paths:
        path = Path(value)
        if not path.is_absolute() or not path.is_relative_to(bodies):
            raise ValueError("Activated legacy document body is not rebound to the installed candidate")
        if not path.is_file():
            raise ValueError("Activated legacy document body is missing")
    return {"quick_check": "ok", "foreign_key_violations": 0,
            "knowledge_documents": count, "bound_paths_checked": len(paths)}


def _retire_active_pointers(harness_home, knowledge_home, stage, *, rollback_mode,
                            rollback_checkpoint, rollback_facts,
                            source_home_identity, source_freeze_sha256):
    receipt_path = stage / POINTERS_NAME
    if rollback_mode == "pre_cutover_abort":
        for side, home in (("harness", harness_home), ("knowledge", knowledge_home)):
            pointer = home / "active-installation.json"
            archive = stage / ("retired-" + side + "-active-installation.json")
            if (pointer.exists() or pointer.is_symlink()
                    or archive.exists() or archive.is_symlink()):
                raise ValueError("Pre-CUTOVER rollback cannot retire an active installation pointer")
        receipt = {"format": "puddingharness-active-pointer-retirement/v1",
                   "state": "not_applicable_pre_cutover", "rollback_mode": rollback_mode,
                   "pointers": [], "active_pointers_retired": False,
                   "activation_allowed": False, "rollback_completed": False}
        if receipt_path.exists() or receipt_path.is_symlink():
            current, raw = _json(receipt_path)
            if current != receipt:
                raise ValueError("Active pointer disposition receipt changed")
        else:
            raw = _write(receipt_path, receipt)
        return receipt, raw

    # Validate both controls before mutating either Home.  A disagreement is
    # evidence of an already split control plane and must leave both pointers
    # untouched for investigation.
    controls = []
    for side, home in (("harness", harness_home), ("knowledge", knowledge_home)):
        pointer = home / "active-installation.json"
        archive = stage / ("retired-" + side + "-active-installation.json")
        if pointer.exists() or pointer.is_symlink():
            if pointer.is_symlink():
                raise ValueError("Active installation pointer is symlinked")
            value, raw = _json(pointer)
        else:
            value, raw = _json(archive)
        if value.get("format") != "puddingharness-active-installation/v1":
            raise ValueError("Active installation pointer format is invalid")
        controls.append((side, home, pointer, archive, value, raw))
    if controls[0][5] != controls[1][5]:
        raise ValueError("Product active installation pointers disagree")
    pointer = controls[0][4]
    expected_keys = {"format", "operation_id", "cutover_manifest_sha256",
                     "prepared_manifest_sha256", "source_home_identity",
                     "source_freeze_receipt_sha256", "active_installation_revision",
                     "harness_assigned_event_sha256", "knowledge_assigned_event_sha256",
                     "active_writers"}
    harness_cutover = rollback_facts["harness_cutover"]
    knowledge_cutover = rollback_facts["knowledge_cutover"]
    expected_writers = {"session_harness": "puddingharness",
                        "knowledge_catalog": "puddingknowledge",
                        "connector_jobs": "puddingknowledge"}
    if (set(pointer) != expected_keys
            or pointer["operation_id"] != rollback_facts["source_operation_id"]
            or pointer["cutover_manifest_sha256"]
                != rollback_checkpoint["cutover_manifest_sha256"]
            or pointer["prepared_manifest_sha256"]
                != harness_cutover["migration_manifest_sha256"]
            or pointer["active_installation_revision"]
                != harness_cutover["active_installation_revision"]
            or pointer["harness_assigned_event_sha256"] != harness_cutover["sha256"]
            or pointer["knowledge_assigned_event_sha256"] != knowledge_cutover["sha256"]
            or pointer["source_home_identity"] != source_home_identity
            or pointer["source_freeze_receipt_sha256"] != "sha256:" + source_freeze_sha256
            or pointer["active_writers"] != expected_writers):
        raise ValueError("Active installation pointer does not bind the rollback authority")

    retired = []
    for side, home, pointer, archive, value, raw in controls:
        if archive.exists():
            if _read(archive) != raw:
                raise ValueError("Retired active installation pointer changed")
        else:
            _write(archive, value)
        if pointer.exists():
            pointer.unlink()
            _sync(home)
        retired.append({"side": side, "sha256": _sha(raw)})
    receipt = {"format": "puddingharness-active-pointer-retirement/v1",
               "state": "retired", "rollback_mode": rollback_mode, "pointers": retired,
               "active_pointers_retired": True, "activation_allowed": False,
               "rollback_completed": False}
    if receipt_path.exists():
        current, raw = _json(receipt_path)
        if current != receipt:
            raise ValueError("Active pointer retirement receipt changed")
    else:
        raw = _write(receipt_path, receipt)
    return receipt, raw


def _checkpoint(stage, base, state, **facts):
    value = {**base, "state": state, "rollback_completed": state == "rollback_completed", **facts}
    _write(stage / CHECKPOINT_NAME, value)
    return value


def activate_rollback(source_home, harness_home, knowledge_home, candidate, knowledge_python, checkpoint_dir,
                      rollback_checkpoint, rollback_evidence, credential_baseline, *, source_operation_id,
                      rollback_operation_id, _after_checkpoint=None, _after_thaw=None,
                      _path_rebind=None, _health_probe=None):
    for operation in (source_operation_id, rollback_operation_id):
        if not isinstance(operation, str) or _OPERATION.fullmatch(operation) is None:
            raise ValueError("Invalid rollback activation operation")
    home, harness, knowledge, source, stage = map(
        _root, (source_home, harness_home, knowledge_home, candidate, checkpoint_dir))
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for index, a in enumerate((home, harness, knowledge, source, stage))
           for b in (home, harness, knowledge, source, stage)[index + 1:]):
        raise ValueError("Rollback activation roots must be disjoint")
    python = Path(knowledge_python).expanduser()
    if (not python.is_absolute() or ".." in python.parts
            or any(parent.is_symlink() for parent in python.parents)):
        raise ValueError("Knowledge interpreter is invalid")
    # Keep the venv launcher path: resolving its final symlink would execute
    # the base Python without the independently installed Knowledge package.
    python = Path(os.path.abspath(python))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Knowledge interpreter is invalid")
    rollback, rollback_raw, rollback_facts = _rollback_checkpoint(
        Path(rollback_checkpoint), rollback_operation_id)
    rollback_mode = rollback_facts["mode"]
    if (rollback_mode == "window_rollback"
            and rollback_facts["source_operation_id"] != source_operation_id):
        raise ValueError("Source fence operation does not match the CUTOVER authority")
    baseline_value, baseline_raw = _json(Path(credential_baseline))
    if baseline_value.get("operation_id") != source_operation_id:
        raise ValueError("Credential baseline operation does not match the source fence")
    candidate_manifest = _read(source / "manifest.json")
    candidate_manifest_value = json.loads(candidate_manifest, object_pairs_hook=_unique)
    if (not isinstance(candidate_manifest_value, dict)
            or _encoded(candidate_manifest_value) != candidate_manifest):
        raise ValueError("Rollback candidate manifest must be canonical")
    _evidence, evidence_raw = _verify_rollback_evidence(
        Path(rollback_evidence), rollback, source, candidate_manifest_value,
        rollback_operation_id)
    plan = {"format": FORMAT, "rollback_mode": rollback_mode,
            "source_operation_id": source_operation_id,
            "rollback_operation_id": rollback_operation_id,
            "source_home": str(home), "harness_home": str(harness),
            "knowledge_home": str(knowledge), "candidate": str(source),
            "candidate_manifest_sha256": _sha(candidate_manifest),
            "rollback_checkpoint_sha256": _sha(rollback_raw),
            "rollback_evidence_sha256": _sha(evidence_raw),
            "credential_baseline_sha256": _sha(baseline_raw),
            "knowledge_python": str(python)}
    lock = stage / LOCK_NAME
    lock_fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan_path = stage / PLAN_NAME
        if plan_path.exists():
            current, _ = _json(plan_path)
            if current != plan:
                raise ValueError("Rollback activation plan changed")
        else:
            _write(plan_path, plan)
        activation_path = stage / ACTIVATION_NAME
        installed_root = home / ".rollback-activation-v1"
        installed_root.mkdir(mode=0o700, exist_ok=True)
        installed_root = _root(installed_root)
        installed = installed_root / (rollback_operation_id.replace(":", "_") + "-candidate")
        archive = installed_root / (rollback_operation_id.replace(":", "_") + "-source-freeze.json")
        marker = home / source_writer_fence.MARKER_NAME
        marker_part = home / source_writer_fence.PART_NAME
        # A completed activation is immutable evidence about the switch point;
        # the live legacy Catalog may legitimately change afterwards.  Verify
        # only the retired controls and receipt chain, without trying to take
        # the installation gate away from an already-running legacy writer.
        if activation_path.exists():
            receipt, raw = _json(activation_path)
            if (receipt.get("format") != ACTIVATION_FORMAT
                    or receipt.get("rollback_mode") != rollback_mode
                    or receipt.get("rollback_completed") is not True
                    or marker.exists() or marker.is_symlink()
                    or marker_part.exists() or marker_part.is_symlink()
                    or not archive.exists()
                    or (harness / "active-installation.json").exists()
                    or (harness / "active-installation.json").is_symlink()
                    or (knowledge / "active-installation.json").exists()
                    or (knowledge / "active-installation.json").is_symlink()
                    or _sha(_read(archive)) != receipt.get("source_freeze_sha256")):
                raise ValueError("Completed rollback activation changed")
            source_freeze_sha = receipt.get("source_freeze_sha256")
            if not isinstance(source_freeze_sha, str) or _HEX64.fullmatch(source_freeze_sha) is None:
                raise ValueError("Completed rollback activation has invalid source freeze evidence")
            pointer_receipt, pointer_raw = _retire_active_pointers(
                harness, knowledge, stage, rollback_mode=rollback_mode,
                rollback_checkpoint=rollback, rollback_facts=rollback_facts,
                source_home_identity=hashlib.sha256(str(home).encode()).hexdigest(),
                source_freeze_sha256=source_freeze_sha)
            expected_retired = rollback_mode == "window_rollback"
            if (pointer_receipt.get("rollback_mode") != rollback_mode
                    or pointer_receipt.get("active_pointers_retired") is not expected_retired
                    or _sha(pointer_raw) != receipt.get("active_pointer_retirement_sha256")):
                raise ValueError("Completed active pointer retirement changed")
            source_writer_fence._verify_capability(home)
            return {**receipt, "activation_receipt_sha256": _sha(raw), "idempotent": True}
        gate_fd = _gate(home)
        try:
            source_writer_fence._verify_capability(home, os.fstat(gate_fd))
            # Recover only the temporary hardlink used to keep startup denied
            # while the marker name is archived for the health probe.
            if marker_part.exists() or marker_part.is_symlink():
                if marker_part.is_symlink():
                    raise ValueError("Legacy freeze publication part is symlinked")
                if not marker.exists() and archive.exists():
                    os.rename(archive, marker)
                    _sync(installed_root); _sync(home)
                if marker.exists():
                    a, b = marker.stat(follow_symlinks=False), marker_part.stat(follow_symlinks=False)
                    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                        raise ValueError("Legacy freeze recovery links disagree")
                    marker_part.unlink(); _sync(home)

            thawed_without_receipt = not marker.exists() and archive.exists() and (stage / READY_NAME).exists()
            if not thawed_without_receipt:
                source_fence = source_writer_fence.verify_source_fence(home, source_operation_id)
                source_freeze_sha = source_fence["source_freeze_receipt_sha256"]
            else:
                ready_value, _ = _json(stage / READY_NAME)
                source_freeze_sha = ready_value.get("source_freeze_sha256")
                if (not isinstance(source_freeze_sha, str)
                        or _HEX64.fullmatch(source_freeze_sha) is None
                        or _sha(_read(archive)) != source_freeze_sha):
                    raise ValueError("Retired source freeze marker changed")

            base = {"format": FORMAT, "rollback_mode": rollback_mode,
                    "source_operation_id": source_operation_id,
                    "rollback_operation_id": rollback_operation_id,
                    "plan_sha256": _sha(_encoded(plan)),
                    "rollback_checkpoint_sha256": _sha(rollback_raw),
                    "activation_allowed": False}
            rebind_path = stage / REBINDS_NAME
            source_inventory, _copied = _copy_candidate(
                source, installed, catalog_may_be_rebound=rebind_path.exists())
            _checkpoint(stage, base, "candidate_installed",
                        candidate_inventory_sha256=_sha(_encoded(source_inventory)))
            if _after_checkpoint: _after_checkpoint("candidate_installed")

            # The rebind command is a one-way mutation of the candidate
            # Catalog.  Once its receipt exists, replay must validate and
            # adopt that exact result instead of invoking the command again.
            # A later index rebuild legitimately changes the Catalog bytes,
            # so in that state the index receipt owns the current digest while
            # the rebind receipt still binds the manifest and installed path.
            index_path = stage / INDEX_NAME
            if not rebind_path.exists():
                runner = _path_rebind or _run_rebind
                runner(python, installed, rebind_path)
            _rebind, rebind_raw = _validate_rebind(
                rebind_path, installed,
                catalog_may_have_rebuilt_indexes=index_path.exists())
            _verify_candidate_inventory(installed, source_inventory, catalog_may_be_rebound=True)
            _checkpoint(stage, base, "paths_rebound", rebind_sha256=_sha(rebind_raw))
            if _after_checkpoint: _after_checkpoint("paths_rebound")

            _indexes, indexes_raw, _ = _rebuild_indexes(installed / "catalog.sqlite3", index_path)
            _checkpoint(stage, base, "indexes_rebuilt", rebind_sha256=_sha(rebind_raw),
                        indexes_sha256=_sha(indexes_raw))
            if _after_checkpoint: _after_checkpoint("indexes_rebuilt")

            credentials = _verify_credentials(home, Path(credential_baseline))
            credentials_raw = _encoded(credentials)
            _checkpoint(stage, base, "credentials_verified", rebind_sha256=_sha(rebind_raw),
                        indexes_sha256=_sha(indexes_raw),
                        credentials_sha256=_sha(credentials_raw))
            if _after_checkpoint: _after_checkpoint("credentials_verified")

            catalog, catalog_raw, _ = _activate_catalog(home, installed / "catalog.sqlite3", stage)
            _checkpoint(stage, base, "catalog_activated", rebind_sha256=_sha(rebind_raw),
                        indexes_sha256=_sha(indexes_raw), credentials_sha256=_sha(credentials_raw),
                        catalog_activation_sha256=_sha(catalog_raw))
            if _after_checkpoint: _after_checkpoint("catalog_activated")

            health = (_health_probe or _health)(home, installed)
            if not isinstance(health, dict):
                raise TypeError("Legacy health probe returned invalid evidence")
            ready = {"format": READY_FORMAT, "state": "legacy_health_verified",
                     "source_operation_id": source_operation_id,
                     "rollback_operation_id": rollback_operation_id,
                     "catalog_sha256": catalog["catalog_sha256_after"],
                     "source_freeze_sha256": source_freeze_sha,
                     "rebind_sha256": _sha(rebind_raw), "indexes_sha256": _sha(indexes_raw),
                     "credentials_sha256": _sha(credentials_raw), "health": health,
                     "activation_allowed": False, "rollback_completed": False}
            ready_raw = _write(stage / READY_NAME, ready)
            _checkpoint(stage, base, "legacy_health_verified", ready_sha256=_sha(ready_raw))
            if _after_checkpoint: _after_checkpoint("legacy_health_verified")

            _pointers, pointers_raw = _retire_active_pointers(
                harness, knowledge, stage, rollback_mode=rollback_mode,
                rollback_checkpoint=rollback, rollback_facts=rollback_facts,
                source_home_identity=hashlib.sha256(str(home).encode()).hexdigest(),
                source_freeze_sha256=source_freeze_sha)
            _checkpoint(stage, base, "active_pointers_retired",
                        ready_sha256=_sha(ready_raw),
                        active_pointer_retirement_sha256=_sha(pointers_raw))
            if _after_checkpoint: _after_checkpoint("active_pointers_retired")

            if not thawed_without_receipt:
                # The hardlinked part keeps every cooperative legacy startup
                # denied if the process dies after the marker name is archived.
                os.link(marker, marker_part)
                _sync(home)
                os.rename(marker, archive)
                _sync(home); _sync(installed_root)
                try:
                    health_after_thaw = (_health_probe or _health)(home, installed)
                    if health_after_thaw != health:
                        raise ValueError("Legacy health changed during audited thaw")
                except BaseException:
                    os.rename(archive, marker)
                    marker_part.unlink()
                    _sync(installed_root); _sync(home)
                    raise
                marker_part.unlink()
                _sync(home)
                if _after_thaw:
                    _after_thaw()

            receipt = {"format": ACTIVATION_FORMAT, "state": "rollback_completed",
                       "rollback_mode": rollback_mode,
                       "source_operation_id": source_operation_id,
                       "rollback_operation_id": rollback_operation_id,
                       "rollback_checkpoint_sha256": _sha(rollback_raw),
                       "catalog_sha256": catalog["catalog_sha256_after"],
                       "source_freeze_sha256": source_freeze_sha,
                       "rebind_sha256": _sha(rebind_raw), "indexes_sha256": _sha(indexes_raw),
                       "credentials_sha256": _sha(credentials_raw),
                       "active_pointer_retirement_sha256": _sha(pointers_raw), "health": health,
                       "legacy_writer_thawed": True, "credential_continuity_verified": True,
                       "indexes_rebuilt": True, "installation_path_rebound": True,
                       "activation_allowed": True, "rollback_completed": True,
                       "production_activated": False}
            activation_raw = _write(activation_path, receipt)
            _checkpoint(stage, base, "rollback_completed",
                        activation_receipt_sha256=_sha(activation_raw))
            if _after_checkpoint: _after_checkpoint("rollback_completed")
            return {**receipt, "activation_receipt_sha256": _sha(activation_raw),
                    "idempotent": False}
        finally:
            os.close(gate_fd)
    finally:
        os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    baseline = commands.add_parser("capture-credentials")
    for name in ("source-home", "output", "operation-id"):
        baseline.add_argument("--" + name, required=True)
    activate = commands.add_parser("activate")
    for name in ("source-home", "candidate", "knowledge-python", "checkpoint-dir",
                 "harness-home", "knowledge-home", "rollback-checkpoint", "rollback-evidence",
                 "credential-baseline", "source-operation-id",
                 "rollback-operation-id"):
        activate.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "capture-credentials":
            result = capture_credential_baseline(args.source_home, args.output,
                                                 operation_id=args.operation_id)
        else:
            result = activate_rollback(
                args.source_home, args.harness_home, args.knowledge_home, args.candidate,
                args.knowledge_python, args.checkpoint_dir,
                args.rollback_checkpoint, args.rollback_evidence, args.credential_baseline,
                source_operation_id=args.source_operation_id,
                rollback_operation_id=args.rollback_operation_id,
            )
    except Exception:  # noqa: BLE001 - the CLI is a fail-closed protocol boundary
        print(json.dumps({"format": FORMAT, "status": "error",
                          "error_code": "rollback_activation_rejected",
                          "activation_allowed": False, "rollback_completed": False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
