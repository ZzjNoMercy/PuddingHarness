"""Durability and receipt boundary tests; fixture is not Knowledge product E2E."""
import json,os,subprocess,sys,time
from pathlib import Path
import pytest
from harness.target_freeze import freeze_targets
from harness.installation_guard import InstallationGuard,AdmissionUnavailable

@pytest.fixture
def roots(tmp_path):
    root=tmp_path.resolve();home=root/'harness';knowledge=root/'knowledge';stage=root/'checkpoint'
    home.mkdir(mode=0o700);knowledge.mkdir(mode=0o700)
    (knowledge/'workspace.json').write_text('{}\n');(knowledge/'workspace.json').chmod(0o600)
    python=root/'knowledge-fixture'
    python.write_text('#!'+sys.executable+'''\nimport hashlib,json,os,sys,time
from pathlib import Path
root=Path(sys.argv[sys.argv.index('--state-dir')+1]);operation=sys.argv[sys.argv.index('--operation-id')+1]
mode=(root/'mode').read_text() if (root/'mode').exists() else 'ok'
if mode=='fail':sys.exit(1)
if mode=='hold':
 (root/'ready').write_text('ready')
 while not (root/'release').exists():time.sleep(.02)
v={'activation_allowed':False,'directory_identity':{'device':root.stat().st_dev,'inode':root.stat().st_ino},'format':'puddingknowledge-workspace-freeze/v1','operation_id':operation,'state':'workspace_frozen','root_path_sha256':hashlib.sha256(str(root).encode()).hexdigest(),'workspace_manifest_sha256':hashlib.sha256((root/'workspace.json').read_bytes()).hexdigest()}
raw=(json.dumps(v,sort_keys=True,separators=(',',':'))+'\\n').encode()
p=root/'.workspace-freeze-v1.json'
if not p.exists():
 fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.write(fd,raw);os.fsync(fd);os.close(fd)
v.update(status='workspace_frozen',receipt_sha256=hashlib.sha256(raw).hexdigest())
if mode=='wrong_operation':v['operation_id']='other'
if mode=='wrong_hash':v['receipt_sha256']='0'*64
if mode=='extra':v['activation_allowed']=True
if mode=='oversized':v['padding']='x'*1100000
print(json.dumps(v))
''');python.chmod(0o700)
    return home,knowledge,python,stage

def run(roots,**kwargs):return freeze_targets(*roots,'operation-1',**kwargs)

def test_retry(roots):
    result=run(roots);assert result==run(roots)
    assert result['state']=='both_targets_frozen' and not result['activation_allowed'] and not result['rollback_completed']
    assert json.loads((roots[3]/'checkpoint.json').read_text())==result
    with pytest.raises(AdmissionUnavailable):InstallationGuard(roots[0]).acquire()

def test_partial_resume(roots):
    (roots[1]/'mode').write_text('fail')
    with pytest.raises(ValueError):run(roots)
    partial=(roots[3]/'checkpoint.json').read_bytes();assert json.loads(partial)['state']=='harness_frozen'
    with pytest.raises(ValueError):run(roots)
    assert (roots[3]/'checkpoint.json').read_bytes()==partial
    (roots[1]/'mode').unlink();assert run(roots)['state']=='both_targets_frozen'

@pytest.mark.parametrize('mode',['wrong_operation','wrong_hash','extra','oversized'])
def test_bad_receipt(roots,mode):
    (roots[1]/'mode').write_text(mode)
    with pytest.raises(ValueError):run(roots)
    assert json.loads((roots[3]/'checkpoint.json').read_text())['state']=='harness_frozen'

def test_no_downgrade(roots):
    run(roots);before=(roots[3]/'checkpoint.json').read_bytes();(roots[1]/'mode').write_text('fail')
    with pytest.raises(ValueError):run(roots)
    assert (roots[3]/'checkpoint.json').read_bytes()==before

@pytest.mark.parametrize('target',[0,1])
def test_missing_marker(roots,target):
    run(roots);marker=roots[target]/('.installation-freeze-v1.json' if target==0 else '.workspace-freeze-v1.json');marker.unlink()
    with pytest.raises((ValueError,FileNotFoundError)):run(roots)
    assert not marker.exists()

def test_changed_plan(roots):
    run(roots);other=roots[0].parent/'other';other.mkdir(mode=0o700)
    with pytest.raises(ValueError):freeze_targets(other,*roots[1:],'operation-1')
    assert not (other/'.installation-freeze-v1.json').exists()

def test_busy(roots):
    with InstallationGuard(roots[0]):
        with pytest.raises(AdmissionUnavailable):run(roots)
    assert not (roots[1]/'.workspace-freeze-v1.json').exists() and not (roots[3]/'checkpoint.json').exists()

def test_write_failure(roots,monkeypatch):
    import harness.target_freeze as module
    original=module._replace_private
    def fail(path,data):
        if path.name=='checkpoint.json':raise OSError('injected failure')
        return original(path,data)
    monkeypatch.setattr(module,'_replace_private',fail)
    with pytest.raises(OSError):run(roots)
    assert (roots[0]/'.installation-freeze-v1.json').exists()
    monkeypatch.setattr(module,'_replace_private',original);assert run(roots)['state']=='both_targets_frozen'

def test_parent_sigkill(roots):
    home,knowledge,python,stage=roots;(knowledge/'mode').write_text('hold')
    code='from harness.target_freeze import freeze_targets;freeze_targets(*'+repr(tuple(map(str,roots)))+',"operation-1")'
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    parent=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+15
        while not (knowledge/'ready').exists():
            assert parent.poll() is None and time.monotonic()<deadline;time.sleep(.02)
        parent.kill();parent.wait(timeout=5)
        with pytest.raises(BlockingIOError):run(roots)
    finally:
        (knowledge/'release').write_text('go')
        if parent.poll() is None:parent.kill();parent.wait(timeout=5)
    (knowledge/'mode').unlink();deadline=time.monotonic()+10
    while True:
        try:result=run(roots);break
        except BlockingIOError:
            assert time.monotonic()<deadline;time.sleep(.02)
    assert result['state']=='both_targets_frozen'


def test_checkpoint_directory_replacement(roots):
    def replace(state):
        if state=='harness_frozen':
            roots[3].rename(roots[3].with_name('old-checkpoint'));roots[3].mkdir(mode=0o700)
    with pytest.raises((ValueError,FileNotFoundError)):run(roots,_after_checkpoint=replace)
    assert not (roots[3]/'checkpoint.json').exists()
    assert not (roots[1]/'.workspace-freeze-v1.json').exists()


def test_executable_drift(roots):
    (roots[1]/'mode').write_text('fail')
    with pytest.raises(ValueError):run(roots)
    roots[2].write_text(roots[2].read_text()+'\n# changed interpreter\n')
    (roots[1]/'mode').unlink()
    with pytest.raises(ValueError,match='plan changed'):run(roots)
    assert not (roots[1]/'.workspace-freeze-v1.json').exists()
