import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from harness.settings_import import prepare_settings_import, project_settings


def source_at(root):
    root.mkdir()
    value = {'schema_version': 1, 'cache': {'enabled': False},
             'harness': {'model_call_limit': {'run_limit': 17}},
             'subagents': {'custom': {'enabled': False, 'system_prompt': 'private prompt'}},
             'database': {'url': 'postgres://secret@old/writer'},
             'mcp': {'servers': {'secret-name': {'command': 'old-writer'}}},
             'knowledge': {'secret': 'do not transfer'}, 'user-secret-key': 'private'}
    (root / 'config.json').write_text(json.dumps(value))
    return (root / 'config.json').read_bytes()


def test_project_resume_and_actual_runtime_read(tmp_path, monkeypatch):
    source = tmp_path / 'source'; raw = source_at(source); stage = tmp_path / 'stage'
    def stop(): raise RuntimeError('injected crash')
    with pytest.raises(RuntimeError): prepare_settings_import(source, stage, _after_copy=stop)
    assert json.loads((stage / 'manifest.json').read_bytes())['state'] == 'copying'
    result = prepare_settings_import(source, stage)
    assert result['credential_rebind_required'] and result['untransferred_section_count'] == 4
    assert not result['activation_allowed'] and not result['writer_fence_verified']
    assert prepare_settings_import(source, stage)['idempotent']
    assert (source / 'config.json').read_bytes() == raw
    import config
    monkeypatch.setattr(config, 'CONFIG_FILE', stage / 'config.json')
    actual = config.load_config()
    assert actual['cache']['enabled'] is False
    assert actual['harness']['model_call_limit']['run_limit'] == 17
    assert actual['database']['provider'] == 'sqlite' and not actual['database']['url']
    assert actual['mcp'] == {'enabled': [], 'servers': {}}
    assert actual['subagents']['custom']['system_prompt'] == 'private prompt'
    manifest = (stage / 'manifest.json').read_text()
    for secret in ['private prompt', 'secret-name', 'user-secret-key', 'postgres://', 'do not transfer']:
        assert secret not in manifest
    assert (stage / 'config.json').stat().st_mode & 0o077 == 0


@pytest.mark.parametrize('raw', [b'{"cache":{},"cache":{}}', b'{"schema_version":true}',
    b'{"schema_version":2}', b'{"cache":null}', b'{"cache":{"x":NaN}}',
    b'{"compression":{"ratio":0.8}}', b'{"harness":{"terminal":{"docker_enabled":true}}}'])
def test_invalid_source_rejected(raw):
    with pytest.raises(ValueError): project_settings(raw)


@pytest.mark.parametrize('mutation', ['source', 'payload', 'manifest', 'missing'])
def test_tampering_does_not_resume(tmp_path, mutation):
    source=tmp_path/'source';source_at(source);stage=tmp_path/'stage'
    prepare_settings_import(source,stage)
    if mutation=='source': (source/'config.json').write_text('{}')
    elif mutation=='payload': (stage/'config.json').write_text('{}')
    elif mutation=='missing': (stage/'config.json').unlink()
    else:
        value=json.loads((stage/'manifest.json').read_text());value['activation_allowed']=0
        (stage/'manifest.json').write_text(json.dumps(value))
    with pytest.raises(ValueError): prepare_settings_import(source,stage)


def test_change_during_copy_rejected(tmp_path):
    source=tmp_path/'source';source_at(source)
    def change(): (source/'config.json').write_text('{}')
    with pytest.raises(ValueError): prepare_settings_import(source,tmp_path/'stage',_after_copy=change)


def test_links_and_overlap_rejected(tmp_path):
    source=tmp_path/'source';source_at(source)
    with pytest.raises(ValueError): prepare_settings_import(source,source/'stage')
    other=tmp_path/'linked';other.mkdir();os.link(source/'config.json',other/'config.json')
    with pytest.raises(ValueError): prepare_settings_import(other,tmp_path/'stage')


def test_cli_hides_sensitive_values_and_paths(tmp_path):
    source=tmp_path/'source';source_at(source)
    env=dict(os.environ)
    if env.get('HARNESS_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    p=subprocess.run([sys.executable,'-m','harness.settings_import','--source-snapshot',str(source),'--staging',str(tmp_path/'stage')],env=env,cwd=tmp_path,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    assert json.loads(p.stdout)['state']=='verified_inactive'
    assert str(source) not in p.stdout and 'secret' not in p.stdout and 'private prompt' not in p.stdout


def test_sigkill_checkpoint_releases_lock_and_resumes(tmp_path):
    import time
    source=tmp_path/'source';source_at(source);stage=tmp_path/'stage';marker=tmp_path/'copied'
    env=dict(os.environ)
    if env.get('HARNESS_TEST_INSTALLED')=='1':env.pop('PYTHONPATH',None)
    else:env['PYTHONPATH']=str(Path(__file__).parents[1])
    program='''
import sys,time
from pathlib import Path
from harness.settings_import import prepare_settings_import
def pause():
    Path(sys.argv[3]).write_text('copied')
    time.sleep(30)
prepare_settings_import(sys.argv[1],sys.argv[2],_after_copy=pause)
'''
    child=subprocess.Popen([sys.executable,'-c',program,str(source),str(stage),str(marker)],env=env,cwd=tmp_path,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not marker.exists() and child.poll() is None and time.monotonic()<deadline: time.sleep(.02)
        assert marker.exists()
        child.kill();child.wait(timeout=5)
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
    assert json.loads((stage/'manifest.json').read_bytes())['state']=='copying'
    assert prepare_settings_import(source,stage)['state']=='verified_inactive'
    assert prepare_settings_import(source,stage)['idempotent']
