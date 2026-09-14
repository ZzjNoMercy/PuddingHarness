import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest
from harness import installation_authority as authority
from harness.session_import import inventory
from harness.source_snapshot import _inventory,_encoded,_sha
from harness.session_reverse import prepare_session_reverse
from test_source_snapshot_admission import fixture_snapshot
from test_writer_barrier import SETUP,KNOWLEDGE

pytestmark=pytest.mark.skipif(not KNOWLEDGE,reason='Explicit independent Knowledge installation required')

@pytest.fixture
def roots(tmp_path):
    root=tmp_path.resolve();source=fixture_snapshot(root)
    p=source/'payload/sessions/deleted.json';p.write_bytes(b'delete me');p.chmod(0o600)
    plan=json.loads((source/'plan.json').read_bytes());plan['inventory']=_inventory(source/'payload')
    manifest=json.loads((source/'manifest.json').read_bytes());manifest['inventory']=plan['inventory'];manifest['plan_digest']=_sha(_encoded(plan))
    (source/'plan.json').write_bytes(_encoded(plan));(source/'manifest.json').write_bytes(_encoded(manifest))
    before=root/'before';before.mkdir(mode=0o700);home=root/'harness';home.mkdir(mode=0o700)
    for name in inventory(source/'payload'):
        for target in (before,home):
            p=target/name;p.parent.mkdir(mode=0o700,parents=True,exist_ok=True);shutil.copyfile(source/'payload'/name,p)
    (home/'sessions/s.json').write_bytes(b'new conversation');(home/'sessions/deleted.json').unlink()
    (home/'sessions/new.json').write_bytes(b'new session')
    authority.enroll(home,root/'harness-authority','enroll-harness')
    subprocess.run([KNOWLEDGE,'-c',SETUP,str(root)],check=True,cwd=root)
    return source,before,home,root/'knowledge',KNOWLEDGE,root/'barrier','reverse-test',root/'candidate'

def run(roots,**kwargs):return prepare_session_reverse(*roots,**kwargs)


def test_actual_file_changes_preserve_other_domains_and_retry(roots):
    result=run(roots);assert result['changes']=={'insert':1,'update':1,'delete':1}
    source,before,home,*_=roots;payload=roots[-1]/'payload'
    assert inventory(payload)==inventory(home)
    for name in ('config.json','other-domain.bin'):
        assert (payload/name).read_bytes()==(source/'payload'/name).read_bytes()
    assert not (payload/'sessions/deleted.json').exists()
    assert (payload/'sessions/new.json').stat().st_mode&0o777==0o600
    retry=run(roots);assert retry==dict(result,idempotent=True)
    assert not result['rollback_completed'] and not result['credential_continuity_verified']


def test_copy_failure_resume(roots):
    def fail(name):raise RuntimeError('interrupted copy')
    with pytest.raises(RuntimeError):run(roots,_after_copy=fail)
    assert json.loads((roots[-1]/'manifest.json').read_bytes())['state']=='copying'
    assert run(roots)['state']=='verified_inactive'

@pytest.mark.parametrize('mode',['tamper','missing','extra'])
def test_completed_candidate_cannot_be_silently_repaired(roots,mode):
    run(roots);candidate=roots[-1];before=(candidate/'manifest.json').read_bytes()
    if mode=='tamper':(candidate/'payload/sessions/new.json').write_bytes(b'tampered')
    if mode=='missing':(candidate/'payload/sessions/new.json').unlink()
    if mode=='extra':(candidate/'payload/extra.bin').touch(mode=0o600)
    with pytest.raises((ValueError,OSError)):run(roots)
    assert before==(candidate/'manifest.json').read_bytes()


def test_bad_baseline_rejected_before_suspension(roots):
    (roots[1]/'sessions/s.json').write_bytes(b'wrong baseline')
    with pytest.raises(ValueError):run(roots)
    assert not (roots[2]/'.installation-freeze-v1.json').exists() and not roots[-1].exists()


