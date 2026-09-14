import json,os,subprocess,sys
from pathlib import Path
import pytest
from harness.installation_authority import enroll,suspend,read,journal,load_binding,lock,BINDING
from harness.installation_guard import InstallationGuard,AdmissionUnavailable
BACKEND=Path(__file__).resolve().parents[1]
NODE=BACKEND.parent/'packages/puddingclaw-deploy-cli/src/home-admission.js'

@pytest.fixture
def roots(tmp_path):
 root=tmp_path.resolve();home=root/'home';home.mkdir(mode=0o700);return home,root/'authority'

def node(home,callback):
 code='import { admitHomeWrite } from '+json.dumps(NODE.as_uri())+';await admitHomeWrite('+json.dumps(str(home))+',"fixture",async()=>{'+callback+'});'
 return subprocess.run(['node','--input-type=module','-e',code],capture_output=True,text=True)

def test_enroll_and_both_runtime_admissions(roots):
 home,authority=roots;first=enroll(home,authority,'enroll-1');assert enroll(home,authority,'enroll-1')==first
 with InstallationGuard(home) as guard:
  assert guard.authority_fd is not None
  with pytest.raises(BlockingIOError):
   with lock(authority,exclusive=True):pass
 result=node(home,'console.log("accepted")');assert result.returncode==0,result.stderr
 assert result.stdout.strip()=='accepted'

def test_running_writer_blocks_suspend(roots):
 home,authority=roots;enroll(home,authority,'enroll-1')
 with InstallationGuard(home):
  with pytest.raises(AdmissionUnavailable):suspend(home,'suspend-1')
 assert len(journal(load_binding(home))['events'])==1
 assert not (home/'.installation-freeze-v1.json').exists()

def test_suspend_exact_retry_and_denial(roots):
 home,authority=roots;enroll(home,authority,'enroll-1');result=suspend(home,'suspend-1');assert result==suspend(home,'suspend-1')
 assert result['events'][-1]['writer'] is None
 with pytest.raises(AdmissionUnavailable):InstallationGuard(home).acquire()
 assert node(home,'throw new Error("callback ran")').returncode!=0
 with pytest.raises(ValueError):suspend(home,'different')

@pytest.mark.parametrize('mode',['missing_journal','corrupt_journal','binding_part','moved_authority'])
def test_authority_failure_denies_python_and_node(roots,mode):
 home,authority=roots;enroll(home,authority,'enroll-1')
 if mode=='missing_journal':(authority/'journal.json').unlink()
 if mode=='corrupt_journal':(authority/'journal.json').write_text('{}\n')
 if mode=='binding_part':(home/(BINDING+'.part')).write_text('broken')
 if mode=='moved_authority':authority.rename(authority.with_name('old'));authority.mkdir(mode=0o700)
 with pytest.raises((ValueError,OSError)):InstallationGuard(home).acquire()
 result=node(home,'console.log("BAD")');assert result.returncode!=0 and 'BAD' not in result.stdout

def test_suspended_revision_denies_even_if_freeze_marker_removed(roots):
 home,authority=roots;enroll(home,authority,'enroll-1');suspend(home,'suspend-1');(home/'.installation-freeze-v1.json').unlink()
 with pytest.raises(ValueError,match='no active'):InstallationGuard(home).acquire()
 assert node(home,'console.log("BAD")').returncode!=0
 with pytest.raises(FileNotFoundError):suspend(home,'suspend-1')
 assert not (home/'.installation-freeze-v1.json').exists()

def test_other_home_cannot_rebind_authority(roots):
 home,authority=roots;enroll(home,authority,'enroll-1');other=home.parent/'other';other.mkdir(mode=0o700)
 with pytest.raises(ValueError):enroll(other,authority,'enroll-1')
 assert not (other/BINDING).exists()

def test_cli_ticket_blocks_enrollment(roots):
 home,authority=roots;leases=home/'.installation-cli-leases';leases.mkdir(mode=0o700);(leases/'unsettled').write_text('pending')
 with pytest.raises(ValueError):enroll(home,authority,'enroll-1')
 assert not authority.exists() and not (home/BINDING).exists()

