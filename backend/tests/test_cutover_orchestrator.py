"""Installed two-product cutover tests opt in through an explicit independent interpreter."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from harness import cutover_orchestrator as orchestrator
from harness import installation_authority as authority
from harness import writer_barrier as barrier
from harness.installation_guard import InstallationGuard
from harness.installation_manifest import validate_manifest
from test_writer_barrier import SETUP

KNOWLEDGE = os.environ.get('KNOWLEDGE_TEST_PYTHON')
pytestmark = pytest.mark.skipif(not KNOWLEDGE, reason='Explicit independent Knowledge installation required')

CUTOVER_WRITERS = {'session_harness': 'puddingharness', 'knowledge_catalog': 'puddingknowledge',
                   'connector_jobs': 'puddingknowledge'}


def _prepared_manifest(directory, suffix=''):
    directory.mkdir(mode=0o700)
    value = {'format': 'agent-knowledge-platform-installation-migration/v1',
             'source': {'installation_id': 'inst-1' + suffix, 'schema_revision': 'rev-1',
                        'catalog_revision': 'rev-1'},
             'targets': {'puddingharness': 'puddingharness-backend@0.1.0',
                         'puddingknowledge': 'puddingknowledge-local@0.1.0'},
             'object_summaries': [{'domain': 'session_harness', 'object_count': 1,
                                   'source_digest': 'sha256:' + 'a' * 64}],
             'id_resource_mappings': [], 'credential_rebinds': [],
             'active_writers': {'session_harness': 'puddingclaw', 'knowledge_catalog': 'puddingclaw',
                                'connector_jobs': 'puddingclaw'},
             'checkpoint': {'stage': 'prepared'}, 'rollback_strategy': 'no_write_until_finalized',
             'state': 'PREPARED', 'rollback_window_open': True,
             'snapshot_digest': 'sha256:' + 'b' * 64, 'staging_namespace': 'sha256:' + 'c' * 64,
             'active_installation_revision': None, 'completed_at': None,
             'started_at': '2026-09-16T00:00:00Z'}
    path = directory / 'manifest.json'
    path.write_bytes(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
    path.chmod(0o600)
    return path


@pytest.fixture
def roots(tmp_path):
    root = tmp_path.resolve()
    home = root / 'harness'
    home.mkdir(mode=0o700)
    authority.enroll(home, root / 'harness-authority', 'enroll-harness')
    subprocess.run([KNOWLEDGE, '-c', SETUP, str(root)], check=True, cwd=root)
    manifest = _prepared_manifest(root / 'manifest')
    return home, root / 'knowledge', KNOWLEDGE, root / 'checkpoint', manifest, root / 'barrier'


def _suspend(roots, operation='cutover-1'):
    barrier.suspend_writers(roots[0], roots[1], roots[2], roots[5], operation)


def run(roots, operation='cutover-1', **kwargs):
    return orchestrator.cutover(roots[0], roots[1], roots[2], roots[3], roots[4], operation, **kwargs)


def _manifest(roots):
    return json.loads(roots[4].read_bytes())


def test_cutover_happy_path_exact_retry_and_runtime_admission(roots):
    _suspend(roots)
    result = run(roots)
    assert result['state'] == 'both_thawed'
    assert result['installation_cutover_performed'] is True
    assert result['activation_allowed'] is False and result['rollback_completed'] is False
    assert result['production_activated'] is False
    prepared = result['prepared_manifest_sha256']
    harness_events = result['journals']['harness']['events']
    knowledge_events = result['journals']['knowledge']['events']
    assert len(harness_events) == 3 and len(knowledge_events) == 3
    harness_head, knowledge_head = harness_events[2], knowledge_events[2]
    assert harness_head['writer'] == 'puddingharness'
    assert knowledge_head['writers'] == {'knowledge_catalog': 'puddingknowledge',
                                         'connector_jobs': 'puddingknowledge'}
    for head in (harness_head, knowledge_head):
        assert head['migration_manifest_sha256'] == prepared
        assert head['active_installation_revision'] == 'sha256:' + prepared
        assert head['rollback_evidence_sha256'] is None
        assert head['operation_id'] == 'cutover-1'
    manifest = _manifest(roots)
    assert manifest['state'] == 'CUTOVER' and manifest['active_writers'] == CUTOVER_WRITERS
    assert manifest['active_installation_revision'] == 'sha256:' + prepared
    assert manifest['checkpoint']['harness_assigned_event_sha256'] == 'sha256:' + harness_head['sha256']
    assert manifest['checkpoint']['knowledge_assigned_event_sha256'] == 'sha256:' + knowledge_head['sha256']
    assert manifest['rollback_window_open'] is True
    validate_manifest(manifest)
    assert hashlib.sha256(roots[4].read_bytes()).hexdigest() == result['cutover_manifest_sha256']
    pointer = json.loads((roots[0] / 'active-installation.json').read_bytes())
    assert pointer['format'] == 'puddingharness-active-installation/v1'
    assert pointer['cutover_manifest_sha256'] == result['cutover_manifest_sha256']
    assert pointer['prepared_manifest_sha256'] == prepared
    assert pointer['active_installation_revision'] == 'sha256:' + prepared
    assert pointer['harness_assigned_event_sha256'] == harness_head['sha256']
    assert pointer['knowledge_assigned_event_sha256'] == knowledge_head['sha256']
    assert pointer['active_writers'] == CUTOVER_WRITERS
    assert hashlib.sha256((roots[0] / 'active-installation.json').read_bytes()).hexdigest() == result['active_pointer_sha256']
    assert not (roots[0] / '.installation-freeze-v1.json').exists()
    assert not (roots[1] / '.workspace-freeze-v1.json').exists()
    assert (roots[0].parent / 'harness-authority' / 'freeze-marker-rev2.json').exists()
    assert (roots[0].parent / 'harness-authority' / 'thaw-receipt-rev2.json').exists()
    assert (roots[1].parent / 'knowledge-authority' / 'freeze-marker-rev2.json').exists()
    assert (roots[1].parent / 'knowledge-authority' / 'thaw-receipt-rev2.json').exists()
    with InstallationGuard(roots[0]):
        pass
    opened = subprocess.run([KNOWLEDGE, '-c',
                             'from knowledge_platform.local.workspace import open_persistent_workspace;'
                             'import sys;open_persistent_workspace(sys.argv[1])', str(roots[1])],
                            capture_output=True)
    assert opened.returncode == 0, opened.stderr
    status = subprocess.run([KNOWLEDGE, '-m', 'knowledge_platform.local.writer_authority', 'status',
                             '--state-dir', str(roots[1])], capture_output=True)
    assert status.returncode == 0
    assert json.loads(status.stdout)['journal'] == result['journals']['knowledge']
    # The advanced CUTOVER bytes no longer satisfy either thaw binding.
    with pytest.raises(ValueError, match='manifest changed'):
        authority.thaw(roots[0], roots[4], 'cutover-1')
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    assert run(roots) == result
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes


@pytest.mark.parametrize('crash_at', orchestrator._ORDER)
def test_crash_at_every_checkpoint_resumes_identically(roots, crash_at):
    _suspend(roots)

    def crash(state):
        if state == crash_at:
            raise RuntimeError('injected crash')

    with pytest.raises(RuntimeError):
        run(roots, _after_checkpoint=crash)
    assert json.loads((roots[3] / 'checkpoint.json').read_bytes())['state'] == crash_at
    if crash_at in ('harness_assigned', 'both_assigned', 'manifest_cutover', 'active_pointer_published'):
        assert (roots[0] / '.installation-freeze-v1.json').exists()
        assert (roots[1] / '.workspace-freeze-v1.json').exists()
    if crash_at == 'harness_thawed':
        assert not (roots[0] / '.installation-freeze-v1.json').exists()
        assert (roots[1] / '.workspace-freeze-v1.json').exists()
    if crash_at in ('harness_assigned', 'both_assigned'):
        assert _manifest(roots)['state'] == 'PREPARED'
    result = run(roots)
    assert result['state'] == 'both_thawed' and result['installation_cutover_performed'] is True
    assert _manifest(roots)['state'] == 'CUTOVER'
    assert not (roots[1] / '.workspace-freeze-v1.json').exists()
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    assert run(roots) == result
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes


def test_delegation_failure_leaves_knowledge_suspended(roots, monkeypatch):
    _suspend(roots)
    original = orchestrator._delegate

    def fail(command, *args, **kwargs):
        if 'assign' in command:
            raise ValueError('injected delegation failure')
        return original(command, *args, **kwargs)

    monkeypatch.setattr(orchestrator, '_delegate', fail)
    with pytest.raises(ValueError, match='injected delegation failure'):
        run(roots)
    assert json.loads((roots[3] / 'checkpoint.json').read_bytes())['state'] == 'harness_assigned'
    assert _manifest(roots)['state'] == 'PREPARED'
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
    knowledge_journal = json.loads((roots[1].parent / 'knowledge-authority' / 'journal.json').read_bytes())
    assert knowledge_journal['events'][-1]['state'] == 'suspended'
    monkeypatch.setattr(orchestrator, '_delegate', original)
    assert run(roots)['state'] == 'both_thawed'


def test_successful_child_with_invalid_receipt_cannot_complete_cutover(roots, monkeypatch):
    _suspend(roots)
    original = orchestrator._delegate

    def alter(command, *args, **kwargs):
        raw = original(command, *args, **kwargs)
        if 'assign' in command:
            value = json.loads(raw)
            value['installation_cutover_performed'] = True
            return json.dumps(value).encode()
        return raw

    monkeypatch.setattr(orchestrator, '_delegate', alter)
    with pytest.raises(ValueError):
        run(roots)
    # The delegated child really committed its assignment; only the receipt was
    # invalid, so completion is never published and retry resumes the window.
    assert json.loads((roots[3] / 'checkpoint.json').read_bytes())['state'] == 'harness_assigned'
    knowledge_journal = json.loads((roots[1].parent / 'knowledge-authority' / 'journal.json').read_bytes())
    assert knowledge_journal['events'][-1]['state'] == 'assigned'
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
    assert _manifest(roots)['state'] == 'PREPARED'
    monkeypatch.setattr(orchestrator, '_delegate', original)
    assert run(roots)['state'] == 'both_thawed'


def test_unsuspended_writers_fail_closed(roots):
    manifest_bytes = roots[4].read_bytes()
    with pytest.raises(ValueError, match='not suspended'):
        run(roots)
    assert roots[4].read_bytes() == manifest_bytes
    assert not (roots[3] / 'checkpoint.json').exists()
    authority.suspend(roots[0], 'cutover-1')
    with pytest.raises(ValueError, match='not suspended'):
        run(roots)
    assert roots[4].read_bytes() == manifest_bytes
    assert not (roots[3] / 'checkpoint.json').exists()
    assert authority.journal(authority.load_binding(roots[0]))['events'][-1]['state'] == 'suspended'


def test_wrong_operation_id_fails_closed(roots):
    _suspend(roots, 'other-operation')
    manifest_bytes = roots[4].read_bytes()
    with pytest.raises(ValueError, match='another operation'):
        run(roots)
    assert roots[4].read_bytes() == manifest_bytes
    assert not (roots[3] / 'checkpoint.json').exists()


def test_manifest_digest_mismatch_fails_closed(roots):
    _suspend(roots)
    other = _prepared_manifest(roots[0].parent / 'other-manifest', suffix='-2')
    authority.assign(roots[0], other, 'cutover-1', 'puddingharness')
    manifest_bytes = roots[4].read_bytes()
    with pytest.raises(ValueError, match='different manifest'):
        run(roots)
    assert roots[4].read_bytes() == manifest_bytes
    assert not (roots[3] / 'checkpoint.json').exists()
    knowledge_journal = json.loads((roots[1].parent / 'knowledge-authority' / 'journal.json').read_bytes())
    assert knowledge_journal['events'][-1]['state'] == 'suspended'


def test_manifest_changed_after_harness_assignment_fails_closed(roots):
    _suspend(roots)

    def stop(state):
        if state == 'harness_assigned':
            raise RuntimeError('stop after Harness assignment')

    with pytest.raises(RuntimeError):
        run(roots, _after_checkpoint=stop)
    manifest = _manifest(roots)
    manifest['object_summaries'][0]['object_count'] += 1
    roots[4].write_bytes(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
    before = (roots[3] / 'checkpoint.json').read_bytes()
    with pytest.raises(ValueError, match='changed before Knowledge assignment'):
        run(roots)
    assert (roots[3] / 'checkpoint.json').read_bytes() == before
    assert (roots[1] / '.workspace-freeze-v1.json').exists()


def test_finalize_command_requires_completed_cutover_then_replays(roots):
    _suspend(roots)
    with pytest.raises(ValueError, match='missing'):
        orchestrator.finalize_cutover(roots[4], roots[3])

    def stop(state):
        if state == 'both_assigned':
            raise RuntimeError('stop before advance')

    with pytest.raises(RuntimeError):
        run(roots, _after_checkpoint=stop)
    with pytest.raises(ValueError, match='completed cutover'):
        orchestrator.finalize_cutover(roots[4], roots[3])
    result = run(roots)
    final = orchestrator.finalize_cutover(roots[4], roots[3])
    assert final['state'] == 'FINALIZED' and final['idempotent'] is False
    assert final['installation_cutover_performed'] is True
    assert final['activation_allowed'] is False and final['rollback_completed'] is False
    assert final['production_activated'] is False
    manifest = _manifest(roots)
    assert manifest['state'] == 'FINALIZED' and manifest['rollback_window_open'] is False
    assert isinstance(manifest['completed_at'], str) and manifest['completed_at']
    assert manifest['active_writers'] == CUTOVER_WRITERS
    validate_manifest(manifest)
    assert final['manifest_digest'] == 'sha256:' + hashlib.sha256(roots[4].read_bytes()).hexdigest()
    before = roots[4].read_bytes()
    again = orchestrator.finalize_cutover(roots[4], roots[3])
    assert again['idempotent'] is True and again['manifest_digest'] == final['manifest_digest']
    assert roots[4].read_bytes() == before
    assert result['state'] == 'both_thawed'


def test_cli_cutover_finalize_and_fail_closed_error(roots):
    _suspend(roots)
    env = dict(os.environ)
    if os.environ.get('HARNESS_TEST_INSTALLED') == '1':
        env.pop('PYTHONPATH', None)
    else:
        env['PYTHONPATH'] = str(Path(__file__).parents[1])

    def cli(*arguments):
        return subprocess.run([sys.executable, '-m', 'harness.cutover_orchestrator', *arguments],
                              env=env, cwd='/private/tmp', capture_output=True, text=True, timeout=120)

    bad_dir = roots[0].parent / 'checkpoint-bad'
    bad = cli('cutover', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
              '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(bad_dir),
              '--manifest', str(roots[4]), '--operation-id', 'wrong-operation')
    assert bad.returncode == 1
    report = json.loads(bad.stdout)
    assert report['error_code'] == 'installation_cutover_rejected'
    assert report['activation_allowed'] is False and report['installation_cutover_performed'] is False
    assert report['rollback_completed'] is False and report['production_activated'] is False
    # A checkpoint directory binds its operation; a changed operation rejects.
    changed = cli('cutover', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
                  '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(bad_dir),
                  '--manifest', str(roots[4]), '--operation-id', 'cutover-1')
    assert changed.returncode == 1
    value = cli('cutover', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
                '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(roots[3]),
                '--manifest', str(roots[4]), '--operation-id', 'cutover-1')
    assert value.returncode == 0, value.stderr + value.stdout
    report = json.loads(value.stdout)
    assert report['state'] == 'both_thawed' and report['installation_cutover_performed'] is True
    assert report['production_activated'] is False
    retry = cli('cutover', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
                '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(roots[3]),
                '--manifest', str(roots[4]), '--operation-id', 'cutover-1')
    assert retry.returncode == 0 and json.loads(retry.stdout) == report
    done = cli('finalize', '--manifest', str(roots[4]), '--checkpoint-dir', str(roots[3]))
    assert done.returncode == 0, done.stderr + done.stdout
    report = json.loads(done.stdout)
    assert report['state'] == 'FINALIZED' and report['installation_cutover_performed'] is True
    again = cli('finalize', '--manifest', str(roots[4]), '--checkpoint-dir', str(roots[3]))
    assert again.returncode == 0 and json.loads(again.stdout)['idempotent'] is True
