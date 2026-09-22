"""Installed two-product rollback tests opt in through an explicit independent interpreter."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from harness import installation_authority as authority
from harness import installation_manifest as manifests
from harness import rollback_orchestrator as orchestrator
from harness import writer_barrier as barrier
from harness.installation_guard import AdmissionUnavailable, InstallationGuard
from harness.installation_manifest import validate_manifest
from test_writer_barrier import SETUP

KNOWLEDGE = os.environ.get('KNOWLEDGE_TEST_PYTHON')
pytestmark = pytest.mark.skipif(not KNOWLEDGE, reason='Explicit independent Knowledge installation required')


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


def _evidence(directory, payload=b'{"format":"rollback-evidence/v1","chain":"out-of-band"}\n'):
    directory.mkdir(mode=0o700)
    path = directory / 'rollback-evidence.json'
    path.write_bytes(payload)
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
    evidence = _evidence(root / 'evidence')
    return home, root / 'knowledge', KNOWLEDGE, root / 'checkpoint', manifest, evidence, root / 'barrier'


def _suspend(roots, operation='rollback-1'):
    barrier.suspend_writers(roots[0], roots[1], roots[2], roots[6], operation)


def run(roots, operation='rollback-1', **kwargs):
    return orchestrator.rollback(roots[0], roots[1], roots[2], roots[3], roots[4], roots[5],
                                 operation, **kwargs)


def _manifest(roots):
    return json.loads(roots[4].read_bytes())


def _evidence_sha(roots):
    return hashlib.sha256(roots[5].read_bytes()).hexdigest()


def test_rollback_happy_path_exact_retry_and_persistent_freeze(roots):
    _suspend(roots)
    result = run(roots)
    assert result['state'] == 'both_reassigned'
    assert result['rollback_completed'] is False
    assert result['activation_allowed'] is False and result['installation_cutover_performed'] is False
    assert result['production_activated'] is False
    prepared = result['prepared_manifest_sha256']
    rolled_back = result['rolled_back_manifest_sha256']
    evidence_sha = _evidence_sha(roots)
    assert result['rollback_evidence_sha256'] == evidence_sha
    assert prepared != rolled_back
    harness_events = result['journals']['harness']['events']
    knowledge_events = result['journals']['knowledge']['events']
    assert len(harness_events) == 3 and len(knowledge_events) == 3
    harness_head, knowledge_head = harness_events[2], knowledge_events[2]
    assert harness_head['writer'] == 'puddingclaw'
    assert knowledge_head['writers'] == {'knowledge_catalog': 'puddingclaw',
                                         'connector_jobs': 'puddingclaw'}
    for head in (harness_head, knowledge_head):
        assert head['migration_manifest_sha256'] == rolled_back
        assert head['active_installation_revision'] == 'sha256:' + rolled_back
        assert head['rollback_evidence_sha256'] == evidence_sha
        assert head['operation_id'] == 'rollback-1'
    manifest = _manifest(roots)
    assert manifest['state'] == 'ROLLED_BACK'
    assert manifest['active_writers'] == {'session_harness': 'puddingclaw',
                                          'knowledge_catalog': 'puddingclaw',
                                          'connector_jobs': 'puddingclaw'}
    assert manifest['rollback_evidence_digest'] == 'sha256:' + evidence_sha
    assert manifest['rollback_window_open'] is True and manifest['completed_at'] is None
    validate_manifest(manifest)
    assert hashlib.sha256(roots[4].read_bytes()).hexdigest() == rolled_back
    # No thaw: both freeze markers remain and nothing is retired or receipted.
    assert (roots[0] / '.installation-freeze-v1.json').exists()
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
    assert not (roots[0] / 'active-installation.json').exists()
    assert not (roots[0].parent / 'harness-authority' / 'freeze-marker-rev2.json').exists()
    assert not (roots[0].parent / 'harness-authority' / 'thaw-receipt-rev2.json').exists()
    assert not (roots[1].parent / 'knowledge-authority' / 'freeze-marker-rev2.json').exists()
    assert not (roots[1].parent / 'knowledge-authority' / 'thaw-receipt-rev2.json').exists()
    assert (roots[3] / 'prepared-manifest.json').read_bytes() != b''
    assert hashlib.sha256((roots[3] / 'prepared-manifest.json').read_bytes()).hexdigest() == prepared
    assert hashlib.sha256((roots[3] / 'rolled-back-manifest.json').read_bytes()).hexdigest() == rolled_back
    # Both new products stay fenced out; thaw refuses a puddingclaw assignment.
    with pytest.raises(AdmissionUnavailable):
        InstallationGuard(roots[0]).acquire()
    opened = subprocess.run([KNOWLEDGE, '-c',
                             'from knowledge_platform.local.workspace import open_persistent_workspace;'
                             'import sys;open_persistent_workspace(sys.argv[1])', str(roots[1])],
                            capture_output=True)
    assert opened.returncode != 0
    with pytest.raises(ValueError, match='not assigned to this Harness'):
        authority.thaw(roots[0], roots[4], 'rollback-1')
    status = subprocess.run([KNOWLEDGE, '-m', 'knowledge_platform.local.writer_authority', 'status',
                             '--state-dir', str(roots[1])], capture_output=True)
    assert status.returncode == 0
    assert json.loads(status.stdout)['journal'] == result['journals']['knowledge']
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
    assert (roots[0] / '.installation-freeze-v1.json').exists()
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
    assert _manifest(roots)['state'] == 'ROLLED_BACK'
    harness_events = json.loads((roots[0].parent / 'harness-authority' / 'journal.json').read_bytes())['events']
    knowledge_events = json.loads((roots[1].parent / 'knowledge-authority' / 'journal.json').read_bytes())['events']
    if crash_at == 'manifest_rolled_back':
        assert len(harness_events) == 2 and len(knowledge_events) == 2
    if crash_at == 'harness_reassigned':
        assert len(harness_events) == 3 and len(knowledge_events) == 2
    result = run(roots)
    assert result['state'] == 'both_reassigned' and result['rollback_completed'] is False
    assert _manifest(roots)['state'] == 'ROLLED_BACK'
    assert (roots[0] / '.installation-freeze-v1.json').exists()
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
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
    assert json.loads((roots[3] / 'checkpoint.json').read_bytes())['state'] == 'harness_reassigned'
    assert _manifest(roots)['state'] == 'ROLLED_BACK'
    assert (roots[0] / '.installation-freeze-v1.json').exists()
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
    knowledge_journal = json.loads((roots[1].parent / 'knowledge-authority' / 'journal.json').read_bytes())
    assert knowledge_journal['events'][-1]['state'] == 'suspended'
    monkeypatch.setattr(orchestrator, '_delegate', original)
    assert run(roots)['state'] == 'both_reassigned'


def test_successful_child_with_invalid_receipt_cannot_complete_rollback(roots, monkeypatch):
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
    assert json.loads((roots[3] / 'checkpoint.json').read_bytes())['state'] == 'harness_reassigned'
    knowledge_journal = json.loads((roots[1].parent / 'knowledge-authority' / 'journal.json').read_bytes())
    assert knowledge_journal['events'][-1]['state'] == 'assigned'
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
    assert _manifest(roots)['state'] == 'ROLLED_BACK'
    monkeypatch.setattr(orchestrator, '_delegate', original)
    assert run(roots)['state'] == 'both_reassigned'


def test_unsuspended_writers_fail_closed(roots):
    manifest_bytes = roots[4].read_bytes()
    with pytest.raises(ValueError, match='not suspended'):
        run(roots)
    assert roots[4].read_bytes() == manifest_bytes
    assert not (roots[3] / 'checkpoint.json').exists()
    authority.suspend(roots[0], 'rollback-1')
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


def test_manifest_not_prepared_fails_closed(roots):
    _suspend(roots)
    manifest = _manifest(roots)
    manifest['state'] = 'DISCOVERED'
    roots[4].write_bytes(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
    with pytest.raises(ValueError, match='requires a PREPARED installation manifest'):
        run(roots)
    assert not (roots[3] / 'checkpoint.json').exists()
    manifest['state'] = 'PREPARED'
    roots[4].write_bytes(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
    # An out-of-band advance without this orchestrator's preserved PREPARED copy
    # cannot pin the prepared commitment and refuses.
    manifests.rollback_installation(roots[4], rollback_evidence=roots[5])
    with pytest.raises(ValueError, match='no prepared copy'):
        orchestrator.rollback(roots[0], roots[1], roots[2], roots[0].parent / 'checkpoint-2',
                              roots[4], roots[5], 'rollback-1')


def test_foreign_harness_assignment_fails_closed(roots):
    _suspend(roots)
    other = _prepared_manifest(roots[0].parent / 'other-manifest', suffix='-2')
    other_evidence = _evidence(roots[0].parent / 'other-evidence', b'"other-evidence"\n')
    manifests.rollback_installation(other, rollback_evidence=other_evidence)
    authority.assign(roots[0], other, 'rollback-1', 'puddingclaw', rollback_evidence=other_evidence)
    manifest_bytes = roots[4].read_bytes()
    with pytest.raises(ValueError, match='no rollback checkpoint'):
        run(roots)
    assert roots[4].read_bytes() == manifest_bytes
    assert not (roots[3] / 'checkpoint.json').exists()
    knowledge_journal = json.loads((roots[1].parent / 'knowledge-authority' / 'journal.json').read_bytes())
    assert knowledge_journal['events'][-1]['state'] == 'suspended'


def test_evidence_digest_drift_between_runs_fails_closed(roots):
    _suspend(roots)

    def stop(state):
        if state == 'manifest_rolled_back':
            raise RuntimeError('stop after manifest advance')

    original = roots[5].read_bytes()
    with pytest.raises(RuntimeError):
        run(roots, _after_checkpoint=stop)
    roots[5].write_bytes(b'"drifted-evidence"\n')
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    with pytest.raises(ValueError, match='Rollback plan changed'):
        run(roots)
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes
    roots[5].write_bytes(original)
    assert run(roots)['state'] == 'both_reassigned'
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    roots[5].write_bytes(b'"drifted-evidence"\n')
    with pytest.raises(ValueError, match='Rollback plan changed'):
        run(roots)
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes
    roots[5].write_bytes(original)
    assert run(roots)['state'] == 'both_reassigned'


def test_committed_manifest_tampering_fails_closed(roots):
    _suspend(roots)
    result = run(roots)
    manifest = _manifest(roots)
    manifest['object_summaries'][0]['object_count'] += 1
    roots[4].write_bytes(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    with pytest.raises(ValueError, match='Rolled back installation manifest changed'):
        run(roots)
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes
    assert result['state'] == 'both_reassigned'


@pytest.mark.parametrize('target', [0, 1])
def test_freeze_marker_vanished_fails_closed(roots, target):
    _suspend(roots)
    run(roots)
    marker = roots[target] / ('.installation-freeze-v1.json' if target == 0 else '.workspace-freeze-v1.json')
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    marker.unlink()
    with pytest.raises(ValueError, match='Freeze marker vanished'):
        run(roots)
    assert not marker.exists()
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes


def test_cli_rollback_and_fail_closed_error(roots):
    _suspend(roots)
    env = dict(os.environ)
    if os.environ.get('HARNESS_TEST_INSTALLED') == '1':
        env.pop('PYTHONPATH', None)
    else:
        env['PYTHONPATH'] = str(Path(__file__).parents[1])

    def cli(*arguments):
        return subprocess.run([sys.executable, '-m', 'harness.rollback_orchestrator', *arguments],
                              env=env, cwd='/private/tmp', capture_output=True, text=True, timeout=120)

    bad_dir = roots[0].parent / 'checkpoint-bad'
    bad = cli('rollback', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
              '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(bad_dir),
              '--manifest', str(roots[4]), '--rollback-evidence', str(roots[5]),
              '--operation-id', 'wrong-operation')
    assert bad.returncode == 1
    report = json.loads(bad.stdout)
    assert report['error_code'] == 'installation_rollback_rejected'
    assert report['activation_allowed'] is False and report['installation_cutover_performed'] is False
    assert report['rollback_completed'] is False and report['production_activated'] is False
    # A checkpoint directory binds its operation; a changed operation rejects.
    changed = cli('rollback', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
                  '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(bad_dir),
                  '--manifest', str(roots[4]), '--rollback-evidence', str(roots[5]),
                  '--operation-id', 'rollback-1')
    assert changed.returncode == 1
    value = cli('rollback', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
                '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(roots[3]),
                '--manifest', str(roots[4]), '--rollback-evidence', str(roots[5]),
                '--operation-id', 'rollback-1')
    assert value.returncode == 0, value.stderr + value.stdout
    report = json.loads(value.stdout)
    assert report['state'] == 'both_reassigned' and report['rollback_completed'] is False
    assert report['activation_allowed'] is False and report['installation_cutover_performed'] is False
    assert report['production_activated'] is False
    retry = cli('rollback', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
                '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(roots[3]),
                '--manifest', str(roots[4]), '--rollback-evidence', str(roots[5]),
                '--operation-id', 'rollback-1')
    assert retry.returncode == 0 and json.loads(retry.stdout) == report
