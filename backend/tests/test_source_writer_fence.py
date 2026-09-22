import hashlib
import json
import os
from pathlib import Path

import pytest

from harness.source_writer_fence import (
    CAPABILITY_FORMAT, CAPABILITY_NAME, LOCK_NAME, MARKER_NAME, PART_NAME,
    publish_source_fence, verify_source_fence,
)


def _capability(home: Path) -> bytes:
    lock = home / LOCK_NAME
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    root_info, lock_info = home.stat(), lock.stat()
    value = {
        "format": CAPABILITY_FORMAT,
        "home_identity": hashlib.sha256(str(home).encode()).hexdigest(),
        "directory_identity": {"device": root_info.st_dev, "inode": root_info.st_ino},
        "lock_identity": {"device": lock_info.st_dev, "inode": lock_info.st_ino},
        "participating_process_admission": True,
        "persistent_source_freeze": True,
    }
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (home / CAPABILITY_NAME).write_bytes(raw)
    (home / CAPABILITY_NAME).chmod(0o600)
    return raw


def _marker(home: Path, operation: str = "cutover-1") -> bytes:
    capability = _capability(home)
    info = home.stat()
    value = {
        "format": "puddingclaw-source-freeze/v1",
        "operation_id": operation,
        "state": "source_frozen",
        "home_identity": hashlib.sha256(str(home).encode()).hexdigest(),
        "directory_identity": {"device": info.st_dev, "inode": info.st_ino},
        "admission_capability_sha256": hashlib.sha256(capability).hexdigest(),
        "installation_cutover_performed": False,
        "external_writers_fenced": False,
    }
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (home / MARKER_NAME).write_bytes(raw)
    (home / MARKER_NAME).chmod(0o600)
    return raw


def test_exact_live_source_freeze_is_admitted(tmp_path):
    home = tmp_path / "source"
    home.mkdir(mode=0o700)
    raw = _marker(home)
    receipt = verify_source_fence(home, "cutover-1")
    assert receipt["legacy_writer_fenced"] is True
    assert receipt["source_freeze_receipt_sha256"] == hashlib.sha256(raw).hexdigest()


def test_publish_source_fence_is_exact_and_idempotent(tmp_path):
    home = tmp_path / "source"
    home.mkdir(mode=0o700)
    _capability(home)
    first = publish_source_fence(home, "cutover-1")
    assert publish_source_fence(home, "cutover-1") == first
    assert first == verify_source_fence(home, "cutover-1")
    assert not (home / PART_NAME).exists()
    assert (home / MARKER_NAME).stat().st_mode & 0o077 == 0
    assert (home / LOCK_NAME).stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        publish_source_fence(home, "cutover-2")


def test_publish_source_fence_refuses_an_active_writer(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    home = tmp_path / "source"
    home.mkdir(mode=0o700)
    _capability(home)
    fd = os.open(home / LOCK_NAME, os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="participating writers"):
            publish_source_fence(home, "cutover-1")
    finally:
        os.close(fd)


def test_marker_without_final_source_capability_refuses(tmp_path):
    home = tmp_path / "source"
    home.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="capability"):
        publish_source_fence(home, "cutover-1")


@pytest.mark.parametrize("change", ["missing", "part", "operation", "identity", "public", "symlink"])
def test_missing_incomplete_or_drifted_source_freeze_refuses(tmp_path, change):
    home = tmp_path / "source"
    home.mkdir(mode=0o700)
    if change != "missing":
        raw = _marker(home, "other" if change == "operation" else "cutover-1")
        marker = home / MARKER_NAME
        if change == "part":
            (home / PART_NAME).write_bytes(raw)
            (home / PART_NAME).chmod(0o600)
        elif change == "identity":
            value = json.loads(raw)
            value["directory_identity"]["inode"] += 1
            marker.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        elif change == "public":
            marker.chmod(0o644)
        elif change == "symlink":
            marker.unlink()
            marker.symlink_to(home / "absent")
    with pytest.raises(ValueError):
        verify_source_fence(home, "cutover-1")
