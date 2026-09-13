"""Real cooperative admission races; all Homes are disposable fixtures."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

fcntl = pytest.importorskip("fcntl", reason="Home freeze requires POSIX flock")

from harness.home_freeze import freeze_home
from harness.installation_guard import AdmissionUnavailable, FREEZE_NAME, InstallationGuard

BACKEND = Path(__file__).resolve().parents[1]
NODE_MODULE = BACKEND.parent / 'packages/puddingclaw-deploy-cli/src/home-admission.js'


def child(code, home):
    env = dict(os.environ, PUDDINGHARNESS_HOME=str(home), PYTHONPATH=str(BACKEND), PYTHONDONTWRITEBYTECODE='1')
    return subprocess.Popen([sys.executable, '-u', '-c', code], cwd=home.parent, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def ready(process):
    assert select.select([process.stdout], [], [], 15)[0], 'child did not become ready'
    line = process.stdout.readline().strip()
    assert line, process.stderr.read() if process.poll() is not None else 'empty child output'
    return line


def stop(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=15)


def test_freeze_idempotency_and_both_writer_modes(tmp_path):
    home = tmp_path.resolve()
    receipt = freeze_home(home, 'freeze-1')
    assert receipt == freeze_home(home, 'freeze-1')
    assert not receipt['activation_allowed']
    assert not receipt['external_writers_fenced']
    for exclusive in (False, True):
        with pytest.raises(AdmissionUnavailable):
            with InstallationGuard(home, exclusive=exclusive):
                pytest.fail('frozen writer admitted')
    with pytest.raises(ValueError):
        freeze_home(home, 'another-operation')


@pytest.mark.parametrize('module', ['app', 'evaluation.worker'])
def test_frozen_import_rejected_before_business_modules(tmp_path, module):
    home = tmp_path.resolve()
    freeze_home(home, 'freeze')
    code = f'''
import sys
class BusinessTrap:
 def find_spec(self, fullname, path=None, target=None):
  if fullname in ('config','evaluation.repository','evaluation.runner'):
   raise AssertionError('business module imported before admission')
sys.meta_path.insert(0,BusinessTrap())
try:
 __import__({module!r})
except Exception as error:
 from harness.installation_guard import AdmissionUnavailable
 assert isinstance(error,AdmissionUnavailable), repr(error)
 print('denied',flush=True)
else: raise AssertionError('admitted')
'''
    process = child(code, home)
    try:
        assert ready(process) == 'denied'
        assert process.wait(timeout=15) == 0
        assert not (home/'state').exists()
    finally:
        stop(process)


def test_live_writer_blocks_freeze(tmp_path):
    home = tmp_path.resolve()
    with InstallationGuard(home):
        with pytest.raises(AdmissionUnavailable):
            freeze_home(home, 'freeze')
    assert freeze_home(home, 'freeze')['state'] == 'home_frozen'


def test_inherited_fd_survives_real_parent_sigkill(tmp_path):
    home = tmp_path.resolve()
    child_code = "import time; time.sleep(60)"
    code = f'''
from harness.installation_guard import admit_backend_process, inherited_guard_fds
import subprocess,sys,time
admit_backend_process()
p=subprocess.Popen([sys.executable,'-c',{child_code!r}],pass_fds=inherited_guard_fds(),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(p.pid,flush=True)
time.sleep(60)
'''
    process = child(code, home)
    descendant = None
    try:
        descendant = int(ready(process))
        stop(process)
        with pytest.raises(AdmissionUnavailable):
            freeze_home(home, 'freeze')
    finally:
        stop(process)
        if descendant:
            os.kill(descendant, 9)
    # flock availability is the observation; avoid assuming kill() means exit.
    import time
    deadline = time.monotonic()+10
    while True:
        try:
            assert freeze_home(home,'freeze')['state'] == 'home_frozen'
            break
        except AdmissionUnavailable:
            if time.monotonic()>deadline: raise
            time.sleep(.02)


@pytest.mark.parametrize('phase', ['part', 'link'])
def test_real_freezer_sigkill_retains_gate_and_denies_writers(tmp_path, phase):
    home = tmp_path.resolve()
    code = f'''
from harness.home_freeze import freeze_home
import time
from pathlib import Path
import os
def hold():
 print('published',flush=True)
 time.sleep(60)
freeze_home(Path(os.environ['PUDDINGHARNESS_HOME']),'freeze',_after_{phase}=hold)
'''
    process = child(code,home)
    try:
        assert ready(process)=='published'
    finally:
        stop(process)
    with pytest.raises(AdmissionUnavailable):
        with InstallationGuard(home): pass
    # Persistent mkdir gate is deliberately not auto-cleaned after death.
    with pytest.raises(ValueError, match='busy or unresolved'):
        freeze_home(home,'freeze')


@pytest.mark.parametrize('phase', ['part', 'link'])
def test_handled_publication_failure_can_retry(tmp_path, phase):
    def fail(): raise RuntimeError('injected interruption')
    with pytest.raises(RuntimeError):
        freeze_home(tmp_path.resolve(),'freeze',**{f'_after_{phase}':fail})
    assert freeze_home(tmp_path.resolve(),'freeze')['state']=='home_frozen'


def test_legacy_backend_lease_and_symlink_state_rejected(tmp_path):
    home=tmp_path.resolve(); state=home/'state'; state.mkdir()
    path=state/'backend.lease'
    with path.open('w') as stream:
        fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError): freeze_home(home,'freeze')
    path.unlink();state.rmdir();state.symlink_to(home)
    with pytest.raises(ValueError): freeze_home(home,'freeze')


@pytest.mark.parametrize('name', [FREEZE_NAME,FREEZE_NAME+'.part'])
def test_corrupt_marker_is_fail_closed(tmp_path,name):
    (tmp_path/name).write_text('broken')
    with pytest.raises(AdmissionUnavailable):
        with InstallationGuard(tmp_path.resolve()): pass
    with pytest.raises(ValueError): freeze_home(tmp_path.resolve(),'freeze')


@pytest.mark.parametrize('kill', [False,True])
def test_actual_node_ticket_blocks_python_freeze(tmp_path,kill):
    home=tmp_path.resolve()
    code=f'''
import {{admitHomeWrite}} from {json.dumps(NODE_MODULE.as_uri())};
await admitHomeWrite({json.dumps(str(home))},'fixture',async()=>{{
 console.log('ticket'); await new Promise(resolve=>process.stdin.once('data',resolve));
}});
'''
    process=subprocess.Popen(['node','--input-type=module','-e',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,cwd=home)
    try:
        assert ready(process)=='ticket'
        with pytest.raises(ValueError,match='active or unresolved'):freeze_home(home,'freeze')
        if kill:
            stop(process)
            with pytest.raises(ValueError,match='active or unresolved'):freeze_home(home,'freeze')
            assert len(list((home/'.installation-cli-leases').iterdir()))==1
        else:
            out,err=process.communicate('complete\n',timeout=15)
            assert process.returncode==0,err
            freeze_home(home,'freeze')
            result=subprocess.run(['node','--input-type=module','-e',code],input='complete\n',capture_output=True,text=True,cwd=home,timeout=15)
            assert result.returncode!=0
            assert 'ticket' not in result.stdout
    finally:
        stop(process)


@pytest.mark.parametrize('sync_failure', [2,3,4])
def test_directory_sync_failure_never_acknowledges_and_retry_is_exact(tmp_path,monkeypatch,sync_failure):
    import harness.home_freeze as module
    original=module._sync_directory
    calls=0
    def sync(root):
        nonlocal calls
        calls+=1
        if calls==sync_failure:raise OSError('injected directory fsync failure')
        return original(root)
    monkeypatch.setattr(module,'_sync_directory',sync)
    with pytest.raises(OSError): freeze_home(tmp_path.resolve(),'freeze')
    monkeypatch.setattr(module,'_sync_directory',original)
    with pytest.raises(AdmissionUnavailable):
        with InstallationGuard(tmp_path.resolve()):pass
    assert freeze_home(tmp_path.resolve(),'freeze')['state']=='home_frozen'


@pytest.mark.parametrize('args', [['start'],['stop'],['restart'],['config','set','backend.port','9999'],['profile','apply','harness'],['runtime','install'],['database','configure'],['agent','run','fixture']])
def test_real_cli_write_commands_refuse_frozen_home(tmp_path,args):
    home=tmp_path.resolve();home.chmod(0o700)
    freeze_home(home,'freeze')
    before={p.name:p.read_bytes() for p in home.iterdir() if p.is_file()}
    result=subprocess.run(['node',str(NODE_MODULE.with_name('cli.js')),*args,'--json'],capture_output=True,text=True,cwd=home,env=dict(os.environ,PUDDINGHARNESS_HOME=str(home)),timeout=15)
    assert result.returncode!=0
    assert 'installation_frozen' in result.stdout+result.stderr
    assert before=={p.name:p.read_bytes() for p in home.iterdir() if p.is_file()}
    assert not (home/'.installation-cli-leases').exists()


def test_python_created_home_admits_node_writer(tmp_path):
    home=tmp_path.resolve()/'new-home'
    with InstallationGuard(home):
        assert home.stat().st_mode & 0o777 == 0o700
        code=f"import {{admitHomeWrite}} from {json.dumps(NODE_MODULE.as_uri())}; await admitHomeWrite({json.dumps(str(home))},'fixture',async()=>console.log('admitted'));"
        result=subprocess.run(['node','--input-type=module','-e',code],capture_output=True,text=True,timeout=15)
        assert result.returncode==0,result.stderr
        assert result.stdout.strip()=='admitted'
    freeze_home(home,'freeze')
