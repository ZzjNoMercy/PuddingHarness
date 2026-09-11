import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from harness.home_import import prepare_home_import


def fixture(root):
    (root/'sessions').mkdir(parents=True)
    (root/'sessions/s.json').write_text(json.dumps({'title':'History','created_at':1,'updated_at':1,'messages':[{'role':'user','content':'preserved'}]}))
    (root/'config.json').write_text(json.dumps({'cache':{'enabled':False},'mcp':{'enabled':['old']},'database':{'url':'postgres://private@old/writer'}}))
    (root/'knowledge').mkdir();(root/'knowledge/private').write_text('not owned')


def test_combined_checkpoint_and_real_readers(tmp_path, monkeypatch):
    source=tmp_path/'snapshot';fixture(source);stage=tmp_path/'stage'
    result=prepare_home_import(source,stage)
    assert result['session_file_count']==1 and result['file_count']==2
    assert prepare_home_import(source,stage)['idempotent']
    assert not (stage/'payload/knowledge').exists()
    import config
    monkeypatch.setattr(config,'CONFIG_FILE',stage/'payload/config.json')
    assert config.load_config()['cache']['enabled'] is False
    assert config.load_config()['database']['url']==''
    from graph.session_manager import SessionManager
    manager=SessionManager();manager.initialize(sessions_dir=stage/'payload/sessions')
    assert manager.load_session('s')[0]['content']=='preserved'


@pytest.mark.parametrize('domain',['sessions','config'])
def test_change_between_domains_prevents_completion(tmp_path,domain):
    source=tmp_path/'snapshot';fixture(source);stage=tmp_path/'stage'
    def change(relative):
        if relative=='config.json':
            path=source/('sessions/s.json' if domain=='sessions' else 'config.json')
            path.write_text('{}')
    with pytest.raises(ValueError):prepare_home_import(source,stage,_after_copy=change)
    assert json.loads((stage/'manifest.json').read_text())['state']=='copying'
    with pytest.raises(ValueError):prepare_home_import(source,stage)


def test_empty_install_and_later_config_creation_rejected(tmp_path):
    source=tmp_path/'source';source.mkdir();stage=tmp_path/'stage'
    result=prepare_home_import(source,stage)
    assert result['session_file_count']==0
    assert json.loads((stage/'payload/config.json').read_text())=={'schema_version':1}
    (source/'config.json').write_text('{}')
    with pytest.raises(ValueError):prepare_home_import(source,stage)


@pytest.mark.parametrize('mode',['payload','manifest','extra','empty_directory'])
def test_completed_tampering_rejected(tmp_path,mode):
    source=tmp_path/'snapshot';fixture(source);stage=tmp_path/'stage';prepare_home_import(source,stage)
    if mode=='payload':(stage/'payload/config.json').unlink()
    elif mode=='extra':(stage/'payload/unowned').write_text('x')
    elif mode=='empty_directory':(stage/'payload/unowned').mkdir()
    else:
        value=json.loads((stage/'manifest.json').read_text());value['writer_fence_verified']=0
        (stage/'manifest.json').write_text(json.dumps(value))
    with pytest.raises(ValueError):prepare_home_import(source,stage)


def test_actual_kill_between_domains_resumes_installed_cli(tmp_path):
    source=tmp_path/'snapshot';fixture(source);stage=tmp_path/'stage';marker=tmp_path/'copied'
    env=dict(os.environ)
    if env.get('HARNESS_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    code='''
import sys,time
from pathlib import Path
from harness.home_import import prepare_home_import
def pause(relative):
    if relative.startswith('sessions/'):
        Path(sys.argv[3]).write_text('session copied')
        time.sleep(30)
prepare_home_import(sys.argv[1],sys.argv[2],_after_copy=pause)
'''
    child=subprocess.Popen([sys.executable,'-c',code,str(source),str(stage),str(marker)],cwd=tmp_path,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.02)
        assert marker.exists()
        child.kill();child.wait(timeout=5)
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
    assert not (stage/'payload/config.json').exists()
    command=[sys.executable,'-m','harness.home_import','--source-snapshot',str(source),'--staging',str(stage)]
    for complete in [False,True]:
        result=subprocess.run(command,cwd=tmp_path,env=env,capture_output=True,text=True,check=True)
        report=json.loads(result.stdout)
        assert report['idempotent'] is complete and report['activation_allowed'] is False
        assert str(source) not in result.stdout and 'private' not in result.stdout
    assert (stage/'payload/config.json').exists()
