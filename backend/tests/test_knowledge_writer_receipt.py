import hashlib
from pathlib import Path
import pytest
from harness.installation_authority import digest, encoded
from harness.knowledge_writer_receipt import inspect_binding, validate_receipt

def _fixture(tmp_path: Path):
    workspace = tmp_path / "workspace"; authority = tmp_path / "authority"
    workspace.mkdir(mode=0o700); authority.mkdir(mode=0o700)
    (workspace / "workspace.json").write_bytes(b'{"version":1}\n')
    (workspace / "catalog.sqlite3").write_bytes(b"sqlite fixture")
    (workspace / "workspace.json").chmod(0o600); (workspace / "catalog.sqlite3").chmod(0o600)
    binding = {"format":"puddingknowledge-writer-authority/v1",
      "workspace":{"path":str(workspace),"device":workspace.stat().st_dev,"inode":workspace.stat().st_ino},
      "authority":{"path":str(authority),"device":authority.stat().st_dev,"inode":authority.stat().st_ino},
      "enrollment_id":"enroll-1",
      "workspace_manifest_sha256":hashlib.sha256((workspace/"workspace.json").read_bytes()).hexdigest()}
    event = {"revision":0,"previous":None,"operation_id":"enroll-1","state":"existing_writer",
      "writers":{"knowledge_catalog":"puddingknowledge","connector_jobs":"puddingknowledge"},
      "freeze_receipt_sha256":None}; event["sha256"] = digest(event)
    journal={"format":binding["format"],"binding_sha256":digest(binding),"events":[event]}
    (workspace/".workspace-authority-v1.json").write_bytes(encoded(binding))
    (authority/"journal.json").write_bytes(encoded(journal))
    (workspace/".workspace-authority-v1.json").chmod(0o600); (authority/"journal.json").chmod(0o600)
    return workspace, authority, binding, journal

def _suspended(workspace, authority, binding, journal):
    marker={"activation_allowed":False,
      "directory_identity":{"device":workspace.stat().st_dev,"inode":workspace.stat().st_ino},
      "format":"puddingknowledge-workspace-freeze/v1","operation_id":"suspend-1",
      "workspace_manifest_sha256":binding["workspace_manifest_sha256"],
      "root_path_sha256":hashlib.sha256(str(workspace).encode()).hexdigest(),"state":"workspace_frozen"}
    marker_raw=encoded(marker); (workspace/".workspace-freeze-v1.json").write_bytes(marker_raw)
    (workspace/".workspace-freeze-v1.json").chmod(0o600)
    event={"revision":1,"previous":journal["events"][0]["sha256"],"operation_id":"suspend-1","state":"suspended",
      "writers":{"knowledge_catalog":None,"connector_jobs":None},
      "freeze_receipt_sha256":hashlib.sha256(marker_raw).hexdigest()}; event["sha256"]=digest(event)
    result=dict(journal,events=[journal["events"][0],event]); (authority/"journal.json").write_bytes(encoded(result))
    (authority/"journal.json").chmod(0o600)
    return result

def _receipt(journal):
    return encoded({"format":"puddingknowledge-writer-authority/v1","status":"ok","journal":journal,
                    "installation_cutover_performed":False})

def test_status_receipt_binds_real_files_and_allows_revision_zero(tmp_path):
    workspace, authority, binding, journal = _fixture(tmp_path)
    assert inspect_binding(workspace) == binding
    assert validate_receipt(_receipt(journal), workspace, binding) == journal

def test_suspend_receipt_requires_marker_and_exact_operation(tmp_path):
    workspace, authority, binding, journal = _fixture(tmp_path)
    suspended = _suspended(workspace, authority, binding, journal)
    assert validate_receipt(_receipt(suspended), workspace, binding, "suspend-1") == suspended
    with pytest.raises(ValueError): validate_receipt(_receipt(suspended), workspace, binding, "other")

@pytest.mark.parametrize("change", ["wrapper","journal","binding","marker"])
def test_receipt_rejects_stale_or_tampered_state(tmp_path, change):
    workspace, authority, binding, journal = _fixture(tmp_path)
    if change == "wrapper":
        raw=encoded({"format":binding["format"],"status":"ok","journal":journal,"installation_cutover_performed":True})
    elif change == "journal":
        altered=dict(journal,events=[*journal["events"]]); altered["events"][0]=dict(altered["events"][0],operation_id="tampered")
        raw=_receipt(altered)
    elif change == "binding":
        with pytest.raises(ValueError): validate_receipt(_receipt(journal),workspace,dict(binding,enrollment_id="wrong"))
        return
    else:
        suspended=_suspended(workspace,authority,binding,journal); (workspace/".workspace-freeze-v1.json").write_bytes(b"{}\n")
        raw=_receipt(suspended)
    with pytest.raises(ValueError): validate_receipt(raw,workspace,binding)

def _freeze_marker(workspace, binding, operation):
    marker={"activation_allowed":False,
      "directory_identity":{"device":workspace.stat().st_dev,"inode":workspace.stat().st_ino},
      "format":"puddingknowledge-workspace-freeze/v1","operation_id":operation,
      "workspace_manifest_sha256":binding["workspace_manifest_sha256"],
      "root_path_sha256":hashlib.sha256(str(workspace).encode()).hexdigest(),"state":"workspace_frozen"}
    raw=encoded(marker); (workspace/".workspace-freeze-v1.json").write_bytes(raw)
    (workspace/".workspace-freeze-v1.json").chmod(0o600)
    return hashlib.sha256(raw).hexdigest()