def test_inherited_authority_lease_survives_guard_close(roots):
 home,authority=roots;enroll(home,authority,'enroll-1');guard=InstallationGuard(home).acquire()
 child=subprocess.Popen([sys.executable,'-c','import sys;print("ready",flush=True);sys.stdin.readline()'],pass_fds=(guard.authority_fd,),stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
 try:
  assert child.stdout.readline().strip()=='ready';guard.close()
  with pytest.raises(BlockingIOError):
   with lock(authority,exclusive=True):pass
 finally:child.communicate('\n',timeout=10);guard.close()
 with lock(authority,exclusive=True):pass

def test_suspension_checkpoint_failure_preserves_freeze(roots,monkeypatch):
 import harness.migration_orchestrator as module
 home,authority=roots;enroll(home,authority,'enroll-1');original=module._replace_private
 def fail(*args):raise OSError('injected checkpoint failure')
 monkeypatch.setattr(module,'_replace_private',fail)
 with pytest.raises(OSError):suspend(home,'suspend-1')
 assert (home/'.installation-freeze-v1.json').exists() and len(journal(load_binding(home))['events'])==1
 monkeypatch.setattr(module,'_replace_private',original);assert suspend(home,'suspend-1')['events'][-1]['state']=='suspended'


def test_admitted_home_environment_drift_rejected(roots):
 home,authority=roots;enroll(home,authority,'enroll-1')
 code="""
from harness.installation_guard import admit_backend_process,AdmissionUnavailable
admit_backend_process()
import os
from pathlib import Path
from runtime_identity.paths import PuddingClawPaths
import config
assert PuddingClawPaths.from_environment().root==Path(os.environ['PUDDINGHARNESS_HOME'])
os.environ['PUDDINGHARNESS_HOME']=str(Path(os.environ['PUDDINGHARNESS_HOME']).parent/'other')
for resolve in (PuddingClawPaths.from_environment,config._config_path):
 try:resolve()
 except AdmissionUnavailable:pass
 else:raise AssertionError('admitted Home drifted')
"""
 env=dict(os.environ,PUDDINGHARNESS_HOME=str(home),PYTHONPATH=str(BACKEND))
 result=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,text=True)
 assert result.returncode==0,result.stderr
 assert not (home.parent/'other').exists()


def test_live_node_callback_blocks_authority_suspension(roots):
 home,authority=roots;enroll(home,authority,'enroll-1')
 code='import { admitHomeWrite } from '+json.dumps(NODE.as_uri())+';await admitHomeWrite('+json.dumps(str(home))+',"held",async()=>{console.log("held");await new Promise(resolve=>process.stdin.once("data",resolve));});process.stdin.pause();'
 child=subprocess.Popen(['node','--input-type=module','-e',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 try:
  import select
  assert select.select([child.stdout],[],[],10)[0]
  assert child.stdout.readline().strip()=='held'
  ticket=json.loads(next((home/'.installation-cli-leases').iterdir()).read_text())
  current=journal(load_binding(home))
  assert ticket['writer_authority']=={'binding_sha256':current['binding_sha256'],'revision':0,'revision_sha256':current['events'][0]['sha256']}
  with pytest.raises(ValueError):suspend(home,'suspend-1')
  assert len(journal(load_binding(home))['events'])==1
 finally:
  out,err=child.communicate('done\n',timeout=10)
  assert child.returncode==0,err
 assert suspend(home,'suspend-1')['events'][-1]['state']=='suspended'


def test_exclusive_authority_refuses_python_without_leaking_home_lock(roots):
 home,authority=roots;enroll(home,authority,'enroll-1')
 with lock(authority,exclusive=True):
  with pytest.raises(BlockingIOError):InstallationGuard(home).acquire()
  # The failed acquisition released Home admission; no permanent wait exists.
  with InstallationGuard(home,exclusive=True,allow_frozen=True):pass
 with InstallationGuard(home):pass


def test_legacy_public_home_rejected_without_permission_repair(roots):
 home,authority=roots;home.chmod(0o755)
 with pytest.raises(ValueError):enroll(home,authority,'enroll-1')
 assert home.stat().st_mode&0o777==0o755
 assert not authority.exists() and not (home/BINDING).exists()
