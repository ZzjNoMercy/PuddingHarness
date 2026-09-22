import fcntl
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from harness import rollback_activation as activation
from harness.source_writer_fence import (
    CAPABILITY_FORMAT,
    CAPABILITY_NAME,
    LOCK_NAME,
    MARKER_NAME,
    PART_NAME,
    publish_source_fence,
)


def _private_file(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(0o600)


def _canonical(path, value):
    _private_file(path, activation._encoded(value))


def _catalog(path, title, storage):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE knowledge_documents (id TEXT PRIMARY KEY,title TEXT NOT NULL,storage_path TEXT);"
            "CREATE INDEX ix_knowledge_documents_title ON knowledge_documents(title);"
        )
        db.execute("INSERT INTO knowledge_documents VALUES ('doc-1',?,?)", (title, storage))
    path.chmod(0o600)


PREPARED = "1" * 64
CUTOVER = "2" * 64
ROLLED_BACK = "3" * 64


def _event(revision, previous, operation, state, *, side, writer, freeze, **extra):
    value = {"revision": revision, "previous": previous, "operation_id": operation,
             "state": state, "freeze_receipt_sha256": freeze, **extra}
    value["writer" if side == "harness" else "writers"] = writer
    value["sha256"] = hashlib.sha256(activation._encoded(value)).hexdigest()
    return value


def _journal(side, *, window, evidence_sha256):
    existing_writer = ("session_harness" if side == "harness" else
                       {"knowledge_catalog": "puddingclaw", "connector_jobs": "puddingclaw"})
    new_writer = ("puddingharness" if side == "harness" else
                  {"knowledge_catalog": "puddingknowledge", "connector_jobs": "puddingknowledge"})
    legacy_writer = ("puddingclaw" if side == "harness" else
                     {"knowledge_catalog": "puddingclaw", "connector_jobs": "puddingclaw"})
    first = _event(0, None, "enroll-1", "existing_writer", side=side,
                   writer=existing_writer, freeze=None)
    if window:
        suspended = _event(1, first["sha256"], "cutover-1", "suspended", side=side,
                           writer=existing_writer, freeze="a" * 64)
        assigned = _event(
            2, suspended["sha256"], "cutover-1", "assigned", side=side,
            writer=new_writer, freeze="a" * 64,
            active_installation_revision="sha256:" + PREPARED,
            migration_manifest_sha256=PREPARED, rollback_evidence_sha256=None)
        resuspended = _event(3, assigned["sha256"], "window-1", "suspended", side=side,
                             writer=new_writer, freeze="b" * 64)
        reassigned = _event(
            4, resuspended["sha256"], "window-1", "assigned", side=side,
            writer=legacy_writer, freeze="b" * 64,
            active_installation_revision="sha256:" + ROLLED_BACK,
            migration_manifest_sha256=ROLLED_BACK,
            rollback_evidence_sha256=evidence_sha256)
        events = [first, suspended, assigned, resuspended, reassigned]
    else:
        suspended = _event(1, first["sha256"], "window-1", "suspended", side=side,
                           writer=existing_writer, freeze="b" * 64)
        reassigned = _event(
            2, suspended["sha256"], "window-1", "assigned", side=side,
            writer=legacy_writer, freeze="b" * 64,
            active_installation_revision="sha256:" + ROLLED_BACK,
            migration_manifest_sha256=ROLLED_BACK,
            rollback_evidence_sha256=evidence_sha256)
        events = [first, suspended, reassigned]
    return {"format": "test-writer-authority/v1", "binding_sha256": "9" * 64,
            "events": events}


def _pointer(path, source, source_freeze_sha256, journals):
    _canonical(path, {
        "format": "puddingharness-active-installation/v1",
        "operation_id": "cutover-1",
        "cutover_manifest_sha256": CUTOVER,
        "prepared_manifest_sha256": PREPARED,
        "source_home_identity": hashlib.sha256(str(source).encode()).hexdigest(),
        "source_freeze_receipt_sha256": "sha256:" + source_freeze_sha256,
        "active_installation_revision": "sha256:" + PREPARED,
        "harness_assigned_event_sha256": journals["harness"]["events"][2]["sha256"],
        "knowledge_assigned_event_sha256": journals["knowledge"]["events"][2]["sha256"],
        "active_writers": {"session_harness": "puddingharness",
                           "knowledge_catalog": "puddingknowledge",
                           "connector_jobs": "puddingknowledge"},
    })


