import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from harness.installation_authority import assign, digest, enroll, suspend
from harness.installation_manifest import (
    cutover_installation as _cutover_installation,
    discover_installation, finalize_installation, prepare_installation as _prepare_installation,
    rollback_installation, validate_manifest,
)
from test_installation_manifest import _staging

DOMAINS = ('session_harness', 'knowledge_catalog', 'connector_jobs')
CUTOVER_WRITERS = {'session_harness': 'puddingharness', 'knowledge_catalog': 'puddingknowledge',
                   'connector_jobs': 'puddingknowledge'}
SOURCE_FREEZE = 'sha256:' + 'f' * 64


def cutover_installation(output, **kwargs):
    kwargs.setdefault('source_freeze_receipt_sha256', SOURCE_FREEZE)
    return _cutover_installation(output, **kwargs)


def prepare_installation(root, stage, receipt, output, **kwargs):
    kwargs.setdefault('knowledge_readiness', root.parent / 'cutover-readiness.json')
    kwargs.setdefault('source_freeze_receipt', root.parent / 'source-freeze-receipt.json')
    return _prepare_installation(root, stage, receipt, output, **kwargs)


@pytest.fixture(scope='module')
def staged(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('installation-manifest-advance')
    return _staging(tmp)


def _prepared(staged, tmp_path, name='m'):
    root, stage, receipt = staged
    output = tmp_path / name / 'manifest.json'
    discover_installation(root, output)
    prepare_installation(root, stage, receipt, output)
    return root, output


def _manifest(path):
    return json.loads(path.read_bytes())


def _rewrite(path, value):
    path.write_bytes(json.dumps(value).encode())


def _harness_journal(tmp_path, output, operation='cutover-1', name='harness-home'):
    home = tmp_path / name
    home.mkdir(mode=0o700)
    enroll(home, tmp_path / (name + '-authority'), 'enroll-harness')
    suspend(home, operation)
    return assign(home, output, operation, 'puddingharness')


def _knowledge_journal(commitment, operation='cutover-1', *, writer='puddingknowledge', evidence=None):
    def event(number, previous, op, state, writers, receipt, **extra):
        value = {'revision': number, 'previous': previous, 'operation_id': op, 'state': state,
                 'writers': writers, 'freeze_receipt_sha256': receipt, **extra}
        return dict(value, sha256=digest(value))
    both = {'knowledge_catalog': 'puddingknowledge', 'connector_jobs': 'puddingknowledge'}
    e0 = event(0, None, 'enroll-knowledge', 'existing_writer', both, None)
    e1 = event(1, e0['sha256'], operation, 'suspended',
               {'knowledge_catalog': None, 'connector_jobs': None}, 'f' * 64)
    extra = {'active_installation_revision': 'sha256:' + commitment,
             'migration_manifest_sha256': commitment, 'rollback_evidence_sha256': evidence}
    e2 = event(2, e1['sha256'], operation, 'assigned',
               {'knowledge_catalog': writer, 'connector_jobs': writer}, 'f' * 64, **extra)
    return {'format': 'puddingknowledge-writer-authority/v1', 'binding_sha256': '0' * 64,
            'events': [e0, e1, e2]}


def _journals(tmp_path, output, operation='cutover-1'):
    commitment = hashlib.sha256(output.read_bytes()).hexdigest()
    return _harness_journal(tmp_path, output, operation), _knowledge_journal(commitment, operation)


def _evidence(root, name='rollback-evidence.json'):
    path = root / name
    path.write_bytes(('{"reverse":"candidate","name":' + json.dumps(name) + '}\n').encode())
    path.chmod(0o600)
    return path


def test_cutover_advance_registers_both_assignments_and_replays(staged, tmp_path):
    _, output = _prepared(staged, tmp_path)
    prepared_bytes = output.read_bytes()
    commitment = hashlib.sha256(prepared_bytes).hexdigest()
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    result = cutover_installation(output, harness_journal=harness_journal,
                                  knowledge_journal=knowledge_journal)
    assert result['state'] == 'CUTOVER' and result['idempotent'] is False
    assert result['activation_allowed'] is False and result['installation_cutover_performed'] is False
    assert result['rollback_completed'] is False
    manifest = _manifest(output)
    assert manifest['state'] == 'CUTOVER'
    assert manifest['active_writers'] == CUTOVER_WRITERS
    assert manifest['active_installation_revision'] == 'sha256:' + commitment
    assert manifest['checkpoint']['harness_assigned_event_sha256'] == 'sha256:' + harness_journal['events'][2]['sha256']
    assert manifest['checkpoint']['knowledge_assigned_event_sha256'] == 'sha256:' + knowledge_journal['events'][2]['sha256']
    assert manifest['checkpoint']['source_freeze_receipt_sha256'] == SOURCE_FREEZE
    assert manifest['rollback_window_open'] is True and manifest['completed_at'] is None
    assert manifest['started_at'] and manifest['staging_namespace']
    validate_manifest(manifest)
    assert result['manifest_digest'] == 'sha256:' + hashlib.sha256(output.read_bytes()).hexdigest()
    before = output.read_bytes()
    again = cutover_installation(output, harness_journal=harness_journal,
                                 knowledge_journal=knowledge_journal)
    assert again['idempotent'] is True and again['manifest_digest'] == result['manifest_digest']
    assert output.read_bytes() == before


def test_cutover_rejects_source_freeze_different_from_prepared_readiness(staged, tmp_path):
    _, output = _prepared(staged, tmp_path)
    before = output.read_bytes()
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    with pytest.raises(ValueError, match='does not match readiness evidence'):
        cutover_installation(
            output, harness_journal=harness_journal, knowledge_journal=knowledge_journal,
            source_freeze_receipt_sha256='sha256:' + 'e' * 64)
    assert output.read_bytes() == before


def test_cutover_requires_prepared_state(staged, tmp_path):
    root, output = _prepared(staged, tmp_path)
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    for state in ('DISCOVERED', 'CUTOVER', 'ROLLED_BACK', 'FINALIZED'):
        other = tmp_path / ('m-' + state.lower()) / 'manifest.json'
        discover_installation(root, other)
        if state != 'DISCOVERED':
            manifest = _manifest(other)
            manifest['state'] = state
            _rewrite(other, manifest)
        with pytest.raises(ValueError):
            cutover_installation(other, harness_journal=harness_journal,
                                 knowledge_journal=knowledge_journal)
        assert _manifest(other)['state'] == state


def _craft_harness_journal(commitment, operation='cutover-1', *, writer='puddingharness'):
    def event(number, previous, op, state, writer_value, receipt, **extra):
        value = {'revision': number, 'previous': previous, 'operation_id': op, 'state': state,
                 'writer': writer_value, 'freeze_receipt_sha256': receipt, **extra}
        return dict(value, sha256=digest(value))
    e0 = event(0, None, 'enroll-harness', 'existing_writer', 'session_harness', None)
    e1 = event(1, e0['sha256'], operation, 'suspended', None, 'f' * 64)
    extra = {'active_installation_revision': 'sha256:' + commitment,
             'migration_manifest_sha256': commitment, 'rollback_evidence_sha256': None}
    e2 = event(2, e1['sha256'], operation, 'assigned', writer, 'f' * 64, **extra)
    return {'format': 'puddingharness-writer-authority/v1', 'binding_sha256': '0' * 64,
            'events': [e0, e1, e2]}


@pytest.mark.parametrize('change', [
    'harness_writer', 'knowledge_writer', 'rollback_evidence', 'event_count',
    'suspended_operation', 'event_digest', 'different_operations',
])
def test_cutover_journal_violations_fail_closed(staged, tmp_path, change):
    _, output = _prepared(staged, tmp_path)
    before = output.read_bytes()
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    if change == 'harness_writer':
        harness_journal['events'][2]['writer'] = 'puddingclaw'
    if change == 'knowledge_writer':
        knowledge_journal = _knowledge_journal(hashlib.sha256(before).hexdigest(), writer='puddingclaw',
                                               evidence='e' * 64)
    if change == 'rollback_evidence':
        knowledge_journal = _knowledge_journal(hashlib.sha256(before).hexdigest(), evidence='e' * 64)
    if change == 'event_count':
        knowledge_journal['events'] = knowledge_journal['events'][:2]
    if change == 'suspended_operation':
        knowledge_journal['events'][1]['operation_id'] = 'other'
    if change == 'event_digest':
        knowledge_journal['events'][2]['sha256'] = '0' * 64
    if change == 'different_operations':
        knowledge_journal = _knowledge_journal(hashlib.sha256(before).hexdigest(), 'other')
    with pytest.raises(ValueError):
        cutover_installation(output, harness_journal=harness_journal,
                             knowledge_journal=knowledge_journal)
    assert output.read_bytes() == before


def test_cutover_commitment_mismatch_leaves_manifest_prepared(staged, tmp_path):
    _, output = _prepared(staged, tmp_path)
    before = output.read_bytes()
    harness_journal = _harness_journal(tmp_path, output)
    # Journals disagree on the committed manifest.
    with pytest.raises(ValueError, match='different manifests'):
        cutover_installation(output, harness_journal=harness_journal,
                             knowledge_journal=_knowledge_journal('0' * 64))
    # Journals agree but commit to a different manifest than the stored one.
    with pytest.raises(ValueError, match='different manifest'):
        cutover_installation(output, harness_journal=_craft_harness_journal('0' * 64),
                             knowledge_journal=_knowledge_journal('0' * 64))
    assert output.read_bytes() == before


@pytest.mark.parametrize('change', ['writer', 'checkpoint_key', 'revision', 'window'])
def test_cutover_tampered_cutover_manifest_rejected(staged, tmp_path, change):
    _, output = _prepared(staged, tmp_path)
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    cutover_installation(output, harness_journal=harness_journal, knowledge_journal=knowledge_journal)
    manifest = _manifest(output)
    if change == 'writer':
        manifest['active_writers']['session_harness'] = 'puddingclaw'
    if change == 'checkpoint_key':
        del manifest['checkpoint']['knowledge_assigned_event_sha256']
    if change == 'revision':
        manifest['active_installation_revision'] = 'sha256:' + '0' * 64
    if change == 'window':
        manifest['rollback_window_open'] = False
    _rewrite(output, manifest)
    before = output.read_bytes()
    with pytest.raises(ValueError):
        cutover_installation(output, harness_journal=harness_journal,
                             knowledge_journal=knowledge_journal)
    assert output.read_bytes() == before


def test_rollback_advance_binds_evidence_and_replays(staged, tmp_path):
    _, output = _prepared(staged, tmp_path)
    evidence = _evidence(tmp_path)
    commitment = 'sha256:' + hashlib.sha256(evidence.read_bytes()).hexdigest()
    result = rollback_installation(output, rollback_evidence=evidence)
    assert result['state'] == 'ROLLED_BACK' and result['idempotent'] is False
    assert result['installation_cutover_performed'] is False and result['rollback_completed'] is False
    manifest = _manifest(output)
    assert manifest['state'] == 'ROLLED_BACK' and manifest['rollback_evidence_digest'] == commitment
    assert manifest['active_writers'] == {domain: 'puddingclaw' for domain in DOMAINS}
    assert manifest['rollback_window_open'] is True and manifest['completed_at'] is None
    validate_manifest(manifest)
    before = output.read_bytes()
    again = rollback_installation(output, rollback_evidence=evidence)
    assert again['idempotent'] is True and again['manifest_digest'] == result['manifest_digest']
    assert output.read_bytes() == before
    other = _evidence(tmp_path, 'other-evidence.json')
    with pytest.raises(ValueError, match='does not match'):
        rollback_installation(output, rollback_evidence=other)
    assert output.read_bytes() == before


def test_rollback_requires_prepared_state(staged, tmp_path):
    root, output = _prepared(staged, tmp_path)
    evidence = _evidence(tmp_path)
    for state in ('DISCOVERED', 'CUTOVER', 'FINALIZED'):
        other = tmp_path / ('m-' + state.lower()) / 'manifest.json'
        discover_installation(root, other)
        if state != 'DISCOVERED':
            manifest = _manifest(other)
            manifest['state'] = state
            _rewrite(other, manifest)
        with pytest.raises(ValueError):
            rollback_installation(other, rollback_evidence=evidence)
        assert 'rollback_evidence_digest' not in _manifest(other)


def test_finalize_closes_window_and_replays(staged, tmp_path):
    _, output = _prepared(staged, tmp_path)
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    cutover_installation(output, harness_journal=harness_journal, knowledge_journal=knowledge_journal)
    result = finalize_installation(output)
    assert result['state'] == 'FINALIZED' and result['idempotent'] is False
    assert result['activation_allowed'] is False and result['installation_cutover_performed'] is False
    manifest = _manifest(output)
    assert manifest['state'] == 'FINALIZED'
    assert manifest['rollback_window_open'] is False
    assert isinstance(manifest['completed_at'], str) and manifest['completed_at']
    assert manifest['active_writers'] == CUTOVER_WRITERS
    validate_manifest(manifest)
    before = output.read_bytes()
    again = finalize_installation(output)
    assert again['idempotent'] is True and again['manifest_digest'] == result['manifest_digest']
    assert output.read_bytes() == before


def test_finalize_requires_cutover_state(staged, tmp_path):
    root, output = _prepared(staged, tmp_path)
    with pytest.raises(ValueError, match='FINALIZED'):
        finalize_installation(output)
    evidence = _evidence(tmp_path)
    rollback_installation(output, rollback_evidence=evidence)
    # ROLLED_BACK to FINALIZED is a future increment and fails closed.
    with pytest.raises(ValueError, match='FINALIZED'):
        finalize_installation(output)
    assert _manifest(output)['state'] == 'ROLLED_BACK'
    other = tmp_path / 'm2' / 'manifest.json'
    discover_installation(root, other)
    with pytest.raises(ValueError, match='FINALIZED'):
        finalize_installation(other)


@pytest.mark.parametrize('change', ['window', 'completed', 'writer', 'checkpoint_key'])
def test_finalize_tampered_manifest_rejected(staged, tmp_path, change):
    _, output = _prepared(staged, tmp_path)
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    cutover_installation(output, harness_journal=harness_journal, knowledge_journal=knowledge_journal)
    finalize_installation(output)
    manifest = _manifest(output)
    if change == 'window':
        manifest['rollback_window_open'] = True
    if change == 'completed':
        manifest['completed_at'] = None
    if change == 'writer':
        manifest['active_writers']['knowledge_catalog'] = 'puddingclaw'
    if change == 'checkpoint_key':
        del manifest['checkpoint']['harness_assigned_event_sha256']
    _rewrite(output, manifest)
    before = output.read_bytes()
    with pytest.raises(ValueError):
        finalize_installation(output)
    assert output.read_bytes() == before


def test_crash_after_advance_commit_resumes_idempotently(staged, tmp_path):
    _, output = _prepared(staged, tmp_path)
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    states = []

    def crash(state):
        states.append(state)
        raise RuntimeError('crash after commit')

    with pytest.raises(RuntimeError):
        cutover_installation(output, harness_journal=harness_journal,
                             knowledge_journal=knowledge_journal, _after_checkpoint=crash)
    assert states == ['CUTOVER'] and _manifest(output)['state'] == 'CUTOVER'
    result = cutover_installation(output, harness_journal=harness_journal,
                                  knowledge_journal=knowledge_journal)
    assert result['state'] == 'CUTOVER' and result['idempotent'] is True
    with pytest.raises(RuntimeError):
        finalize_installation(output, _after_checkpoint=crash)
    assert states == ['CUTOVER', 'FINALIZED'] and _manifest(output)['state'] == 'FINALIZED'
    assert finalize_installation(output)['idempotent'] is True
    other = tmp_path / 'm2' / 'manifest.json'
    discover_installation(staged[0], other)
    prepare_installation(staged[0], staged[1], staged[2], other)
    evidence = _evidence(tmp_path)
    with pytest.raises(RuntimeError):
        rollback_installation(other, rollback_evidence=evidence, _after_checkpoint=crash)
    assert states == ['CUTOVER', 'FINALIZED', 'ROLLED_BACK']
    assert rollback_installation(other, rollback_evidence=evidence)['idempotent'] is True


def test_cli_advance_commands_and_fail_closed_error(staged, tmp_path):
    _, output = _prepared(staged, tmp_path)
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    env = dict(os.environ)
    if os.environ.get('HARNESS_TEST_INSTALLED') == '1':
        env.pop('PYTHONPATH', None)
    else:
        env['PYTHONPATH'] = str(Path(__file__).parents[1])

    def run(*arguments):
        return subprocess.run([sys.executable, '-m', 'harness.installation_manifest', *arguments],
                              env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)

    def journal_file(name, journal):
        path = tmp_path / name
        path.write_bytes(json.dumps(journal).encode())
        path.chmod(0o600)
        return path

    harness_file = journal_file('harness-journal.json', harness_journal)
    knowledge_file = journal_file('knowledge-journal.json', knowledge_journal)
    bad = run('finalize', '--output', str(output))
    assert bad.returncode == 1
    report = json.loads(bad.stdout)
    assert report['error_code'] == 'installation_manifest_rejected'
    assert report['activation_allowed'] is False and report['installation_cutover_performed'] is False
    value = run('cutover', '--output', str(output), '--harness-journal', str(harness_file),
                '--knowledge-journal', str(knowledge_file),
                '--source-freeze-receipt-sha256', SOURCE_FREEZE)
    assert value.returncode == 0, value.stderr + value.stdout
    report = json.loads(value.stdout)
    assert report['state'] == 'CUTOVER' and report['idempotent'] is False
    assert report['installation_cutover_performed'] is False
    retry = run('cutover', '--output', str(output), '--harness-journal', str(harness_file),
                '--knowledge-journal', str(knowledge_file),
                '--source-freeze-receipt-sha256', SOURCE_FREEZE)
    assert retry.returncode == 0 and json.loads(retry.stdout)['idempotent'] is True
    done = run('finalize', '--output', str(output))
    assert done.returncode == 0 and json.loads(done.stdout)['state'] == 'FINALIZED'
    again = run('finalize', '--output', str(output))
    assert again.returncode == 0 and json.loads(again.stdout)['idempotent'] is True
    other = tmp_path / 'm2' / 'manifest.json'
    discover_installation(staged[0], other)
    prepare_installation(staged[0], staged[1], staged[2], other)
    evidence = _evidence(tmp_path)
    rolled = run('rollback', '--output', str(other), '--rollback-evidence', str(evidence))
    assert rolled.returncode == 0 and json.loads(rolled.stdout)['state'] == 'ROLLED_BACK'
    conflict = _evidence(tmp_path, 'conflict-evidence.json')
    rejected = run('rollback', '--output', str(other), '--rollback-evidence', str(conflict))
    assert rejected.returncode == 1
    assert json.loads(rejected.stdout)['error_code'] == 'installation_manifest_rejected'