def _assigned(authority, suspended, *, writer="puddingknowledge", rollback=None, manifest_hex="c"*64):
    head=suspended["events"][-1]
    event={"revision":len(suspended["events"]),"previous":head["sha256"],"operation_id":head["operation_id"],"state":"assigned",
      "writers":{"knowledge_catalog":writer,"connector_jobs":writer},"freeze_receipt_sha256":head["freeze_receipt_sha256"],
      "active_installation_revision":"sha256:"+manifest_hex,"migration_manifest_sha256":manifest_hex,
      "rollback_evidence_sha256":rollback}
    event["sha256"]=digest(event)
    result=dict(suspended,events=[*suspended["events"],event])
    (authority/"journal.json").write_bytes(encoded(result)); (authority/"journal.json").chmod(0o600)
    return result

def test_assigned_revision_accepted_without_freeze_validation(tmp_path):
    workspace, authority, binding, journal = _fixture(tmp_path)
    suspended = _suspended(workspace, authority, binding, journal)
    assigned = _assigned(authority, suspended)
    assert validate_receipt(_receipt(assigned), workspace, binding) == assigned
    with pytest.raises(ValueError): validate_receipt(_receipt(assigned), workspace, binding, "suspend-1")

def test_rollback_assigned_revision_accepted(tmp_path):
    workspace, authority, binding, journal = _fixture(tmp_path)
    suspended = _suspended(workspace, authority, binding, journal)
    assigned = _assigned(authority, suspended, writer="puddingclaw", rollback="d"*64)
    assert validate_receipt(_receipt(assigned), workspace, binding) == assigned

def test_resuspended_revision_three_validates_the_new_marker(tmp_path):
    workspace, authority, binding, journal = _fixture(tmp_path)
    suspended = _suspended(workspace, authority, binding, journal)
    assigned = _assigned(authority, suspended)
    commitment = _freeze_marker(workspace, binding, "suspend-2")
    head = assigned["events"][-1]
    event={"revision":3,"previous":head["sha256"],"operation_id":"suspend-2","state":"suspended",
      "writers":{"knowledge_catalog":None,"connector_jobs":None},"freeze_receipt_sha256":commitment}
    event["sha256"]=digest(event)
    result=dict(assigned,events=[*assigned["events"],event])
    (authority/"journal.json").write_bytes(encoded(result)); (authority/"journal.json").chmod(0o600)
    assert validate_receipt(_receipt(result), workspace, binding) == result
    (workspace/".workspace-freeze-v1.json").write_bytes(b"{}\n")
    with pytest.raises(ValueError): validate_receipt(_receipt(result), workspace, binding)

@pytest.mark.parametrize("change", ["alternation","mixed_writers","unknown_writer","receipt","missing_field","rollback_missing","rollback_unexpected","revision_pointer","manifest_prefix"])
def test_assigned_chain_violations_rejected(tmp_path, change):
    workspace, authority, binding, journal = _fixture(tmp_path)
    suspended = _suspended(workspace, authority, binding, journal)
    head = suspended["events"][-1]
    event={"revision":2,"previous":head["sha256"],"operation_id":"suspend-1","state":"assigned",
      "writers":{"knowledge_catalog":"puddingknowledge","connector_jobs":"puddingknowledge"},
      "freeze_receipt_sha256":head["freeze_receipt_sha256"],
      "active_installation_revision":"sha256:"+"c"*64,"migration_manifest_sha256":"c"*64,"rollback_evidence_sha256":None}
    if change=="alternation":
        event={"revision":2,"previous":head["sha256"],"operation_id":"suspend-1","state":"suspended",
          "writers":{"knowledge_catalog":None,"connector_jobs":None},"freeze_receipt_sha256":head["freeze_receipt_sha256"]}
    if change=="mixed_writers": event["writers"]={"knowledge_catalog":"puddingknowledge","connector_jobs":"puddingclaw"}
    if change=="unknown_writer": event["writers"]={"knowledge_catalog":"puddingharness","connector_jobs":"puddingharness"}
    if change=="receipt": event["freeze_receipt_sha256"]="e"*64
    if change=="missing_field": del event["migration_manifest_sha256"]
    if change=="rollback_missing": event["writers"]={"knowledge_catalog":"puddingclaw","connector_jobs":"puddingclaw"}
    if change=="rollback_unexpected": event["rollback_evidence_sha256"]="d"*64
    if change=="revision_pointer": event["active_installation_revision"]="c"*64
    if change=="manifest_prefix": event["migration_manifest_sha256"]="sha256:"+"c"*64
    event["sha256"]=digest(event)
    crafted=dict(suspended,events=[*suspended["events"],event])
    (authority/"journal.json").write_bytes(encoded(crafted)); (authority/"journal.json").chmod(0o600)
    with pytest.raises(ValueError): validate_receipt(_receipt(crafted), workspace, binding)