def _capability(home):
    lock = home / LOCK_NAME
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    root_info, lock_info = home.stat(), lock.stat()
    _canonical(home / CAPABILITY_NAME, {
        "format": CAPABILITY_FORMAT,
        "home_identity": hashlib.sha256(str(home).encode()).hexdigest(),
        "directory_identity": {"device": root_info.st_dev, "inode": root_info.st_ino},
        "lock_identity": {"device": lock_info.st_dev, "inode": lock_info.st_ino},
        "participating_process_admission": True,
        "persistent_source_freeze": True,
    })


def _rollback_checkpoint(path, operation="window-1", *, window=True):
    evidence_sha256 = hashlib.sha256(path.with_name("rollback-evidence.json").read_bytes()).hexdigest()
    journals = {side: _journal(side, window=window, evidence_sha256=evidence_sha256)
                for side in ("harness", "knowledge")}
    value = {"format": ("puddingharness-window-rollback-orchestrator/v1" if window
                         else "puddingharness-rollback-orchestrator/v1"),
             "state": "both_reassigned", "operation_id": operation,
             "plan_sha256": "8" * 64,
             ("cutover_manifest_sha256" if window else "prepared_manifest_sha256"):
                 (CUTOVER if window else PREPARED),
             "rollback_evidence_sha256": evidence_sha256,
             "rolled_back_manifest_sha256": ROLLED_BACK,
             "activation_allowed": False, "installation_cutover_performed": False,
             "rollback_completed": False, "production_activated": False,
             "journals": journals}
    _canonical(path, value)
    return journals


