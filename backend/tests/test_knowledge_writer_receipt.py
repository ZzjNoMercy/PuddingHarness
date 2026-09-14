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
