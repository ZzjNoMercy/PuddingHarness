import json,hashlib,os,select,subprocess,sys
from pathlib import Path
import pytest
from harness.installation_authority import enroll,suspend,assign,thaw,read,journal,load_binding,lock,encoded,digest,BINDING
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


def _manifest(root,state,*,rollback_digest=None,suffix=''):
 value={'format':'agent-knowledge-platform-installation-migration/v1',
  'source':{'installation_id':'inst-1'+suffix,'schema_revision':'rev-1','catalog_revision':'rev-1'},
  'targets':{'puddingharness':'puddingharness-backend@0.1.0'},
  'object_summaries':[{'domain':'session_harness','object_count':1,'source_digest':'sha256:'+'a'*64}],
  'id_resource_mappings':[],'credential_rebinds':[],
  'active_writers':{'session_harness':'puddingclaw','knowledge_catalog':'puddingclaw','connector_jobs':'puddingclaw'},
  'checkpoint':{'stage':'prepared'},'rollback_strategy':'no_write_until_finalized','state':state,'rollback_window_open':True}
 if rollback_digest is not None:value['rollback_evidence_digest']='sha256:'+rollback_digest
 path=root/f'manifest-{state.lower()}{suffix}.json'
 path.write_bytes((json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode());path.chmod(0o600)
 return path

def _evidence(root,name='reverse-evidence.json'):
 path=root/name;path.write_bytes(('{"reverse":"candidate","name":'+json.dumps(name)+'}\n').encode());path.chmod(0o600)
 return path,hashlib.sha256(path.read_bytes()).hexdigest()

def _pointer(home,head):
 value={'format':'puddingharness-active-installation/v1','operation_id':head['operation_id'],
  'cutover_manifest_sha256':'a'*64,'prepared_manifest_sha256':head['migration_manifest_sha256'],
  'source_home_identity':'b'*64,'source_freeze_receipt_sha256':'sha256:'+'c'*64,
  'active_installation_revision':head['active_installation_revision'],
  'harness_assigned_event_sha256':head['sha256'],'knowledge_assigned_event_sha256':'d'*64,
  'active_writers':{'session_harness':'puddingharness','knowledge_catalog':'puddingknowledge','connector_jobs':'puddingknowledge'}}
 path=home/'active-installation.json';path.write_bytes(encoded(value));path.chmod(0o600);return path

def test_assign_and_thaw_restores_self_writes(roots):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 manifest=_manifest(home.parent,'PREPARED')
 result=assign(home,manifest,'op-1','puddingharness')
 assert result==assign(home,manifest,'op-1','puddingharness')
 head=result['events'][-1]
 _pointer(home,head)
 assert head['revision']==2 and head['state']=='assigned' and head['writer']=='puddingharness'
 assert head['freeze_receipt_sha256']==result['events'][1]['freeze_receipt_sha256']
 assert head['rollback_evidence_sha256'] is None
 with pytest.raises(AdmissionUnavailable):InstallationGuard(home).acquire()
 assert node(home,'console.log("BAD")').returncode!=0
 report=thaw(home,manifest,'op-1')
 assert report==thaw(home,manifest,'op-1') and report['activation_allowed'] is True
 assert not (home/'.installation-freeze-v1.json').exists()
 retired=authority/'freeze-marker-rev2.json';receipt=authority/'thaw-receipt-rev2.json'
 assert hashlib.sha256(retired.read_bytes()).hexdigest()==head['freeze_receipt_sha256']
 assert read(receipt)['revision_sha256']==head['sha256'] and report['journal']==journal(load_binding(home))
 with InstallationGuard(home) as guard:assert guard.authority_fd is not None
 result=node(home,'console.log("accepted")');assert result.returncode==0,result.stderr
 assert result.stdout.strip()=='accepted'
 (home/'active-installation.json').unlink()
 with pytest.raises((ValueError,FileNotFoundError)):InstallationGuard(home).acquire()
 denied=node(home,'console.log("BAD")');assert denied.returncode!=0 and 'BAD' not in denied.stdout

def test_assign_requires_suspended_authority(roots):
 home,authority=roots;enroll(home,authority,'op-1')
 manifest=_manifest(home.parent,'PREPARED')
 with pytest.raises(ValueError,match='not suspended'):assign(home,manifest,'op-1','puddingharness')
 with pytest.raises(ValueError,match='not assigned'):thaw(home,manifest,'op-1')
 assert len(journal(load_binding(home))['events'])==1

def test_assign_and_thaw_bind_the_suspension_operation(roots):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 manifest=_manifest(home.parent,'PREPARED')
 with pytest.raises(ValueError,match='operation'):assign(home,manifest,'other','puddingharness')
 assign(home,manifest,'op-1','puddingharness')
 with pytest.raises(ValueError,match='operation'):thaw(home,manifest,'other')
 with pytest.raises(ValueError,match='assignment changed'):assign(home,_manifest(home.parent,'PREPARED',suffix='-2'),'op-1','puddingharness')
 assert thaw(home,manifest,'op-1')['activation_allowed'] is True

@pytest.mark.parametrize('state',['DISCOVERED','CUTOVER','ROLLED_BACK','FINALIZED'])
def test_forward_assign_requires_prepared_manifest(roots,state):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 with pytest.raises(ValueError,match='PREPARED'):assign(home,_manifest(home.parent,state),'op-1','puddingharness')
 assert len(journal(load_binding(home))['events'])==2

def test_rollback_assign_requires_rolled_back_manifest_and_matching_evidence(roots):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 evidence,commitment=_evidence(home.parent)
 with pytest.raises(ValueError,match='ROLLED_BACK'):assign(home,_manifest(home.parent,'PREPARED'),'op-1','puddingclaw',rollback_evidence=evidence)
 rolled=_manifest(home.parent,'ROLLED_BACK',rollback_digest=commitment)
 with pytest.raises(ValueError,match='requires rollback evidence'):assign(home,rolled,'op-1','puddingclaw')
 other,_=_evidence(home.parent,'other-evidence.json')
 with pytest.raises(ValueError,match='does not match'):assign(home,rolled,'op-1','puddingclaw',rollback_evidence=other)
 with pytest.raises(ValueError,match='does not match'):assign(home,_manifest(home.parent,'ROLLED_BACK'),'op-1','puddingclaw',rollback_evidence=evidence)
 with pytest.raises(ValueError,match='no rollback evidence'):assign(home,_manifest(home.parent,'PREPARED',suffix='-3'),'op-1','puddingharness',rollback_evidence=evidence)
 assert len(journal(load_binding(home))['events'])==2

def test_rollback_assignment_denies_self_runtime_and_offers_no_thaw(roots):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 forward=_manifest(home.parent,'PREPARED')
 forward_result=assign(home,forward,'op-1','puddingharness');_pointer(home,forward_result['events'][-1]);thaw(home,forward,'op-1')
 with InstallationGuard(home):pass
 suspend(home,'op-2')
 evidence,commitment=_evidence(home.parent)
 rolled=_manifest(home.parent,'ROLLED_BACK',rollback_digest=commitment)
 result=assign(home,rolled,'op-2','puddingclaw',rollback_evidence=evidence)
 assert result==assign(home,rolled,'op-2','puddingclaw',rollback_evidence=evidence)
 head=result['events'][-1]
 assert head['revision']==4 and head['writer']=='puddingclaw' and head['rollback_evidence_sha256']==commitment
 with pytest.raises(AdmissionUnavailable):InstallationGuard(home).acquire()
 with pytest.raises(ValueError,match='not assigned'):thaw(home,rolled,'op-2')
 (home/'.installation-freeze-v1.json').unlink()
 with pytest.raises(ValueError,match='assigned to another product'):InstallationGuard(home).acquire()
 denied=node(home,'console.log("BAD")');assert denied.returncode!=0 and 'BAD' not in denied.stdout

@pytest.mark.parametrize('mode',['repeat_suspend','skip_revision','missing_binding','bad_writer','rollback_without_evidence','forward_with_evidence','receipt_mismatch','bad_revision_pointer','bad_manifest_digest'])
def test_assigned_chain_violations_rejected(roots,mode):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 binding=load_binding(home);current=journal(binding);head=current['events'][-1]
 value={'revision':2,'previous':head['sha256'],'operation_id':'op-1','state':'assigned','writer':'puddingharness',
  'freeze_receipt_sha256':head['freeze_receipt_sha256'],'active_installation_revision':'sha256:'+'b'*64,
  'migration_manifest_sha256':'c'*64,'rollback_evidence_sha256':None}
 if mode=='repeat_suspend':value={'revision':2,'previous':head['sha256'],'operation_id':'op-1','state':'suspended','writer':None,'freeze_receipt_sha256':head['freeze_receipt_sha256']}
 if mode=='skip_revision':value['revision']=3
 if mode=='missing_binding':del value['migration_manifest_sha256']
 if mode=='bad_writer':value['writer']='puddingknowledge'
 if mode=='rollback_without_evidence':value['writer']='puddingclaw'
 if mode=='forward_with_evidence':value['rollback_evidence_sha256']='d'*64
 if mode=='receipt_mismatch':value['freeze_receipt_sha256']='e'*64
 if mode=='bad_revision_pointer':value['active_installation_revision']='b'*64
 if mode=='bad_manifest_digest':value['migration_manifest_sha256']='sha256:'+'c'*64
 crafted=dict(value,sha256=digest(value))
 (authority/'journal.json').write_bytes(encoded(dict(current,events=[*current['events'],crafted])))
 with pytest.raises(ValueError):journal(binding)
 assert node(home,'console.log("BAD")').returncode!=0

def test_thaw_interrupted_after_receipt_resumes_exactly(roots):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 manifest=_manifest(home.parent,'PREPARED');assign(home,manifest,'op-1','puddingharness')
 def stop():raise RuntimeError('injected interruption after receipt')
 with pytest.raises(RuntimeError):thaw(home,manifest,'op-1',_after_receipt=stop)
 assert (authority/'thaw-receipt-rev2.json').exists() and (home/'.installation-freeze-v1.json').exists()
 with pytest.raises(AdmissionUnavailable):InstallationGuard(home).acquire()
 report=thaw(home,manifest,'op-1')
 assert report==thaw(home,manifest,'op-1') and report['activation_allowed'] is True
 assert (authority/'freeze-marker-rev2.json').exists() and not (home/'.installation-freeze-v1.json').exists()

def test_thaw_receipt_write_failure_preserves_freeze(roots,monkeypatch):
 import harness.migration_orchestrator as module
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 manifest=_manifest(home.parent,'PREPARED');assign(home,manifest,'op-1','puddingharness')
 original=module._replace_private
 def fail(*args):raise OSError('injected receipt failure')
 monkeypatch.setattr(module,'_replace_private',fail)
 with pytest.raises(OSError):thaw(home,manifest,'op-1')
 assert (home/'.installation-freeze-v1.json').exists() and not (authority/'thaw-receipt-rev2.json').exists()
 monkeypatch.setattr(module,'_replace_private',original)
 assert thaw(home,manifest,'op-1')['activation_allowed'] is True

@pytest.mark.parametrize('sync_failure',[1,2,3])
def test_thaw_directory_sync_failure_retry_is_exact(roots,monkeypatch,sync_failure):
 import harness.home_freeze as module
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 manifest=_manifest(home.parent,'PREPARED');assign(home,manifest,'op-1','puddingharness')
 original=module._sync_directory;calls=0
 def sync(root):
  nonlocal calls;calls+=1
  if calls==sync_failure:raise OSError('injected directory fsync failure')
  return original(root)
 monkeypatch.setattr(module,'_sync_directory',sync)
 with pytest.raises(OSError):thaw(home,manifest,'op-1')
 monkeypatch.setattr(module,'_sync_directory',original)
 report=thaw(home,manifest,'op-1')
 assert report==thaw(home,manifest,'op-1') and report['activation_allowed'] is True

def test_sigkill_during_thaw_retains_gate_and_marker_then_recovers(roots):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 manifest=_manifest(home.parent,'PREPARED');assign(home,manifest,'op-1','puddingharness')
 code='import time\nfrom harness.installation_authority import thaw\n'
 code+='def hold():\n print("receipt",flush=True)\n time.sleep(60)\n'
 code+='thaw('+repr(str(home))+','+repr(str(manifest))+',"op-1",_after_receipt=hold)'
 env=dict(os.environ,PYTHONPATH=str(BACKEND))
 process=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
 try:
  assert select.select([process.stdout],[],[],15)[0],'child did not become ready'
  assert process.stdout.readline().strip()=='receipt'
 finally:
  process.kill();process.communicate(timeout=15)
 assert (authority/'thaw-receipt-rev2.json').exists() and (home/'.installation-freeze-v1.json').exists()
 with pytest.raises(AdmissionUnavailable):InstallationGuard(home).acquire()
 with pytest.raises(ValueError,match='busy or unresolved'):thaw(home,manifest,'op-1')
 (home/'.installation-cli-admission').rmdir()
 assert thaw(home,manifest,'op-1')['activation_allowed'] is True

def test_thaw_receipt_or_retired_marker_tamper_rejected(roots):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 manifest=_manifest(home.parent,'PREPARED');assign(home,manifest,'op-1','puddingharness');thaw(home,manifest,'op-1')
 receipt=authority/'thaw-receipt-rev2.json';committed=receipt.read_bytes()
 receipt.write_bytes(b'{}\n')
 with pytest.raises(ValueError,match='thaw receipt changed'):thaw(home,manifest,'op-1')
 receipt.write_bytes(committed)
 retired=authority/'freeze-marker-rev2.json';marker=retired.read_bytes()
 retired.write_bytes(b'{}\n')
 with pytest.raises(ValueError,match='Retired freeze marker changed'):thaw(home,manifest,'op-1')
 retired.write_bytes(marker)
 assert thaw(home,manifest,'op-1')['activation_allowed'] is True

def test_thaw_missing_receipt_for_retired_marker_rejected(roots):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 manifest=_manifest(home.parent,'PREPARED');assign(home,manifest,'op-1','puddingharness');thaw(home,manifest,'op-1')
 (authority/'thaw-receipt-rev2.json').unlink()
 with pytest.raises(ValueError,match='receipt missing'):thaw(home,manifest,'op-1')

def test_thaw_rejects_changed_manifest(roots):
 home,authority=roots;enroll(home,authority,'op-1');suspend(home,'op-1')
 manifest=_manifest(home.parent,'PREPARED');assign(home,manifest,'op-1','puddingharness')
 with pytest.raises(ValueError,match='thaw manifest changed'):thaw(home,_manifest(home.parent,'PREPARED',suffix='-2'),'op-1')

def test_cli_assign_status_thaw_fail_closed_reporting(roots):
 home,authority=roots
 env=dict(os.environ,PYTHONPATH=str(BACKEND))
 def run(*args):return subprocess.run([sys.executable,'-m','harness.installation_authority',*args],env=env,capture_output=True,text=True)
 assert run('enroll','--home',str(home),'--authority',str(authority),'--operation-id','op-1').returncode==0
 assert run('suspend','--home',str(home),'--operation-id','op-1').returncode==0
 manifest=_manifest(home.parent,'PREPARED')
 bad=run('assign','--home',str(home),'--operation-id','other','--manifest',str(manifest),'--writer','puddingharness')
 assert bad.returncode==1 and json.loads(bad.stdout)['activation_allowed'] is False
 assert run('assign','--home',str(home),'--operation-id','op-1','--manifest',str(manifest),'--writer','puddingharness').returncode==0
 status=run('status','--home',str(home))
 assert status.returncode==0 and json.loads(status.stdout)['head']=={'revision':2,'state':'assigned','writer':'puddingharness'}
 done=run('thaw','--home',str(home),'--operation-id','op-1','--manifest',str(manifest))
 assert done.returncode==0 and json.loads(done.stdout)['activation_allowed'] is True
