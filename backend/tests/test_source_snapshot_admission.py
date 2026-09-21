import fcntl
import hashlib
import json
import os
from pathlib import Path

import pytest
from harness.source_snapshot import VerifiedSourceSnapshot, FORMAT, LOCK_NAME, _inventory
from harness.migration_orchestrator import prepare_migration
from test_harness_migration_orchestrator import _python_wrapper


def encoded(value): return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()
def sha(value): return hashlib.sha256(value).hexdigest()


def fixture_snapshot(tmp_path):
    root=tmp_path/'snapshot';root.mkdir(mode=0o700)
    payload=root/'payload';payload.mkdir(mode=0o700)
    (payload/'sessions').mkdir(mode=0o700)
    for relative,data in {'config.json':b'{}','sessions/s.json':b'{}','other-domain.bin':b'not imported by Harness'}.items():
        path=payload/relative;path.write_bytes(data);path.chmod(0o600)
    (root/LOCK_NAME).touch(mode=0o600)
    files={p.relative_to(payload).as_posix():{'size':p.stat().st_size,'sha256':sha(p.read_bytes())} for p in payload.rglob('*') if p.is_file()}
    inventory={'files':files,'directories':['sessions'],'total_bytes':sum(v['size'] for v in files.values())}
    plan={'format':FORMAT,'source_identity':'a'*64,'source_directory_identity':{'device':1,'inode':2},'output_identity':sha(str(root).encode()),'inventory':inventory}
    manifest={'format':FORMAT,'plan_digest':sha(encoded(plan)),'state':'verified_raw_home_snapshot','inventory':inventory,
        'cooperative_admission_held':True,'writer_fence_verified':False,'catalog_normalization_required':True,
        'activation_allowed':False,'installation_prepared':False,'credential_rebind_required':True,
        'requires_domain_migration':True,'source_home_kind':'legacy_puddingclaw_home',
        'catalog':{'relative_path':'db/catalog.sqlite3','role':'legacy_claw_core_catalog','direct_import_allowed':False,'wal_policy':'captured_as_sibling_files'}}
    for name,value in [('plan.json',plan),('manifest.json',manifest)]:
        (root/name).write_bytes(encoded(value));(root/name).chmod(0o600)
    return root


