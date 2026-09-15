import os
import json
from pathlib import Path
import sys

import pytest

from harness.migration_orchestrator import prepare_migration


def _source(root: Path) -> Path:
    source = root / "source"
    (source / "sessions").mkdir(parents=True)
    (source / "sessions" / "session.json").write_bytes(b"{}")
    (source / "config.json").write_bytes(b"{}")
    return source


def _python_module(root: Path) -> Path:
    package = root / "fake-python" / "knowledge_platform" / "distribution"
    package.mkdir(parents=True)
    for parent in (package.parent.parent, package.parent, package):
        (parent / "__init__.py").write_text("")
    (package / "migrate_from_claw.py").write_text(
        "import argparse, hashlib, json, pathlib\n"
        "p=argparse.ArgumentParser(); p.add_argument('--source-snapshot'); p.add_argument('--request'); p.add_argument('--output'); a=p.parse_args()\n"
        "request=pathlib.Path(a.request).read_bytes(); out=pathlib.Path(a.output); out.mkdir(exist_ok=True)\n"
        "body=b'artifact'; (out/'catalog.json').write_bytes(body); (out/'catalog.json').chmod(0o600)\n"
        "print(json.dumps({'format':'puddingknowledge-migrate-from-claw-receipt/v1',"
        "'source_snapshot_identity':'sha256:'+hashlib.sha256(a.source_snapshot.encode()).hexdigest(),'request_digest':'sha256:'+hashlib.sha256(request).hexdigest(),'state':'verified_inactive_partial',"
        "'artifacts':{'catalog.json':'sha256:'+hashlib.sha256(body).hexdigest()},"
        "'covered_domains':['document_catalog','document_blobs'],"
        "'pending_domains':['other_catalog_domains','wiki','indexes','knowledge_credentials'],"
        "'activation_allowed':False,'installation_prepared':False,'writer_fence_verified':False,'credential_rebind_required':True}))\n"
    )
    identity = {'format':'puddingknowledge-installed-identity/v1', 'package':'puddingknowledge-local',
                'version':'0.1.0', 'inventory_sha256':'sha256:'+'a'*64, 'file_count':10,
                'scope':'owned_distribution_files', 'authenticated':False}
    (package / 'installed_identity.json').write_text(json.dumps(identity))
    (package / 'installed_identity.py').write_text("from pathlib import Path\nprint(Path(__file__).with_suffix('.json').read_text())\n")
    return root / "fake-python"


def _python_wrapper(root: Path) -> Path:
    module_root = _python_module(root)
    wrapper = root / "python-wrapper"
    wrapper.write_text(f"#!/bin/sh\nPYTHONPATH={module_root}:{Path(__file__).parents[1]} exec {sys.executable} \"$@\"\n")
    wrapper.chmod(0o700)
    return wrapper


def test_prepare_delegates_and_returns_bounded_receipt(tmp_path, monkeypatch):
    source = _source(tmp_path)
    wrapper = _python_wrapper(tmp_path)
    request = b'{"opaque":true}'
    result = prepare_migration(source, request, wrapper, tmp_path / "stage")
    assert result["format"] == "puddingharness-migration-orchestrator/v1"
    assert result["state"] == "verified_inactive_partial"
    assert result["activation_allowed"] is False
    assert result["credential_rebind_required"] is True
    assert (tmp_path / "stage" / "harness" / "manifest.json").exists()


def test_request_and_source_change_are_rejected(tmp_path, monkeypatch):
    source = _source(tmp_path)
    wrapper = _python_wrapper(tmp_path)
    stage = tmp_path / "stage"
    prepare_migration(source, b'{"opaque":true}', wrapper, stage)
    with pytest.raises(ValueError, match="request"):
        prepare_migration(source, b'{"opaque":false}', Path(sys.executable), stage)


def test_bad_receipt_is_fail_closed(tmp_path, monkeypatch):
    source = _source(tmp_path)
    module_root = _python_module(tmp_path)
    wrapper = tmp_path / "python-wrapper"
    wrapper.write_text(f"#!/bin/sh\nPYTHONPATH={module_root}:{Path(__file__).parents[1]} exec {sys.executable} \"$@\"\n")
    wrapper.chmod(0o700)
    module = module_root / "knowledge_platform" / "distribution" / "migrate_from_claw.py"
    module.write_text("print('{}')")
    with pytest.raises(ValueError, match="receipt"):
        prepare_migration(source, b'{"opaque":true}', wrapper, tmp_path / "stage")


def test_path_and_limits_fail_closed(tmp_path):
    source = _source(tmp_path)
    with pytest.raises(ValueError):
        prepare_migration(source, b"", Path(sys.executable), tmp_path / "stage")
    with pytest.raises(ValueError):
        prepare_migration(source, b"{}", Path(sys.executable), source / "nested")


def test_failed_completed_retry_preserves_old_receipt_commitment(tmp_path):
    source = _source(tmp_path); python = _python_wrapper(tmp_path); stage = tmp_path/'stage'
    first = prepare_migration(source, b'{}', python, stage)
    before = (stage/'checkpoint.json').read_bytes()
    module = tmp_path/'fake-python/knowledge_platform/distribution/migrate_from_claw.py'
    module.write_text(module.read_text().replace("body=b'artifact'", "body=b'changed'"))
    for _ in range(2):
        with pytest.raises(ValueError, match='receipt changed'):
            prepare_migration(source, b'{}', python, stage)
        assert (stage/'checkpoint.json').read_bytes() == before


