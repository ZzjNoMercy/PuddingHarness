import hashlib

import pytest

from harness.session_reverse_plan import plan_session_reverse


def source_inventory(files, directories):
    return {
        "files": files,
        "directories": directories,
        "total_bytes": sum(f["size"] for f in files.values()),
    }


def sf(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def cf(data):
    return {"digest": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data)}


def test_reverse_replaces_and_preserves_foreign_data():
    src = source_inventory(
        {
            "sessions/old.json": sf(b"old"),
            "sessions/stale.json": sf(b"stale"),
            "data/attachments/a.bin": sf(b"attachment"),
            "knowledge/keep.txt": sf(b"keep"),
        },
        ["sessions", "data", "data/attachments", "knowledge"],
    )
    baseline = {
        "sessions/old.json": cf(b"old"),
        "sessions/stale.json": cf(b"stale"),
        "data/attachments/a.bin": cf(b"attachment"),
    }
    result = plan_session_reverse(src, baseline, {"sessions/new.json": cf(b"new")})
    assert result["changes"] == {"insert": 1, "update": 0, "delete": 3}
    assert set(result["inventory"]["files"]) == {"sessions/new.json", "knowledge/keep.txt"}
    assert "sessions" in result["inventory"]["directories"]
    assert "data/attachments" not in result["inventory"]["directories"]
    assert "knowledge" in result["inventory"]["directories"]


def test_update_and_empty_lock_removal():
    src = source_inventory(
        {"sessions/a": sf(b"old"), "sessions/.lock": sf(b"")},
        ["sessions"],
    )
    result = plan_session_reverse(src, {"sessions/a": cf(b"old")}, {"sessions/a": cf(b"new")})
    assert result["changes"] == {"insert": 0, "update": 1, "delete": 1}


def test_baseline_path_and_commitment_validation():
    src = source_inventory({"sessions/a": sf(b"a")}, ["sessions"])
    with pytest.raises(ValueError):
        plan_session_reverse(src, {}, {})
    with pytest.raises(ValueError):
        plan_session_reverse(src, {"sessions-evil/a": cf(b"a")}, {})
    with pytest.raises(ValueError):
        plan_session_reverse(src, {"sessions/a": {"digest": "sha256:bad", "size": 1}}, {})


@pytest.mark.parametrize("path", ["sessions/a.migration-part", "sessions-evil/a", "../sessions/a", "/sessions/a"])
def test_reserved_or_lookalike_after_paths_rejected(path):
    src = source_inventory({}, [])
    with pytest.raises(ValueError):
        plan_session_reverse(src, {}, {path: cf(b"x")})


def test_directory_file_collision_and_nonempty_lock_rejected():
    src = source_inventory({"sessions/a": sf(b"a")}, ["sessions"])
    # The source validator also rejects a file used as an ancestor directory.
    conflicting = source_inventory({"data": sf(b"file")}, ["data/attachments"])
    with pytest.raises(ValueError):
        plan_session_reverse(conflicting, {}, {"data/attachments/a": cf(b"b")})
    bad = source_inventory({"sessions/.lock": sf(b"x")}, ["sessions"])
    with pytest.raises(ValueError):
        plan_session_reverse(bad, {}, {})
