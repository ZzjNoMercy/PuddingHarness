import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from harness.session_import import prepare_session_import, inventory


def fixture(root):
    (root/'sessions').mkdir(parents=True)
    (root/'sessions'/'s.json').write_text(json.dumps({'title':'History','created_at':1,'updated_at':1,'messages':[{'role':'user','content':'preserve opaque secret=example'}], 'analytics_model_id':'legacy-selector'}))
    (root/'sessions'/'traces').mkdir();(root/'sessions'/'traces'/'s.json').write_text('{"traces":{}}')
    (root/'data/attachments/s/a').mkdir(parents=True);(root/'data/attachments/s/a/image.bin').write_bytes(b'attachment')
    (root/'knowledge').mkdir();(root/'knowledge'/'source.txt').write_text('not session owned')
    (root/'config.json').write_text('{"secret":"not imported"}')


def test_resume_after_copy_failure_and_real_session_read(tmp_path):
    source=tmp_path/'source';fixture(source);before=inventory(source);stage=tmp_path/'stage'
    def fail(_):raise RuntimeError('injected stop')
    with pytest.raises(RuntimeError):prepare_session_import(source,stage,_after_copy=fail)
    assert json.loads((stage/'manifest.json').read_text())['state']=='copying'
    result=prepare_session_import(source,stage)
    assert result['state']=='verified_inactive' and result['activation_allowed'] is False
    assert result['idempotent'] is False
    assert prepare_session_import(source,stage)['idempotent'] is True
    assert inventory(source)==before
    assert (stage/'payload/sessions/s.json').read_bytes()==(source/'sessions/s.json').read_bytes()
    assert not (stage/'payload/knowledge').exists() and not (stage/'payload/config.json').exists()
    from graph.session_manager import SessionManager
    manager=SessionManager();manager.initialize(sessions_dir=stage/'payload/sessions')
    messages=manager.load_session('s')
    assert messages[0]['content']=='preserve opaque secret=example'
    assert inventory(source)==before


def test_source_change_and_target_tampering_rejected(tmp_path):
    source=tmp_path/'source';fixture(source);stage=tmp_path/'stage'
    prepare_session_import(source,stage)
    (stage/'payload/sessions/s.json').write_text('{}')
    with pytest.raises(ValueError,match='integrity'):prepare_session_import(source,stage)
    (source/'sessions/s.json').write_text('{}')
    with pytest.raises(ValueError,match='plan changed'):prepare_session_import(source,stage)


def test_symlink_overlap_and_foreign_stage_rejected(tmp_path):
    source=tmp_path/'source';fixture(source)
    with pytest.raises(ValueError):prepare_session_import(source,source/'child')
    stage=tmp_path/'stage';stage.mkdir(mode=0o700);(stage/'foreign').write_text('keep')
    with pytest.raises(ValueError):prepare_session_import(source,stage)
    assert (stage/'foreign').read_text()=='keep'
    (source/'sessions/link').symlink_to(source/'config.json')
    with pytest.raises(ValueError):prepare_session_import(source,tmp_path/'other')


def test_source_update_during_copy_leaves_unverified_stage(tmp_path):
    source=tmp_path/'source';fixture(source);stage=tmp_path/'stage'
    def mutate(_):(source/'sessions/s.json').write_text('{}')
    with pytest.raises(ValueError):prepare_session_import(source,stage,_after_copy=mutate)
    assert json.loads((stage/'manifest.json').read_text())['state']=='copying'


def test_cli_is_independent_and_report_does_not_include_content(tmp_path):
    source=tmp_path/'source';fixture(source)
    env=dict(os.environ)
    if os.environ.get('HARNESS_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    value=subprocess.run([sys.executable,'-m','harness.session_import','--source-snapshot',str(source),
        '--staging',str(tmp_path/'stage')],env=env,cwd=tmp_path,capture_output=True,text=True,timeout=20)
    assert value.returncode==0,value.stderr+value.stdout
    report=json.loads(value.stdout);assert report['state']=='verified_inactive'
    assert report['settings_migrated'] is False
    assert 'secret=example' not in value.stdout and str(source) not in value.stdout


@pytest.mark.parametrize('key',['activation_allowed','writer_fence_verified','settings_migrated'])
def test_forged_manifest_cannot_promote_inactive_staging(tmp_path,key):
    source=tmp_path/'source';fixture(source);stage=tmp_path/'stage'
    prepare_session_import(source,stage)
    path=stage/'manifest.json';manifest=json.loads(path.read_text());manifest[key]=True
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='plan changed'):prepare_session_import(source,stage)


def test_sigkill_releases_import_lock_and_can_resume(tmp_path):
    import time
    source=tmp_path/'source';fixture(source);stage=tmp_path/'stage';marker=tmp_path/'paused'
    env=dict(os.environ)
    if os.environ.get('HARNESS_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    program='''
import sys,time
from pathlib import Path
from harness.session_import import prepare_session_import
def pause(_):
    Path(sys.argv[3]).write_text('copied')
    time.sleep(30)
prepare_session_import(Path(sys.argv[1]),Path(sys.argv[2]),_after_copy=pause)
'''
    process=subprocess.Popen([sys.executable,'-c',program,str(source),str(stage),str(marker)],
        env=env,cwd=tmp_path,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and process.poll() is None and time.monotonic()<deadline:time.sleep(.03)
        assert marker.exists()
        process.kill();process.wait(timeout=5)
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)
    assert json.loads((stage/'manifest.json').read_text())['state']=='copying'
    result=prepare_session_import(source,stage)
    assert result['state']=='verified_inactive'
    assert prepare_session_import(source,stage)['idempotent'] is True
