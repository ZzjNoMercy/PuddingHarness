"""Product/protocol identity must precede an installed CLI status."""
import json
from pathlib import Path
import subprocess

import pytest
import cli_runtime


def payload(**changes):
    return {'cli':'puddingharness','agent_id':'puddingharness','schema_version':'1',
        'protocol_version':'1','cli_version':cli_runtime.CLI_VERSION,**changes}


def runner(output, returncode=0):
    def run(command, **kwargs):
        if command[1:] == ['--version']:
            return subprocess.CompletedProcess(command,0,'v24.0.0' if 'node' in command[0] else '11.0.0','')
        return subprocess.CompletedProcess(command,returncode,output,'')
    return run


@pytest.mark.parametrize('response', ['', '{}', '0.1.19', 'null', json.dumps(payload(cli='puddingclaw')),
    json.dumps(payload(cli_version='nonsense')),json.dumps(payload(protocol_version='2')),
    json.dumps(payload(schema_version=1)),json.dumps(payload(agent_id='puddingclaw'))])
def test_successful_process_without_harness_handshake_is_not_installed(tmp_path,monkeypatch,response):
    monkeypatch.setattr(cli_runtime.shutil,'which',lambda name: '/fake/'+name)
    state=cli_runtime.detect_cli_runtime(tmp_path,runner=runner(response))
    assert state['installed'] is False


def test_real_cli_shape_and_wrong_version_status(tmp_path,monkeypatch):
    names=[]
    def which(name):names.append(name);return '/fake/'+name
    monkeypatch.setattr(cli_runtime.shutil,'which',which)
    assert cli_runtime.detect_cli_runtime(tmp_path,runner=runner(json.dumps(payload())))['installed']
    wrong=cli_runtime.detect_cli_runtime(tmp_path,runner=runner(json.dumps(payload(cli_version='0.1.18'))))
    assert not wrong['installed'] and wrong['version_mismatch'] and wrong['identity_verified']
    assert 'puddingclaw' not in names


def test_stderr_and_nonzero_are_not_installation_evidence(tmp_path,monkeypatch):
    monkeypatch.setattr(cli_runtime.shutil,'which',lambda name:'/fake/'+name)
    assert not cli_runtime.detect_cli_runtime(tmp_path,runner=runner(json.dumps(payload()),1))['installed']
    def stderr(command,**kwargs):return subprocess.CompletedProcess(command,0,'',json.dumps(payload()))
    assert not cli_runtime.detect_cli_runtime(tmp_path,runner=stderr)['installed']


def test_repository_package_matches_runtime_validator():
    package=Path(__file__).parents[2]/'packages/puddingclaw-deploy-cli'
    assert cli_runtime._validate_package_dir(package) is None


@pytest.mark.parametrize('change',['legacy_name','legacy_bin','extra_alias','symlink'])
def test_legacy_or_ambiguous_package_is_not_installed(tmp_path,change):
    (tmp_path/'src').mkdir();(tmp_path/'src/cli.js').write_text('')
    manifest={'name':'@puddingai/puddingharness','version':cli_runtime.CLI_VERSION,'bin':{'puddingharness':'src/cli.js'}}
    if change=='legacy_name':manifest['name']='@puddingai/puddingclaw'
    if change=='legacy_bin':manifest['bin']={'puddingclaw':'src/cli.js'}
    if change=='extra_alias':manifest['bin']['puddingclaw']='src/cli.js'
    if change=='symlink':
        (tmp_path/'src/cli.js').unlink();(tmp_path/'actual').write_text('');(tmp_path/'src/cli.js').symlink_to(tmp_path/'actual')
    (tmp_path/'package.json').write_text(json.dumps(manifest))
    assert cli_runtime._validate_package_dir(tmp_path)


def test_installed_layout_uses_home_lock_not_site_packages(tmp_path,monkeypatch):
    home=tmp_path/'home';base=tmp_path/'site-packages';base.mkdir()
    monkeypatch.setenv('PUDDINGHARNESS_HOME',str(home))
    lock=cli_runtime._acquire_file_lock(base)
    try:
        assert (home/'data/.puddingharness-cli-install.lock').is_file()
        assert not (base/'data').exists()
    finally:cli_runtime._release_file_lock(lock)


def install_fixture(tmp_path,monkeypatch):
    import threading
    monkeypatch.setenv('PUDDINGHARNESS_CLI_INSTALL_POLICY','auto')
    monkeypatch.setenv('PUDDINGHARNESS_HOME',str(tmp_path/'home'))
    monkeypatch.setenv('PUDDINGHARNESS_CLI_PACKAGE_DIR',str(Path(__file__).parents[2]/'packages/puddingclaw-deploy-cli'))
    monkeypatch.setattr(cli_runtime.shutil,'which',lambda name:None if name=='puddingharness' else '/fake/'+name)
    monkeypatch.setattr(cli_runtime,'_install_thread_lock',threading.Lock())


def test_npm_success_without_verified_cli_is_not_install_success(tmp_path,monkeypatch):
    install_fixture(tmp_path,monkeypatch)
    commands=[]
    def run(command,**kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command,0,'v24.0.0' if 'node' in command[0] else '11.0.0','')
    result=cli_runtime.ensure_cli_runtime(tmp_path/'site-packages',runner=run)
    assert result['install_attempted'] and not result['installed'] and not result['install_succeeded']
    assert any('install' in c for c in commands)
    assert not cli_runtime._install_thread_lock.locked()


def test_lock_io_failure_does_not_strand_install_thread_lock(tmp_path,monkeypatch):
    install_fixture(tmp_path,monkeypatch)
    def fail(_):raise OSError('unwritable home')
    monkeypatch.setattr(cli_runtime,'_acquire_file_lock',fail)
    result=cli_runtime.ensure_cli_runtime(tmp_path/'site-packages',runner=runner(''))
    assert not result['install_attempted'] and not result['installed']
    assert not cli_runtime._install_thread_lock.locked()


def test_cli_prerelease_identity_is_preserved(tmp_path,monkeypatch):
    monkeypatch.setattr(cli_runtime.shutil,'which',lambda name:'/fake/'+name)
    expected=cli_runtime.detect_cli_runtime(tmp_path,runner=runner(json.dumps(payload())))
    assert expected['installed'] and expected['version']==cli_runtime.CLI_VERSION
    other=cli_runtime.detect_cli_runtime(tmp_path,runner=runner(json.dumps(payload(cli_version='0.1.20'))))
    assert not other['installed'] and other['version_mismatch']