@pytest.mark.parametrize('change', ['request_digest','snapshot_identity','flag','artifact_digest','artifact_path','labels'])
def test_receipt_adversarial_validation(tmp_path, change):
    source = _source(tmp_path); python = _python_wrapper(tmp_path)
    module = tmp_path/'fake-python/knowledge_platform/distribution/migrate_from_claw.py'
    text = module.read_text()
    if change == 'request_digest': text=text.replace("hashlib.sha256(request)","hashlib.sha256(b'wrong')")
    if change == 'snapshot_identity': text=text.replace('hashlib.sha256(a.source_snapshot.encode())',"hashlib.sha256(b'wrong')")
    if change == 'flag': text=text.replace("'installation_prepared':False", "'installation_prepared':0")
    if change == 'artifact_digest': text=text.replace("hashlib.sha256(body)","hashlib.sha256(b'wrong')")
    if change == 'artifact_path': text=text.replace("'artifacts':{'catalog.json'", "'artifacts':{'../catalog.json'")
    if change == 'labels': text=text.replace("['document_catalog','document_blobs']", "['wiki']")
    module.write_text(text)
    with pytest.raises(ValueError): prepare_migration(source,b'{}',python,tmp_path/'stage')
    assert json.loads((tmp_path/'stage/checkpoint.json').read_text())['state']=='harness_verified'


@pytest.mark.parametrize('mode', ['timeout','large_stdout','failure','source_change'])
def test_delegate_failure_cannot_complete(tmp_path, mode):
    source = _source(tmp_path); python = _python_wrapper(tmp_path)
    module = tmp_path/'fake-python/knowledge_platform/distribution/migrate_from_claw.py'
    if mode == 'timeout': module.write_text('import time;time.sleep(60)')
    if mode == 'large_stdout': module.write_text("import sys;sys.stdout.write('x'*(2*1024*1024))")
    if mode == 'failure': module.write_text('raise SystemExit(9)')
    if mode == 'source_change': module.write_text("from pathlib import Path\nPath("+repr(str(source/'sessions/session.json'))+").write_text('changed')\n"+module.read_text())
    with pytest.raises(ValueError): prepare_migration(source,b'{}',python,tmp_path/'stage',timeout_seconds=1)
    assert json.loads((tmp_path/'stage/checkpoint.json').read_text())['state']=='harness_verified'


def test_partial_atomic_temporary_is_validated_and_recovered(tmp_path):
    source = _source(tmp_path); python = _python_wrapper(tmp_path); stage=tmp_path/'stage'
    stage.mkdir(mode=0o700)
    part=stage/'.plan.json.tmp-0123456789abcdef';part.write_bytes(b'{');part.chmod(0o600)
    assert prepare_migration(source,b'{}',python,stage)['state']=='verified_inactive_partial'
    assert not part.exists()
    (stage/'.plan.json.tmp-unowned').mkdir()
    with pytest.raises(ValueError): prepare_migration(source,b'{}',python,stage)


def test_live_delegate_retains_lock_after_parent_sigkill(tmp_path):
    import fcntl
    import multiprocessing
    import signal
    import time
    source=_source(tmp_path); python=_python_wrapper(tmp_path); stage=tmp_path/'stage'
    module=tmp_path/'fake-python/knowledge_platform/distribution/migrate_from_claw.py'
    marker=tmp_path/'child-pid';release=tmp_path/'release-child'
    original=module.read_text()
    module.write_text("import os,time\nfrom pathlib import Path\nPath("+repr(str(marker))+").write_text(str(os.getpid()))\nwhile not Path("+repr(str(release))+").exists(): time.sleep(.02)\n"+original)
    parent=multiprocessing.get_context('fork').Process(target=prepare_migration,args=(source,b'{}',python,stage))
    parent.start();child_pid=None
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and parent.is_alive() and time.monotonic()<deadline: time.sleep(.02)
        assert marker.exists();child_pid=int(marker.read_text());os.kill(child_pid,0)
        os.kill(parent.pid,signal.SIGKILL);parent.join(timeout=5)
        assert parent.exitcode == -signal.SIGKILL
        with pytest.raises(BlockingIOError): prepare_migration(source,b'{}',python,stage)
        release.write_text('continue')
        deadline=time.monotonic()+10
        while True:
            with (stage/'.orchestrator.lock').open('r+b') as stream:
                try: fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    assert time.monotonic()<deadline
                    time.sleep(.02)
        assert prepare_migration(source,b'{}',python,stage)['state']=='verified_inactive_partial'
    finally:
        if parent.is_alive(): parent.kill();parent.join(timeout=5)
        if child_pid:
            try: os.kill(child_pid,signal.SIGKILL)
            except ProcessLookupError: pass


def test_same_version_new_installed_release_cannot_resume_old_plan(tmp_path):
    source=_source(tmp_path);python=_python_wrapper(tmp_path);stage=tmp_path/'stage'
    prepare_migration(source,b'{}',python,stage)
    checkpoint=(stage/'checkpoint.json').read_bytes()
    identity=tmp_path/'fake-python/knowledge_platform/distribution/installed_identity.json'
    identity.write_text(identity.read_text().replace('a'*64,'b'*64))
    with pytest.raises(ValueError,match='plan changed'):
        prepare_migration(source,b'{}',python,stage)
    assert (stage/'checkpoint.json').read_bytes()==checkpoint


def test_release_change_during_delegate_cannot_complete_checkpoint(tmp_path):
    source=_source(tmp_path);python=_python_wrapper(tmp_path);stage=tmp_path/'stage'
    module=tmp_path/'fake-python/knowledge_platform/distribution/migrate_from_claw.py'
    identity=module.with_name('installed_identity.json')
    module.write_text(module.read_text()+f"\np=pathlib.Path({str(identity)!r});p.write_text(p.read_text().replace('a'*64,'b'*64))\n")
    with pytest.raises(ValueError,match='release changed'):
        prepare_migration(source,b'{}',python,stage)
    assert json.loads((stage/'checkpoint.json').read_text())['state']=='harness_verified'
