"""Post-cutover window rollback tests (rev3 resuspension, rev4 reassignment).

The manifest-level CUTOVER to ROLLED_BACK unit tests run unconditionally; the
installed two-product orchestrator tests opt in through an explicit independent
Knowledge interpreter (KNOWLEDGE_TEST_PYTHON) and skip cleanly without it.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from harness import cutover_orchestrator as cutover
from harness import installation_authority as authority
from harness import installation_manifest as manifests
from harness import rollback_orchestrator as orchestrator
from harness import writer_barrier as barrier
from harness.installation_guard import AdmissionUnavailable, InstallationGuard
from harness.installation_manifest import validate_manifest
from harness.source_writer_fence import publish_source_fence
from test_installation_manifest import _staging
from test_installation_manifest_advance import _evidence, _journals, _manifest, _prepared, _rewrite
from test_installation_manifest_advance import SOURCE_FREEZE
from test_rollback_orchestrator import _prepared_manifest
from test_rollback_orchestrator import _evidence as _private_evidence
from test_cutover_orchestrator import _source_capability
from test_writer_barrier import SETUP

KNOWLEDGE = os.environ.get('KNOWLEDGE_TEST_PYTHON')
installed = pytest.mark.skipif(not KNOWLEDGE, reason='Explicit independent Knowledge installation required')
DOMAINS = ('session_harness', 'knowledge_catalog', 'connector_jobs')


@pytest.fixture(scope='module')
def staged(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('window-rollback-manifest')
    return _staging(tmp)


def _cutover_manifest(staged, tmp_path, name='m'):
    _, output = _prepared(staged, tmp_path, name)
    harness_journal, knowledge_journal = _journals(tmp_path, output)
    manifests.cutover_installation(output, harness_journal=harness_journal,
                                   knowledge_journal=knowledge_journal,
                                   source_freeze_receipt_sha256=SOURCE_FREEZE)
    return output


def test_window_rollback_advance_flips_writers_and_preserves_history(staged, tmp_path):
    output = _cutover_manifest(staged, tmp_path)
    before = _manifest(output)
    evidence = _evidence(tmp_path)
    commitment = 'sha256:' + hashlib.sha256(evidence.read_bytes()).hexdigest()
    result = manifests.rollback_installation(output, rollback_evidence=evidence)
    assert result['state'] == 'ROLLED_BACK' and result['idempotent'] is False
    assert result['activation_allowed'] is False and result['installation_cutover_performed'] is False
    assert result['rollback_completed'] is False
    manifest = _manifest(output)
    assert manifest['state'] == 'ROLLED_BACK'
    assert manifest['active_writers'] == {domain: 'puddingclaw' for domain in DOMAINS}
    assert manifest['rollback_evidence_digest'] == commitment
    assert manifest['rollback_window_open'] is True and manifest['completed_at'] is None
    # The cutover registrations, committed revision and preparation evidence are
    # carried as history; the rev4 events stay journal-level and nothing new is
    # registered.
    assert manifest['checkpoint'] == before['checkpoint']
    assert manifest['active_installation_revision'] == before['active_installation_revision']
    assert manifest['started_at'] == before['started_at']
    assert manifest['staging_namespace'] == before['staging_namespace']
    validate_manifest(manifest)
    assert result['manifest_digest'] == 'sha256:' + hashlib.sha256(output.read_bytes()).hexdigest()
    rolled_bytes = output.read_bytes()
    again = manifests.rollback_installation(output, rollback_evidence=evidence)
    assert again['idempotent'] is True and again['manifest_digest'] == result['manifest_digest']
    assert output.read_bytes() == rolled_bytes
    assert _manifest(output)['checkpoint'] == before['checkpoint']
    conflict = _evidence(tmp_path, 'conflict-evidence.json')
    with pytest.raises(ValueError, match='does not match'):
        manifests.rollback_installation(output, rollback_evidence=conflict)
    assert output.read_bytes() == rolled_bytes


def test_window_rollback_idempotent_reentry_refuses_writer_drift(staged, tmp_path):
    output = _cutover_manifest(staged, tmp_path)
    evidence = _evidence(tmp_path)
    manifests.rollback_installation(output, rollback_evidence=evidence)
    manifest = _manifest(output)
    manifest['active_writers']['knowledge_catalog'] = 'puddingknowledge'
    _rewrite(output, manifest)
    before = output.read_bytes()
    with pytest.raises(ValueError, match='Active writer drift'):
        manifests.rollback_installation(output, rollback_evidence=evidence)
    assert output.read_bytes() == before


def test_window_rollback_advance_requires_cutover_invariants(staged, tmp_path):
    _, output = _prepared(staged, tmp_path)
    # A state label without the cutover invariants (flipped writers, both
    # registered assigned events) is not a CUTOVER manifest and never advances.
    manifest = _manifest(output)
    manifest['state'] = 'CUTOVER'
    _rewrite(output, manifest)
    evidence = _evidence(tmp_path)
    before = output.read_bytes()
    with pytest.raises(ValueError):
        manifests.rollback_installation(output, rollback_evidence=evidence)
    assert output.read_bytes() == before
    assert 'rollback_evidence_digest' not in _manifest(output)


def test_window_rollback_advance_crash_resumes_idempotently(staged, tmp_path):
    output = _cutover_manifest(staged, tmp_path)
    evidence = _evidence(tmp_path)
    states = []

    def crash(state):
        states.append(state)
        raise RuntimeError('crash after commit')

    with pytest.raises(RuntimeError):
        manifests.rollback_installation(output, rollback_evidence=evidence, _after_checkpoint=crash)
    assert states == ['ROLLED_BACK'] and _manifest(output)['state'] == 'ROLLED_BACK'
    assert manifests.rollback_installation(output, rollback_evidence=evidence)['idempotent'] is True


def test_finalize_after_window_rollback_fails_closed(staged, tmp_path):
    output = _cutover_manifest(staged, tmp_path)
    evidence = _evidence(tmp_path)
    manifests.rollback_installation(output, rollback_evidence=evidence)
    before = output.read_bytes()
    with pytest.raises(ValueError, match='FINALIZED'):
        manifests.finalize_installation(output)
    assert output.read_bytes() == before
    assert _manifest(output)['state'] == 'ROLLED_BACK'


@pytest.fixture
def roots(tmp_path):
    root = tmp_path.resolve()
    home = root / 'harness'
    home.mkdir(mode=0o700)
    authority.enroll(home, root / 'harness-authority', 'enroll-harness')
    subprocess.run([KNOWLEDGE, '-c', SETUP, str(root)], check=True, cwd=root)
    manifest = _prepared_manifest(root / 'manifest')
    evidence = _private_evidence(root / 'evidence')
    source = root / 'source'
    source.mkdir(mode=0o700)
    _source_capability(source)
    return (home, root / 'knowledge', KNOWLEDGE, root / 'window-checkpoint', manifest,
            evidence, root / 'barrier', root / 'cutover-checkpoint', source)


def _cutover(roots, operation='cutover-1'):
    barrier.suspend_writers(roots[0], roots[1], roots[2], roots[6], operation)
    receipt = publish_source_fence(roots[8], operation)
    manifest = _document(roots)
    manifest['checkpoint']['source_freeze_receipt_sha256'] = (
        'sha256:' + receipt['source_freeze_receipt_sha256'])
    roots[4].write_bytes(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode())
    return cutover.cutover(roots[0], roots[1], roots[2], roots[7], roots[4], operation,
                           source_home=roots[8])


def run(roots, operation='window-1', **kwargs):
    return orchestrator.window_rollback(roots[0], roots[1], roots[2], roots[3], roots[4], roots[5],
                                        operation, **kwargs)


def _document(roots):
    return json.loads(roots[4].read_bytes())


def _evidence_sha(roots):
    return hashlib.sha256(roots[5].read_bytes()).hexdigest()


def _journal(roots, side):
    return json.loads((roots[0].parent / (side + '-authority') / 'journal.json').read_bytes())


def _knowledge_cli(roots, *arguments):
    return subprocess.run([roots[2], '-m', 'knowledge_platform.local.writer_authority',
                           *arguments, '--state-dir', str(roots[1])],
                          capture_output=True, text=True, timeout=120)


@installed
def test_window_rollback_happy_path_exact_retry_and_persistent_freeze(roots):
    cutover_result = _cutover(roots)
    assert not (roots[0] / '.installation-freeze-v1.json').exists()
    result = run(roots)
    assert result['state'] == 'both_reassigned'
    assert result['rollback_completed'] is False
    assert result['activation_allowed'] is False and result['installation_cutover_performed'] is False
    assert result['production_activated'] is False
    cutover_hex = result['cutover_manifest_sha256']
    rolled_back = result['rolled_back_manifest_sha256']
    evidence_sha = _evidence_sha(roots)
    assert result['rollback_evidence_sha256'] == evidence_sha
    assert cutover_hex == cutover_result['cutover_manifest_sha256'] and cutover_hex != rolled_back
    harness_events = result['journals']['harness']['events']
    knowledge_events = result['journals']['knowledge']['events']
    assert len(harness_events) == 5 and len(knowledge_events) == 5
    for events in (harness_events, knowledge_events):
        assert events[2]['state'] == 'assigned' and events[2]['operation_id'] == 'cutover-1'
        assert events[3]['state'] == 'suspended' and events[3]['operation_id'] == 'window-1'
        assert events[3]['previous'] == events[2]['sha256']
        assert events[4]['state'] == 'assigned' and events[4]['operation_id'] == 'window-1'
        assert events[4]['previous'] == events[3]['sha256']
        assert events[4]['freeze_receipt_sha256'] == events[3]['freeze_receipt_sha256']
    harness_head, knowledge_head = harness_events[4], knowledge_events[4]
    assert harness_head['writer'] == 'puddingclaw'
    assert knowledge_head['writers'] == {'knowledge_catalog': 'puddingclaw',
                                         'connector_jobs': 'puddingclaw'}
    for head in (harness_head, knowledge_head):
        assert head['migration_manifest_sha256'] == rolled_back
        assert head['active_installation_revision'] == 'sha256:' + rolled_back
        assert head['rollback_evidence_sha256'] == evidence_sha
    manifest = _document(roots)
    assert manifest['state'] == 'ROLLED_BACK'
    assert manifest['active_writers'] == {domain: 'puddingclaw' for domain in DOMAINS}
    assert manifest['rollback_evidence_digest'] == 'sha256:' + evidence_sha
    assert manifest['rollback_window_open'] is True and manifest['completed_at'] is None
    # History carried from the CUTOVER manifest; nothing new is registered.
    assert manifest['active_installation_revision'] == 'sha256:' + cutover_result['prepared_manifest_sha256']
    assert manifest['checkpoint']['harness_assigned_event_sha256'] == 'sha256:' + harness_events[2]['sha256']
    assert manifest['checkpoint']['knowledge_assigned_event_sha256'] == 'sha256:' + knowledge_events[2]['sha256']
    validate_manifest(manifest)
    assert hashlib.sha256(roots[4].read_bytes()).hexdigest() == rolled_back
    # No thaw: both freeze markers are re-applied under the rev3 receipts and
    # the retired rev2 artifacts and active pointer stay as history.
    harness_marker = (roots[0] / '.installation-freeze-v1.json').read_bytes()
    knowledge_marker = (roots[1] / '.workspace-freeze-v1.json').read_bytes()
    assert hashlib.sha256(harness_marker).hexdigest() == harness_events[3]['freeze_receipt_sha256']
    assert hashlib.sha256(knowledge_marker).hexdigest() == knowledge_events[3]['freeze_receipt_sha256']
    assert (roots[0].parent / 'harness-authority' / 'freeze-marker-rev2.json').exists()
    assert (roots[0].parent / 'harness-authority' / 'thaw-receipt-rev2.json').exists()
    assert (roots[1].parent / 'knowledge-authority' / 'freeze-marker-rev2.json').exists()
    assert (roots[1].parent / 'knowledge-authority' / 'thaw-receipt-rev2.json').exists()
    assert not (roots[0].parent / 'harness-authority' / 'freeze-marker-rev4.json').exists()
    assert not (roots[0].parent / 'harness-authority' / 'thaw-receipt-rev4.json').exists()
    pointer = (roots[0] / 'active-installation.json').read_bytes()
    assert hashlib.sha256(pointer).hexdigest() == cutover_result['active_pointer_sha256']
    assert hashlib.sha256((roots[3] / 'cutover-manifest.json').read_bytes()).hexdigest() == cutover_hex
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
        authority.thaw(roots[0], roots[4], 'window-1')
    status = _knowledge_cli(roots, 'status')
    assert status.returncode == 0
    assert json.loads(status.stdout)['journal'] == result['journals']['knowledge']
    # ROLLED_BACK stays fail-closed for finalize and re-cutover.
    with pytest.raises(ValueError, match='FINALIZED'):
        manifests.finalize_installation(roots[4])
    with pytest.raises(ValueError, match='CUTOVER'):
        cutover.finalize_cutover(roots[4], roots[7])
    with pytest.raises(ValueError):
        cutover.cutover(roots[0], roots[1], roots[2], roots[0].parent / 'cutover-checkpoint-2',
                        roots[4], 'cutover-2', source_home=roots[8])
    assert not (roots[0].parent / 'cutover-checkpoint-2' / 'checkpoint.json').exists()
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    assert run(roots) == result
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes


@installed
@pytest.mark.parametrize('crash_at', orchestrator._WINDOW_ORDER)
def test_crash_at_every_checkpoint_resumes_identically(roots, crash_at):
    _cutover(roots)

    def crash(state):
        if state == crash_at:
            raise RuntimeError('injected crash')

    with pytest.raises(RuntimeError):
        run(roots, _after_checkpoint=crash)
    assert json.loads((roots[3] / 'checkpoint.json').read_bytes())['state'] == crash_at
    assert (roots[0] / '.installation-freeze-v1.json').exists()
    harness_events = _journal(roots, 'harness')['events']
    knowledge_events = _journal(roots, 'knowledge')['events']
    if crash_at == 'harness_resuspended':
        assert len(harness_events) == 4 and len(knowledge_events) == 3
        assert not (roots[1] / '.workspace-freeze-v1.json').exists()
        assert _document(roots)['state'] == 'CUTOVER'
    else:
        assert (roots[1] / '.workspace-freeze-v1.json').exists()
    if crash_at == 'both_resuspended':
        assert len(harness_events) == 4 and len(knowledge_events) == 4
        assert _document(roots)['state'] == 'CUTOVER'
    if crash_at == 'manifest_rolled_back':
        assert len(harness_events) == 4 and len(knowledge_events) == 4
        assert _document(roots)['state'] == 'ROLLED_BACK'
    if crash_at == 'harness_reassigned':
        assert len(harness_events) == 5 and len(knowledge_events) == 4
        assert _document(roots)['state'] == 'ROLLED_BACK'
    if crash_at == 'both_reassigned':
        assert len(harness_events) == 5 and len(knowledge_events) == 5
        assert _document(roots)['state'] == 'ROLLED_BACK'
    result = run(roots)
    assert result['state'] == 'both_reassigned' and result['rollback_completed'] is False
    assert _document(roots)['state'] == 'ROLLED_BACK'
    assert (roots[0] / '.installation-freeze-v1.json').exists()
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    assert run(roots) == result
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes


@installed
def test_precommitted_resuspension_is_an_exact_retry(roots):
    _cutover(roots)
    # A crash before the first checkpoint may leave both rev3 suspensions
    # committed; the run consumes them without duplicating an event.
    authority.suspend(roots[0], 'window-1')
    suspended = _knowledge_cli(roots, 'suspend', '--operation-id', 'window-1')
    assert suspended.returncode == 0, suspended.stderr
    harness_journal = _journal(roots, 'harness')
    knowledge_journal = _journal(roots, 'knowledge')
    assert len(harness_journal['events']) == 4 and len(knowledge_journal['events']) == 4
    result = run(roots)
    assert result['state'] == 'both_reassigned'
    assert result['journals']['harness']['events'][:4] == harness_journal['events']
    assert result['journals']['knowledge']['events'][:4] == knowledge_journal['events']
    assert run(roots) == result


@installed
def test_delegation_failure_leaves_knowledge_suspended(roots, monkeypatch):
    _cutover(roots)
    original = orchestrator._delegate

    def fail(command, *args, **kwargs):
        if 'suspend' in command:
            raise ValueError('injected delegation failure')
        return original(command, *args, **kwargs)

    monkeypatch.setattr(orchestrator, '_delegate', fail)
    with pytest.raises(ValueError, match='injected delegation failure'):
        run(roots)
    assert json.loads((roots[3] / 'checkpoint.json').read_bytes())['state'] == 'harness_resuspended'
    assert _document(roots)['state'] == 'CUTOVER'
    assert (roots[0] / '.installation-freeze-v1.json').exists()
    assert not (roots[1] / '.workspace-freeze-v1.json').exists()
    knowledge_journal = _journal(roots, 'knowledge')
    assert knowledge_journal['events'][-1]['state'] == 'assigned'

    def fail_assign(command, *args, **kwargs):
        if 'assign' in command:
            raise ValueError('injected delegation failure')
        return original(command, *args, **kwargs)

    monkeypatch.setattr(orchestrator, '_delegate', fail_assign)
    with pytest.raises(ValueError, match='injected delegation failure'):
        run(roots)
    assert json.loads((roots[3] / 'checkpoint.json').read_bytes())['state'] == 'harness_reassigned'
    assert _document(roots)['state'] == 'ROLLED_BACK'
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
    knowledge_journal = _journal(roots, 'knowledge')
    assert knowledge_journal['events'][-1]['state'] == 'suspended'
    monkeypatch.setattr(orchestrator, '_delegate', original)
    assert run(roots)['state'] == 'both_reassigned'


@installed
def test_successful_child_with_invalid_receipt_cannot_complete_rollback(roots, monkeypatch):
    _cutover(roots)
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
    # The delegated child really committed its rev4 assignment; only the
    # receipt was invalid, so completion is never published and retry resumes
    # the window.
    assert json.loads((roots[3] / 'checkpoint.json').read_bytes())['state'] == 'harness_reassigned'
    knowledge_journal = _journal(roots, 'knowledge')
    assert knowledge_journal['events'][-1]['state'] == 'assigned'
    assert len(knowledge_journal['events']) == 5
    assert (roots[1] / '.workspace-freeze-v1.json').exists()
    assert _document(roots)['state'] == 'ROLLED_BACK'
    monkeypatch.setattr(orchestrator, '_delegate', original)
    assert run(roots)['state'] == 'both_reassigned'


@installed
def test_wrong_operation_ids_fail_closed(roots):
    _cutover(roots)
    manifest_bytes = roots[4].read_bytes()
    # The window rollback runs under a NEW operation, never the cutover's.
    with pytest.raises(ValueError, match='new operation'):
        run(roots, operation='cutover-1')
    assert roots[4].read_bytes() == manifest_bytes
    assert not (roots[3] / 'checkpoint.json').exists()
    # A resuspension under another operation is not consumed.  The checkpoint
    # directory above is now bound to the rejected cutover operation, so this
    # attempt gets a fresh one.
    other_checkpoint = roots[0].parent / 'window-checkpoint-2'
    authority.suspend(roots[0], 'other-operation')
    suspended = _knowledge_cli(roots, 'suspend', '--operation-id', 'other-operation')
    assert suspended.returncode == 0, suspended.stderr
    with pytest.raises(ValueError, match='another operation'):
        orchestrator.window_rollback(roots[0], roots[1], roots[2], other_checkpoint,
                                     roots[4], roots[5], 'window-1')
    assert roots[4].read_bytes() == manifest_bytes
    assert not (other_checkpoint / 'checkpoint.json').exists()
    assert _journal(roots, 'harness')['events'][-1]['operation_id'] == 'other-operation'


@installed
def test_foreign_rev4_assignment_fails_closed(roots):
    _cutover(roots)
    cutover_bytes = roots[4].read_bytes()
    # An out-of-band window rollback commits both rev4 assignments under
    # another operation.  The manifest it advanced is already ROLLED_BACK, so
    # this orchestrator refuses at the manifest gate...
    manifests.rollback_installation(roots[4], rollback_evidence=roots[5])
    authority.suspend(roots[0], 'foreign-window')
    suspended = _knowledge_cli(roots, 'suspend', '--operation-id', 'foreign-window')
    assert suspended.returncode == 0, suspended.stderr
    authority.assign(roots[0], roots[4], 'foreign-window', 'puddingclaw',
                     rollback_evidence=roots[5])
    assigned = _knowledge_cli(roots, 'assign', '--operation-id', 'foreign-window',
                              '--manifest', str(roots[4]), '--writer', 'puddingclaw',
                              '--rollback-evidence', str(roots[5]))
    assert assigned.returncode == 0, assigned.stderr
    assert len(_journal(roots, 'harness')['events']) == 5
    with pytest.raises(ValueError, match='requires a CUTOVER installation manifest'):
        run(roots)
    assert not (roots[3] / 'checkpoint.json').exists()
    # ...and with the CUTOVER bytes restored, the foreign rev4 heads reject as
    # having no checkpoint, even when the caller reuses the foreign operation.
    roots[4].write_bytes(cutover_bytes)
    with pytest.raises(ValueError, match='no window rollback checkpoint'):
        run(roots)
    assert not (roots[3] / 'checkpoint.json').exists()
    foreign_checkpoint = roots[0].parent / 'window-checkpoint-3'
    with pytest.raises(ValueError, match='no window rollback checkpoint'):
        orchestrator.window_rollback(roots[0], roots[1], roots[2], foreign_checkpoint,
                                     roots[4], roots[5], 'foreign-window')
    assert not (foreign_checkpoint / 'checkpoint.json').exists()
    assert _journal(roots, 'knowledge')['events'][-1]['operation_id'] == 'foreign-window'


@installed
def test_manifest_not_cutover_fails_closed(roots):
    # PREPARED belongs to the pre-cutover abort path and refuses here.
    manifest_bytes = roots[4].read_bytes()
    with pytest.raises(ValueError, match='requires a CUTOVER installation manifest'):
        run(roots)
    assert roots[4].read_bytes() == manifest_bytes
    assert not (roots[3] / 'checkpoint.json').exists()
    # FINALIZED closed the rollback window and refuses.
    _cutover(roots)
    manifests.finalize_installation(roots[4])
    with pytest.raises(ValueError, match='requires a CUTOVER installation manifest'):
        run(roots)
    assert not (roots[3] / 'checkpoint.json').exists()


@installed
def test_rolled_back_manifest_with_fresh_checkpoint_refuses(roots):
    _cutover(roots)
    assert run(roots)['state'] == 'both_reassigned'
    with pytest.raises(ValueError, match='requires a CUTOVER installation manifest'):
        orchestrator.window_rollback(roots[0], roots[1], roots[2],
                                     roots[0].parent / 'window-checkpoint-2',
                                     roots[4], roots[5], 'window-2')


@installed
def test_evidence_digest_drift_between_runs_fails_closed(roots):
    _cutover(roots)

    def stop(state):
        if state == 'manifest_rolled_back':
            raise RuntimeError('stop after manifest advance')

    original = roots[5].read_bytes()
    with pytest.raises(RuntimeError):
        run(roots, _after_checkpoint=stop)
    roots[5].write_bytes(b'"drifted-evidence"\n')
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    with pytest.raises(ValueError, match='Window rollback plan changed'):
        run(roots)
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes
    roots[5].write_bytes(original)
    assert run(roots)['state'] == 'both_reassigned'


@installed
@pytest.mark.parametrize('target', [0, 1])
def test_freeze_marker_vanished_after_resuspension_fails_closed(roots, target):
    _cutover(roots)

    def stop(state):
        if state == 'both_resuspended':
            raise RuntimeError('stop after resuspension')

    with pytest.raises(RuntimeError):
        run(roots, _after_checkpoint=stop)
    marker = roots[target] / ('.installation-freeze-v1.json' if target == 0 else '.workspace-freeze-v1.json')
    checkpoint_bytes = (roots[3] / 'checkpoint.json').read_bytes()
    marker.unlink()
    with pytest.raises((ValueError, OSError)):
        run(roots)
    assert not marker.exists()
    assert (roots[3] / 'checkpoint.json').read_bytes() == checkpoint_bytes


@installed
def test_cli_window_rollback_and_fail_closed_error(roots):
    _cutover(roots)
    env = dict(os.environ)
    if os.environ.get('HARNESS_TEST_INSTALLED') == '1':
        env.pop('PYTHONPATH', None)
    else:
        env['PYTHONPATH'] = str(Path(__file__).parents[1])

    def cli(*arguments):
        return subprocess.run([sys.executable, '-m', 'harness.rollback_orchestrator', *arguments],
                              env=env, cwd='/private/tmp', capture_output=True, text=True, timeout=120)

    bad = cli('window-rollback', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
              '--knowledge-python', str(roots[2]), '--checkpoint-dir',
              str(roots[0].parent / 'checkpoint-bad'), '--manifest', str(roots[4]),
              '--rollback-evidence', str(roots[5]), '--operation-id', 'cutover-1')
    assert bad.returncode == 1
    report = json.loads(bad.stdout)
    assert report['format'] == 'puddingharness-window-rollback-orchestrator/v1'
    assert report['error_code'] == 'installation_rollback_rejected'
    assert report['activation_allowed'] is False and report['installation_cutover_performed'] is False
    assert report['rollback_completed'] is False and report['production_activated'] is False
    value = cli('window-rollback', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
                '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(roots[3]),
                '--manifest', str(roots[4]), '--rollback-evidence', str(roots[5]),
                '--operation-id', 'window-1')
    assert value.returncode == 0, value.stderr + value.stdout
    report = json.loads(value.stdout)
    assert report['format'] == 'puddingharness-window-rollback-orchestrator/v1'
    assert report['state'] == 'both_reassigned' and report['rollback_completed'] is False
    assert report['activation_allowed'] is False and report['installation_cutover_performed'] is False
    assert report['production_activated'] is False
    retry = cli('window-rollback', '--harness-home', str(roots[0]), '--knowledge-state', str(roots[1]),
                '--knowledge-python', str(roots[2]), '--checkpoint-dir', str(roots[3]),
                '--manifest', str(roots[4]), '--rollback-evidence', str(roots[5]),
                '--operation-id', 'window-1')
    assert retry.returncode == 0 and json.loads(retry.stdout) == report
