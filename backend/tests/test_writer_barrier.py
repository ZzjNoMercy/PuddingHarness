"""Installed two-product tests opt in through an explicit independent interpreter."""
import json
import os
from pathlib import Path
import subprocess
import time
import pytest
from harness import installation_authority as authority
from harness import writer_barrier as barrier
from harness.installation_guard import InstallationGuard, AdmissionUnavailable

KNOWLEDGE = os.environ.get('KNOWLEDGE_TEST_PYTHON')
pytestmark = pytest.mark.skipif(not KNOWLEDGE, reason='Explicit independent Knowledge installation required')

SETUP = '''
from pathlib import Path
import sys
from sqlalchemy import create_engine,text
from knowledge_platform.catalog.migrations import migrate_to_latest
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.writer_authority import enroll
root=Path(sys.argv[1]); engine=create_engine('sqlite:///'+str(root/'source.sqlite3'))
with engine.begin() as c:
 migrate_to_latest(c)
 c.execute(text("INSERT INTO knowledge_spaces (id,name,description,permissions_json,created_at,updated_at) VALUES ('space_kb_default','fixture','local','{}','now','now')"))
 c.execute(text("INSERT INTO knowledge_datasets (id,space_id,name,version,kind,description,asset_ids,semantic_asset_ids,capabilities,freshness,permissions_json,manifest_digest,created_at,updated_at) VALUES ('dataset_kb_default','space_kb_default','fixture','1','wiki','local','[]','[]','[]','{}','{}','','now','now')"))
engine.dispose(); wiki=root/'source-wiki';wiki.mkdir();(wiki/'guide.md').write_text('# Fixture')
with open_persistent_workspace(root/'knowledge',catalog=root/'source.sqlite3',wiki_root=wiki):pass
enroll(root/'knowledge',root/'knowledge-authority','enroll-knowledge')
'''

@pytest.fixture
def roots(tmp_path):
    root=tmp_path.resolve(); home=root/'harness';home.mkdir(mode=0o700)
    authority.enroll(home,root/'harness-authority','enroll-harness')
    subprocess.run([KNOWLEDGE,'-c',SETUP,str(root)],check=True,cwd=root)
    return home,root/'knowledge',KNOWLEDGE,root/'checkpoint'

def run(roots,**kwargs):return barrier.suspend_writers(*roots,'suspend-pair',**kwargs)

def test_installed_pair_retry_and_restart_denial(roots):
    result=run(roots);assert run(roots)==result
    assert result['state']=='both_writers_suspended' and not result['rollback_completed']
    assert result['journals']['harness']['events'][-1]['writer'] is None
    assert result['journals']['knowledge']['events'][-1]['writers']=={'knowledge_catalog':None,'connector_jobs':None}
    with pytest.raises(AdmissionUnavailable):InstallationGuard(roots[0]).acquire()
    result=subprocess.run([KNOWLEDGE,'-c','from knowledge_platform.local.workspace import open_persistent_workspace;import sys;open_persistent_workspace(sys.argv[1])',str(roots[1])],capture_output=True)
    assert result.returncode!=0