def test_valid_snapshot_is_read_only_and_holds_shared_admission(tmp_path):
    root=fixture_snapshot(tmp_path)
    before={p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with VerifiedSourceSnapshot(root) as snapshot:
        assert snapshot.commitment['format']==FORMAT
        with (root/LOCK_NAME).open('rb') as stream:
            with pytest.raises(BlockingIOError): fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert snapshot.verify()==snapshot.commitment
    assert before=={p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('change',['file','extra','missing','symlink','hardlink','public','flag','integer_flag','format','duplicate','incomplete','relocated'])
def test_snapshot_rejects_untrusted_or_changed_artifacts_before_staging(tmp_path,change):
    root=fixture_snapshot(tmp_path)
    if change=='file': (root/'payload/other-domain.bin').write_bytes(b'changed')
    if change=='extra': (root/'payload/extra').touch(mode=0o600)
    if change=='missing': (root/'payload/other-domain.bin').unlink()
    if change=='symlink':
        (root/'payload/other-domain.bin').unlink();(root/'payload/other-domain.bin').symlink_to(root/'payload/config.json')
    if change=='hardlink': os.link(root/'payload/config.json',tmp_path/'alias')
    if change=='public': (root/'payload/config.json').chmod(0o644)
    if change in ('flag','integer_flag','format'):
        m=json.loads((root/'manifest.json').read_bytes())
        if change=='flag':m['activation_allowed']=True
        elif change=='integer_flag':m['activation_allowed']=0
        else:m['format']='unknown'
        (root/'manifest.json').write_bytes(encoded(m))
    if change=='duplicate':
        p=root/'plan.json';p.write_bytes(p.read_bytes().replace(b'{',b'{"format":"duplicate",',1))
    if change=='incomplete':(root/'manifest.json.part').touch(mode=0o600)
    if change=='relocated':
        new=tmp_path/'moved';root.rename(new);root=new
    with pytest.raises((ValueError,OSError)):
        prepare_migration(root/'payload',b'{}',Path('/unused-python'),tmp_path/'target',source_home_snapshot=root)
    assert not (tmp_path/'target').exists()


def test_inventory_directory_order_is_canonical_for_prefix_siblings(tmp_path):
    # Real Homes carry sibling directories like 'lark-vc' and 'lark-vc-agent':
    # '-' sorts before '/', so lexicographic order differs from depth-first
    # traversal order. The inventory must be canonical regardless of names.
    root=fixture_snapshot(tmp_path);payload=root/'payload'
    for directory in ['lark-vc','lark-vc/child','lark-vc-agent','lark-vc-agent/child']:
        (payload/directory).mkdir(mode=0o700)
    for relative in ['lark-vc/child/data.bin','lark-vc-agent/child/data.bin']:
        path=payload/relative;path.write_bytes(b'x');path.chmod(0o600)
    files={p.relative_to(payload).as_posix():{'size':p.stat().st_size,'sha256':sha(p.read_bytes())} for p in payload.rglob('*') if p.is_file()}
    directories=sorted(p.relative_to(payload).as_posix() for p in payload.rglob('*') if p.is_dir())
    inventory={'files':files,'directories':directories,'total_bytes':sum(v['size'] for v in files.values())}
    assert _inventory(payload)==inventory
    plan={'format':FORMAT,'source_identity':'a'*64,'source_directory_identity':{'device':1,'inode':2},'output_identity':sha(str(root).encode()),'inventory':inventory}
    manifest=json.loads((root/'manifest.json').read_bytes())
    manifest['plan_digest']=sha(encoded(plan));manifest['inventory']=inventory
    (root/'plan.json').write_bytes(encoded(plan));(root/'manifest.json').write_bytes(encoded(manifest))
    with VerifiedSourceSnapshot(root) as snapshot:
        assert snapshot.commitment['format']==FORMAT


def test_orchestrator_binds_envelope_and_prevents_downgrade(tmp_path):
    root=fixture_snapshot(tmp_path);python=_python_wrapper(tmp_path);target=tmp_path/'target'
    first=prepare_migration(root/'payload',b'{}',python,target,source_home_snapshot=root)
    assert first==prepare_migration(root/'payload',b'{}',python,target,source_home_snapshot=root)
    assert 'source_snapshot_commitment' in first
    assert json.loads((target/'plan.json').read_bytes())['source_snapshot_commitment']==first['source_snapshot_commitment']
    before=(target/'checkpoint.json').read_bytes()
    with pytest.raises(ValueError,match='plan changed'):
        prepare_migration(root/'payload',b'{}',python,target)
    assert (target/'checkpoint.json').read_bytes()==before


def test_ignored_domain_tamper_during_delegation_prevents_completion(tmp_path):
    root=fixture_snapshot(tmp_path);python=_python_wrapper(tmp_path);target=tmp_path/'target'
    def tamper(phase):
        if phase=='harness_verified': (root/'payload/other-domain.bin').write_bytes(b'changed')
    with pytest.raises(ValueError,match='integrity'):
        prepare_migration(root/'payload',b'{}',python,target,source_home_snapshot=root,_after_checkpoint=tamper)
    assert json.loads((target/'checkpoint.json').read_bytes())['state']=='harness_verified'


def test_validly_rewritten_envelope_is_not_same_commitment(tmp_path):
    root=fixture_snapshot(tmp_path)
    with VerifiedSourceSnapshot(root) as snapshot:
        p=json.loads((root/'plan.json').read_bytes());p['source_identity']='b'*64
        m=json.loads((root/'manifest.json').read_bytes());m['plan_digest']=sha(encoded(p))
        (root/'plan.json').write_bytes(encoded(p));(root/'manifest.json').write_bytes(encoded(m))
        with pytest.raises(ValueError,match='commitment changed'): snapshot.verify()


def test_staging_inside_snapshot_envelope_is_rejected(tmp_path):
    root=fixture_snapshot(tmp_path)
    with pytest.raises(ValueError):
        prepare_migration(root/'payload',b'{}','/unused',root/'target',source_home_snapshot=root)
    assert not (root/'target').exists()


def test_delegate_retains_snapshot_admission_after_parent_sigkill(tmp_path):
    import multiprocessing
    import signal
    import time
    root=fixture_snapshot(tmp_path);python=_python_wrapper(tmp_path);target=tmp_path/'target'
    module=tmp_path/'fake-python/knowledge_platform/distribution/migrate_from_claw.py'
    marker=tmp_path/'child-pid';release=tmp_path/'release-child'
    original=module.read_text()
    module.write_text("import os,time\nfrom pathlib import Path\nPath("+repr(str(marker))+").write_text(str(os.getpid()))\nwhile not Path("+repr(str(release))+").exists(): time.sleep(.02)\n"+original)
    parent=multiprocessing.get_context('fork').Process(target=prepare_migration,
        args=(root/'payload',b'{}',python,target),kwargs={'source_home_snapshot':root})
    parent.start();child_pid=None
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and parent.is_alive() and time.monotonic()<deadline: time.sleep(.02)
        assert marker.exists();child_pid=int(marker.read_text());os.kill(child_pid,0)
        parent.kill();parent.join(timeout=5)
        assert parent.exitcode == -signal.SIGKILL
        with (root/LOCK_NAME).open('rb') as stream:
            with pytest.raises(BlockingIOError): fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        release.write_text('continue')
        deadline=time.monotonic()+10
        while True:
            with (root/LOCK_NAME).open('rb') as stream:
                try:
                    fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    child_pid=None
                    break
                except BlockingIOError:
                    assert time.monotonic()<deadline
                    time.sleep(.02)
        assert prepare_migration(root/'payload',b'{}',python,target,source_home_snapshot=root)['state']=='verified_inactive_partial'
    finally:
        if parent.is_alive(): parent.kill();parent.join(timeout=5)
        if child_pid:
            try: os.kill(child_pid,signal.SIGKILL)
            except ProcessLookupError: pass