def test_source_change_during_copy_denies_completion(roots):
    def mutate(name):(roots[0]/'payload/other-domain.bin').write_bytes(b'changed')
    with pytest.raises(ValueError):run(roots,_after_copy=mutate)
    assert json.loads((roots[-1]/'manifest.json').read_bytes())['state']=='copying'


def test_suspended_home_change_during_copy_denies_completion(roots):
    def mutate(name):(roots[2]/'sessions/new.json').write_bytes(b'changed')
    with pytest.raises(ValueError):run(roots,_after_copy=mutate)
    assert json.loads((roots[-1]/'manifest.json').read_bytes())['state']=='copying'


def test_marker_removed_after_copy_denies_completion(roots):
    def remove(name):(roots[3]/'.workspace-freeze-v1.json').unlink(missing_ok=True)
    with pytest.raises((ValueError,OSError)):run(roots,_after_copy=remove)
    assert json.loads((roots[-1]/'manifest.json').read_bytes())['state']=='copying'


def test_sigkill_copy_releases_leases_and_resumes(roots):
    import sys,time
    ready=roots[-1].parent/'copy-ready'
    code='from harness.session_reverse import prepare_session_reverse;from pathlib import Path;import time\n'
    code+='def wait(name):\n Path('+repr(str(ready))+').write_text(name)\n while True:time.sleep(.02)\n'
    code+='prepare_session_reverse(*'+repr(tuple(map(str,roots)))+',_after_copy=wait)'
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+20
        while not ready.exists():
            assert child.poll() is None and time.monotonic()<deadline;time.sleep(.02)
        child.kill();child.wait(timeout=5)
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
    assert run(roots)['state']=='verified_inactive'


def test_sigkill_mid_file_part_rewritten_without_truncating_source(tmp_path):
    import hashlib,sys,time
    from harness.session_reverse import _copy
    root=tmp_path.resolve();source=root/'input';target=root/'output';ready=root/'ready'
    data=b'unchanged source bytes\x00'*100000;source.write_bytes(data);source.chmod(0o600)
    fact={'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)}
    code='import os,time;from pathlib import Path;from harness.session_reverse import _copy\nreal=os.write\n'
    code+='def paused(fd,data):\n result=real(fd,data[:4096])\n Path('+repr(str(ready))+').write_text("ready")\n while True:time.sleep(.02)\n'
    code+='os.write=paused\n_copy(Path('+repr(str(source))+'),Path('+repr(str(target))+'),'+repr(fact)+')'
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+10
        while not ready.exists():
            assert child.poll() is None and time.monotonic()<deadline;time.sleep(.02)
        child.kill();child.wait(timeout=5)
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
    assert not target.exists() and (root/'output.reverse-part').stat().st_size==4096
    _copy(source,target,fact)
    assert target.read_bytes()==data==source.read_bytes() and not (root/'output.reverse-part').exists()


def test_candidate_sessions_read_by_real_session_manager(roots):
    for name,content in [('s','continued after split'),('new','new post-cutover session')]:
        (roots[2]/'sessions'/f'{name}.json').write_text(json.dumps({'title':name,'created_at':1,'updated_at':2,'messages':[{'role':'user','content':content}]}))
    run(roots)
    # SessionManager.initialize creates archive/traces directories; keep the
    # verified candidate immutable and open a disposable reader copy instead.
    reader=roots[-1].parent/'reader';shutil.copytree(roots[-1]/'payload/sessions',reader/'sessions')
    from graph.session_manager import SessionManager
    manager=SessionManager();manager.initialize(sessions_dir=reader/'sessions')
    assert manager.load_session('s')[0]['content']=='continued after split'
    assert manager.load_session('new')[0]['content']=='new post-cutover session'
    assert manager.load_session('deleted')==[]
    assert run(roots)['idempotent']