def test_busy_knowledge_resume(roots):
    code='from knowledge_platform.local.workspace import open_persistent_workspace;import sys;w=open_persistent_workspace(sys.argv[1]);print("ready",flush=True);sys.stdin.read()'
    child=subprocess.Popen([KNOWLEDGE,'-c',code,str(roots[1])],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='ready'
        with pytest.raises(ValueError):run(roots)
        partial=(roots[3]/'checkpoint.json').read_bytes()
        assert json.loads(partial)['state']=='harness_suspended'
        with pytest.raises(ValueError):run(roots)
        assert (roots[3]/'checkpoint.json').read_bytes()==partial
    finally:
        child.communicate(timeout=5)
    assert run(roots)['state']=='both_writers_suspended'

@pytest.mark.parametrize('target',[0,1])
def test_missing_committed_marker_never_recreated(roots,target):
    run(roots);checkpoint=(roots[3]/'checkpoint.json').read_bytes()
    marker=roots[target]/('.installation-freeze-v1.json' if target==0 else '.workspace-freeze-v1.json')
    marker.unlink()
    with pytest.raises((ValueError,OSError)):run(roots)
    assert not marker.exists() and (roots[3]/'checkpoint.json').read_bytes()==checkpoint


def test_missing_knowledge_journal_before_harness_mutation(roots):
    (roots[1].parent/'knowledge-authority/journal.json').unlink()
    with pytest.raises((ValueError,OSError)):run(roots)
    assert not (roots[0]/'.installation-freeze-v1.json').exists()


def test_write_failure_resume(roots,monkeypatch):
    original=barrier._replace_private
    def fail(path,raw):
        if path.name=='checkpoint.json':raise OSError('injected')
        return original(path,raw)
    monkeypatch.setattr(barrier,'_replace_private',fail)
    with pytest.raises(OSError):run(roots)
    assert authority.journal(authority.load_binding(roots[0]))['events'][-1]['state']=='suspended'
    monkeypatch.setattr(barrier,'_replace_private',original)
    assert run(roots)['state']=='both_writers_suspended'


def test_changed_operation_rejected(roots):
    run(roots);before=(roots[3]/'checkpoint.json').read_bytes()
    with pytest.raises(ValueError):barrier.suspend_writers(*roots,'different')
    assert before==(roots[3]/'checkpoint.json').read_bytes()


def test_busy_harness_does_not_suspend_knowledge(roots):
    with InstallationGuard(roots[0]):
        with pytest.raises(AdmissionUnavailable):run(roots)
    assert not (roots[1]/'.workspace-freeze-v1.json').exists()


def test_changed_checkpoint_directory(roots):
    def move(state):
        if state=='harness_suspended':
            roots[3].rename(roots[3].with_name('old-checkpoint'));roots[3].mkdir(mode=0o700)
    with pytest.raises((ValueError,OSError)):run(roots,_after_checkpoint=move)
    assert not (roots[1]/'.workspace-freeze-v1.json').exists()


def test_parent_death_keeps_child_barrier_lease(roots):
    import sys
    root=roots[0].parent;wrapper=root/'knowledge-wrapper'
    wrapper.write_text('#!'+sys.executable+'\n'+f'''import os,sys,time
from pathlib import Path
root=Path({str(root)!r})
if 'suspend' in sys.argv:
 (root/'delegated-ready').write_text(str(os.getpid()))
 while not (root/'delegated-release').exists():time.sleep(.02)
os.execv({KNOWLEDGE!r},[{KNOWLEDGE!r},*sys.argv[1:]])
''');wrapper.chmod(0o700)
    args=(roots[0],roots[1],wrapper,roots[3])
    code='from harness.writer_barrier import suspend_writers;suspend_writers(*'+repr(tuple(map(str,args)))+',"suspend-pair")'
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    parent=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+15
        while not (root/'delegated-ready').exists():
            assert parent.poll() is None and time.monotonic()<deadline;time.sleep(.02)
        parent.kill();parent.wait(timeout=5)
        with pytest.raises(BlockingIOError):run(args)
    finally:
        (root/'delegated-release').write_text('go')
        if parent.poll() is None:parent.kill();parent.wait(timeout=5)
    deadline=time.monotonic()+15
    while True:
        try:result=run(args);break
        except BlockingIOError:
            assert time.monotonic()<deadline;time.sleep(.02)
    assert result['state']=='both_writers_suspended'


@pytest.mark.parametrize('mode',['extra','wrong_journal','duplicate','activation'])
def test_successful_child_with_invalid_receipt_cannot_suspend_harness(roots,monkeypatch,mode):
    original=barrier._delegate
    def alter(*args,**kwargs):
        raw=original(*args,**kwargs);value=json.loads(raw)
        if mode=='extra':value['unexpected']=True
        if mode=='wrong_journal':value['journal']['events'][0]['operation_id']='wrong'
        if mode=='activation':value['installation_cutover_performed']=True
        if mode=='duplicate':return raw.rstrip()[:-1]+b',"status":"ok"}\n'
        return json.dumps(value).encode()
    monkeypatch.setattr(barrier,'_delegate',alter)
    with pytest.raises(ValueError):run(roots)
    assert not (roots[0]/'.installation-freeze-v1.json').exists()


def test_different_prior_suspension_rejected_before_harness_change(roots):
    subprocess.run([KNOWLEDGE,'-m','knowledge_platform.local.writer_authority','suspend','--state-dir',str(roots[1]),'--operation-id','unrelated'],check=True,capture_output=True)
    with pytest.raises(ValueError):run(roots)
    assert not (roots[0]/'.installation-freeze-v1.json').exists()


def test_final_checkpoint_failure_preserves_both_revocations(roots,monkeypatch):
    original=barrier._replace_private
    def fail(path,raw):
        if path.name=='checkpoint.json' and json.loads(raw)['state']=='both_writers_suspended':
            raise OSError('injected final checkpoint failure')
        return original(path,raw)
    monkeypatch.setattr(barrier,'_replace_private',fail)
    with pytest.raises(OSError):run(roots)
    assert json.loads((roots[3]/'checkpoint.json').read_text())['state']=='harness_suspended'
    committed=json.loads((roots[1].parent/'knowledge-authority/journal.json').read_text())
    assert committed['events'][-1]['state']=='suspended'
    monkeypatch.setattr(barrier,'_replace_private',original)
    assert run(roots)['journals']['knowledge']==committed


def test_same_version_release_change_preserves_completed_checkpoint(roots,monkeypatch):
    original=barrier._installed_knowledge_identity
    run(roots);before=(roots[3]/'checkpoint.json').read_bytes()
    def changed(*args,**kwargs):return dict(original(*args,**kwargs),inventory_sha256='sha256:'+'b'*64)
    monkeypatch.setattr(barrier,'_installed_knowledge_identity',changed)
    with pytest.raises(ValueError,match='plan changed'):run(roots)
    assert (roots[3]/'checkpoint.json').read_bytes()==before


def test_release_change_after_partial_freeze_preserves_recovery(roots,monkeypatch):
    original=barrier._installed_knowledge_identity;drift=[False]
    def observe(*args,**kwargs):
        value=original(*args,**kwargs)
        return dict(value,inventory_sha256='sha256:'+'b'*64) if drift[0] else value
    monkeypatch.setattr(barrier,'_installed_knowledge_identity',observe)
    def change(phase):
        if phase=='harness_suspended':drift[0]=True
    with pytest.raises(ValueError,match='release changed'):run(roots,_after_checkpoint=change)
    assert json.loads((roots[3]/'checkpoint.json').read_text())['state']=='harness_suspended'
    assert (roots[0]/'.installation-freeze-v1.json').exists()
    assert not (roots[1]/'.workspace-freeze-v1.json').exists()
    drift[0]=False
    assert run(roots)['state']=='both_writers_suspended'


def test_release_change_during_knowledge_suspend_cannot_publish_completion(roots,monkeypatch):
    original_identity=barrier._installed_knowledge_identity;original_delegate=barrier._delegate;drift=[False]
    def observe(*args,**kwargs):
        value=original_identity(*args,**kwargs)
        return dict(value,inventory_sha256='sha256:'+'b'*64) if drift[0] else value
    def delegate(command,*args,**kwargs):
        raw=original_delegate(command,*args,**kwargs)
        if 'suspend' in command:drift[0]=True
        return raw
    monkeypatch.setattr(barrier,'_installed_knowledge_identity',observe)
    monkeypatch.setattr(barrier,'_delegate',delegate)
    with pytest.raises(ValueError,match='release changed'):run(roots)
    assert json.loads((roots[3]/'checkpoint.json').read_text())['state']=='harness_suspended'
    assert (roots[1]/'.workspace-freeze-v1.json').exists()
    drift[0]=False;monkeypatch.setattr(barrier,'_delegate',original_delegate)
    assert run(roots)['state']=='both_writers_suspended'