def _rollback_evidence(path, candidate):
    manifest_raw = (candidate / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    value = {
        "format": activation.EVIDENCE_FORMAT,
        "state": "verified_rollback_evidence",
        "operation_id": "window-1",
        "source_revision": "source-1",
        "artifacts": [
            {"role": "frozen_export", "state": "verified_frozen_export", "sha256": "5" * 64},
            {"role": "other_catalog_disposition", "state": "verified_other_catalog_disposition",
             "sha256": "6" * 64},
            {"role": "document_reverse", "state": "verified_inactive_documents",
             "sha256": hashlib.sha256(manifest_raw).hexdigest()},
            {"role": "wiki_reverse", "state": "verified_absent_wiki", "sha256": "7" * 64},
        ],
        "linkages": {"document_candidate_catalog_sha256": hashlib.sha256(
            (candidate / "catalog.sqlite3").read_bytes()).hexdigest()},
        "artifact_digests": {"document_bodies_inventory_sha256": hashlib.sha256(
            activation._encoded(manifest["plan"]["output_inventory"])).hexdigest()},
        "rollback_completed": False,
        "activation_allowed": False,
        "installation_cutover_performed": False,
        "indexes_rebuilt": False,
    }
    _canonical(path, value)


def _fake_rebind(_python, candidate, receipt):
    body = candidate / "bodies/doc.md"
    with sqlite3.connect(candidate / "catalog.sqlite3") as db:
        db.execute("UPDATE knowledge_documents SET storage_path=?", (str(body),))
    digest = hashlib.sha256((candidate / "catalog.sqlite3").read_bytes()).hexdigest()
    value = {"format": "puddingknowledge-installation-path-rebind/v1",
             "state": "verified_installation_path_rebound", "candidate": str(candidate),
             "manifest_sha256": hashlib.sha256((candidate / "manifest.json").read_bytes()).hexdigest(),
             "old_prefix": "/old/bodies", "new_prefix": str(candidate / "bodies"),
             "rebound_fields": {"knowledge_documents.storage_path": 1},
             "documents": 1, "catalog_sha256_before": "0" * 64,
             "catalog_sha256_after": digest, "bodies_verified": 1,
             "installation_path_rebound": True, "indexes_rebuilt": False,
             "rollback_completed": False, "activation_allowed": False,
             "installation_cutover_performed": False}
    activation._write(receipt, value)
    return value


@pytest.fixture
def roots(tmp_path):
    root = tmp_path.resolve()
    source = root / "legacy-home"; source.mkdir(mode=0o700)
    _capability(source)
    _catalog(source / "db/catalog.sqlite3", "legacy title", "/old/doc.md")
    _private_file(source / "users/local/credentials/registry.enc", b"encrypted-credential")
    harness = root / "harness-home"; harness.mkdir(mode=0o700)
    knowledge = root / "knowledge-home"; knowledge.mkdir(mode=0o700)
    candidate = root / "candidate"; candidate.mkdir(mode=0o700)
    (candidate / "bodies").mkdir(mode=0o700)
    _private_file(candidate / "bodies/doc.md", b"# restored\n")
    _private_file(candidate / ".writer-authority.lock", b"")
    _catalog(candidate / "catalog.sqlite3", "window title", "/old/bodies/doc.md")
    _canonical(candidate / "manifest.json", {
        "format": activation.DOCUMENT_REVERSE_FORMAT,
        "state": "verified_inactive_documents",
        "plan": {"output_inventory": activation._inventory(candidate / "bodies")},
    })
    checkpoint = root / "activation-checkpoint"; checkpoint.mkdir(mode=0o700)
    rollback = root / "rollback-checkpoint.json"
    baseline = root / "credential-baseline.json"
    activation.capture_credential_baseline(source, baseline, operation_id="cutover-1")
    source_fence = publish_source_fence(source, "cutover-1")
    _rollback_evidence(rollback.with_name("rollback-evidence.json"), candidate)
    journals = _rollback_checkpoint(rollback)
    _pointer(harness / "active-installation.json", source,
             source_fence["source_freeze_receipt_sha256"], journals)
    _pointer(knowledge / "active-installation.json", source,
             source_fence["source_freeze_receipt_sha256"], journals)
    return source, harness, knowledge, candidate, checkpoint, rollback, baseline


def run(roots, **kwargs):
    source, harness, knowledge, candidate, checkpoint, rollback, baseline = roots
    path_rebind = kwargs.pop("_path_rebind", _fake_rebind)
    return activation.activate_rollback(
        source, harness, knowledge, candidate, sys.executable, checkpoint, rollback,
        rollback.with_name("rollback-evidence.json"), baseline,
        source_operation_id="cutover-1", rollback_operation_id="window-1",
        _path_rebind=path_rebind, **kwargs,
    )


def test_complete_activation_installs_data_retires_pointers_and_only_then_completes(roots):
    result = run(roots)
    source, harness, knowledge, _candidate, checkpoint, _rollback, _baseline = roots
    assert result["rollback_completed"] is True
    assert result["legacy_writer_thawed"] is True
    assert result["installation_path_rebound"] is True
    assert result["indexes_rebuilt"] is True
    assert result["credential_continuity_verified"] is True
    assert not (source / MARKER_NAME).exists() and not (source / PART_NAME).exists()
    assert not (harness / "active-installation.json").exists()
    assert not (knowledge / "active-installation.json").exists()
    with sqlite3.connect(source / "db/catalog.sqlite3") as db:
        title, storage = db.execute(
            "SELECT title,storage_path FROM knowledge_documents WHERE id='doc-1'").fetchone()
    assert title == "window title"
    assert Path(storage).read_bytes() == b"# restored\n"
    final = json.loads((checkpoint / activation.CHECKPOINT_NAME).read_bytes())
    assert final["state"] == "rollback_completed" and final["rollback_completed"] is True
    first_receipt = (checkpoint / activation.ACTIVATION_NAME).read_bytes()
    again = run(roots)
    assert again == {**result, "idempotent": True}
    assert (checkpoint / activation.ACTIVATION_NAME).read_bytes() == first_receipt


def test_completed_retry_does_not_contend_with_live_legacy_writer(roots):
    result = run(roots)
    source = roots[0]
    with (source / LOCK_NAME).open("r+b") as gate:
        fcntl.flock(gate, fcntl.LOCK_SH | fcntl.LOCK_NB)
        assert run(roots) == {**result, "idempotent": True}


def test_mismatched_product_pointers_are_rejected_before_either_is_retired(roots):
    source, harness, knowledge, _candidate, checkpoint, _rollback, _baseline = roots
    _canonical(knowledge / "active-installation.json", {
        "format": "puddingharness-active-installation/v1",
        "active_installation_revision": "sha256:" + "b" * 64,
    })
    with pytest.raises(ValueError, match="pointers disagree"):
        run(roots)
    assert (harness / "active-installation.json").exists()
    assert (knowledge / "active-installation.json").exists()
    assert (source / MARKER_NAME).exists()
    assert not (checkpoint / activation.ACTIVATION_NAME).exists()


def test_assignment_checkpoint_cannot_preclaim_rollback_completion(roots):
    checkpoint = roots[5]
    value = json.loads(checkpoint.read_bytes())
    value["rollback_completed"] = True
    _canonical(checkpoint, value)
    with pytest.raises(ValueError, match="awaiting legacy activation"):
        run(roots)
    assert (roots[0] / MARKER_NAME).exists()
    assert not (roots[4] / activation.ACTIVATION_NAME).exists()


def test_checkpoint_requires_exact_schema_and_self_digested_journal_chain(roots):
    checkpoint = roots[5]
    value = json.loads(checkpoint.read_bytes())
    value["unexpected"] = True
    _canonical(checkpoint, value)
    with pytest.raises(ValueError, match="awaiting legacy activation"):
        run(roots)
    value.pop("unexpected")
    value["journals"]["harness"]["events"][-1]["migration_manifest_sha256"] = "f" * 64
    _canonical(checkpoint, value)
    with pytest.raises(ValueError, match="digest mismatch"):
        run(roots)
    assert (roots[0] / MARKER_NAME).exists()
    assert (roots[1] / "active-installation.json").exists()


def test_rollback_evidence_rejects_candidate_body_swap_before_install(roots):
    (roots[3] / "bodies/doc.md").write_bytes(b"swapped candidate body\n")
    with pytest.raises(ValueError, match="content commitment changed"):
        run(roots)
    assert not (roots[0] / ".rollback-activation-v1").exists()
    assert (roots[0] / MARKER_NAME).exists()
    assert (roots[1] / "active-installation.json").exists()


def test_pre_cutover_abort_completes_without_active_pointer_retirement(roots):
    source, harness, knowledge, _candidate, checkpoint, rollback, _baseline = roots
    (harness / "active-installation.json").unlink()
    (knowledge / "active-installation.json").unlink()
    _rollback_checkpoint(rollback, window=False)
    result = run(roots)
    assert result["rollback_mode"] == "pre_cutover_abort"
    assert result["rollback_completed"] is True
    disposition = json.loads((checkpoint / activation.POINTERS_NAME).read_bytes())
    assert disposition == {
        "format": "puddingharness-active-pointer-retirement/v1",
        "state": "not_applicable_pre_cutover",
        "rollback_mode": "pre_cutover_abort",
        "pointers": [],
        "active_pointers_retired": False,
        "activation_allowed": False,
        "rollback_completed": False,
    }
    assert run(roots) == {**result, "idempotent": True}


def test_pre_cutover_abort_rejects_unexpected_cutover_pointer(roots):
    _rollback_checkpoint(roots[5], window=False)
    with pytest.raises(ValueError, match="cannot retire"):
        run(roots)
    assert (roots[0] / MARKER_NAME).exists()
    assert (roots[1] / "active-installation.json").exists()
    assert not (roots[4] / activation.ACTIVATION_NAME).exists()


def test_rebind_receipt_cannot_hide_a_path_outside_installed_candidate(roots):
    def lying_rebind(python, candidate, receipt):
        result = _fake_rebind(python, candidate, receipt)
        with sqlite3.connect(candidate / "catalog.sqlite3") as db:
            db.execute("UPDATE knowledge_documents SET storage_path='/outside/doc.md'")
        result["catalog_sha256_after"] = hashlib.sha256(
            (candidate / "catalog.sqlite3").read_bytes()).hexdigest()
        activation._write(receipt, result)
        return result

    with pytest.raises(ValueError, match="not rebound"):
        run(roots, _path_rebind=lying_rebind)
    assert (roots[0] / MARKER_NAME).exists()
    assert (roots[1] / "active-installation.json").exists()
    assert (roots[2] / "active-installation.json").exists()
    assert not (roots[4] / activation.ACTIVATION_NAME).exists()


def test_health_failure_restores_source_freeze_and_never_completes(roots):
    calls = 0

    def fail_after_archive(home, candidate):
        nonlocal calls
        calls += 1
        result = activation._health(home, candidate)
        if calls == 2:
            raise ValueError("injected post-thaw health failure")
        return result

    with pytest.raises(ValueError, match="health failure"):
        run(roots, _health_probe=fail_after_archive)
    source, _harness, _knowledge, _candidate, checkpoint, _rollback, _baseline = roots
    assert (source / MARKER_NAME).exists() and not (source / PART_NAME).exists()
    assert not (checkpoint / activation.ACTIVATION_NAME).exists()
    assert json.loads((checkpoint / activation.CHECKPOINT_NAME).read_bytes())["rollback_completed"] is False
    assert run(roots)["rollback_completed"] is True


def test_sigkill_window_after_marker_unlink_is_adopted_exactly(roots):
    def crash():
        raise RuntimeError("injected crash after audited thaw")

    with pytest.raises(RuntimeError, match="injected crash"):
        run(roots, _after_thaw=crash)
    source, _harness, _knowledge, _candidate, checkpoint, _rollback, _baseline = roots
    assert not (source / MARKER_NAME).exists() and not (source / PART_NAME).exists()
    assert not (checkpoint / activation.ACTIVATION_NAME).exists()
    result = run(roots)
    assert result["rollback_completed"] is True
    assert (checkpoint / activation.ACTIVATION_NAME).exists()


@pytest.mark.parametrize("stop", ["candidate_installed", "paths_rebound", "indexes_rebuilt",
                                  "credentials_verified", "catalog_activated",
                                  "legacy_health_verified", "active_pointers_retired"])
def test_checkpoint_interruptions_resume_fail_closed(roots, stop):
    def interrupt(state):
        if state == stop:
            raise RuntimeError("checkpoint interruption")

    with pytest.raises(RuntimeError, match="checkpoint interruption"):
        run(roots, _after_checkpoint=interrupt)
    source = roots[0]
    assert (source / MARKER_NAME).exists()
    assert json.loads((roots[4] / activation.CHECKPOINT_NAME).read_bytes())["rollback_completed"] is False
    assert run(roots)["rollback_completed"] is True


def test_resume_after_rebind_never_reexecutes_the_one_way_rebind(roots):
    calls = 0

    def one_shot_rebind(python, candidate, receipt):
        nonlocal calls
        calls += 1
        if calls != 1:
            raise AssertionError("path rebind was executed more than once")
        return _fake_rebind(python, candidate, receipt)

    def interrupt(state):
        if state == "credentials_verified":
            raise RuntimeError("checkpoint interruption")

    with pytest.raises(RuntimeError, match="checkpoint interruption"):
        run(roots, _path_rebind=one_shot_rebind, _after_checkpoint=interrupt)
    assert run(roots, _path_rebind=one_shot_rebind)["rollback_completed"] is True
    assert calls == 1


def test_candidate_body_tamper_after_install_is_not_adopted_on_retry(roots):
    def interrupt(state):
        if state == "candidate_installed":
            raise RuntimeError("checkpoint interruption")

    with pytest.raises(RuntimeError, match="checkpoint interruption"):
        run(roots, _after_checkpoint=interrupt)
    installed = (roots[0] / ".rollback-activation-v1"
                 / "window-1-candidate" / "bodies" / "doc.md")
    installed.write_bytes(b"tampered after crash\n")
    with pytest.raises(ValueError, match="Installed rollback candidate changed"):
        run(roots)
    assert (roots[0] / MARKER_NAME).exists()
    assert (roots[1] / "active-installation.json").exists()
    assert (roots[2] / "active-installation.json").exists()
    assert not (roots[4] / activation.ACTIVATION_NAME).exists()


def test_catalog_backup_symlink_cannot_escape_checkpoint_stage(roots):
    outside = roots[4].parent / "outside-backup.sqlite3"
    outside.write_bytes(b"must remain unchanged")
    (roots[4] / "legacy-catalog-before.sqlite3").symlink_to(outside)
    with pytest.raises(ValueError, match="backup changed"):
        run(roots)
    assert outside.read_bytes() == b"must remain unchanged"
    assert (roots[0] / MARKER_NAME).exists()
    assert (roots[1] / "active-installation.json").exists()
    assert not (roots[4] / activation.ACTIVATION_NAME).exists()


def test_crash_after_atomic_catalog_replace_before_receipt_replays(roots, monkeypatch):
    original = activation._write
    crashed = False

    def crash_before_catalog_receipt(path, value):
        nonlocal crashed
        if path.name == activation.CATALOG_NAME and not crashed:
            crashed = True
            raise RuntimeError("crash before catalog receipt")
        return original(path, value)

    monkeypatch.setattr(activation, "_write", crash_before_catalog_receipt)
    with pytest.raises(RuntimeError, match="crash before catalog receipt"):
        run(roots)
    with sqlite3.connect(roots[0] / "db/catalog.sqlite3") as db:
        assert db.execute("SELECT title FROM knowledge_documents").fetchone()[0] == "window title"
    assert (roots[0] / MARKER_NAME).exists()
    monkeypatch.setattr(activation, "_write", original)
    assert run(roots)["rollback_completed"] is True


def test_completed_retry_rejects_dangling_active_pointer(roots):
    run(roots)
    dangling = roots[1] / "active-installation.json"
    dangling.symlink_to(roots[1] / "missing-pointer")
    with pytest.raises(ValueError, match="Completed rollback activation changed"):
        run(roots)


def test_credential_drift_refuses_before_catalog_or_thaw(roots):
    source = roots[0]
    (source / "users/local/credentials/registry.enc").write_bytes(b"changed")
    with pytest.raises(ValueError, match="credential continuity"):
        run(roots)
    assert (source / MARKER_NAME).exists()
    assert not (roots[4] / activation.CATALOG_NAME).exists()
    assert not (roots[4] / activation.ACTIVATION_NAME).exists()
