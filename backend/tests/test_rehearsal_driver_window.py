"""Window-rollback rehearsal scenario tests: the real reverse chain.

The cross-product tests drive the shipped chain CLIs — forward through
cutover, then the Knowledge-era delta, the window fence, the frozen export,
the other-catalog disposition, the document reverse bundle, the absent-wiki
attestation, the rollback evidence assembly and the window rollback itself —
against a synthetic legacy Home and require an explicit independent Knowledge
interpreter (KNOWLEDGE_TEST_PYTHON); the step-table unit test is Harness-only.
"""
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import time

from harness import rehearsal_driver as driver
from test_rehearsal_driver import (
    KNOWLEDGE,
    _assert_sealed_tree,
    _checkpoint_steps,
    _corpus,
    _documents,
    _driver_command,
    _env,
    _legacy_home,
    _report,
    _run_driver,
    _tree_digest,
    installed,
)

WINDOW_STEP_NAMES = [name for name, _, _ in driver.WINDOW_STEPS]
DOMAINS = ('session_harness', 'knowledge_catalog', 'connector_jobs')


def _run_window(work, home, corpus, *extra):
    return _run_driver(work, home, corpus, '--scenario', 'window-rollback', *extra)


def _kill_mid_chain(work, home, corpus, committed, *extra):
    child = subprocess.Popen(_driver_command(work, home, corpus,
                                             '--scenario', 'window-rollback', *extra),
                             env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    checkpoint = work / 'driver-checkpoint.json'
    deadline = time.monotonic() + 240
    count = 0
    while child.poll() is None and time.monotonic() < deadline:
        if checkpoint.exists():
            try:
                count = len(json.loads(checkpoint.read_bytes())['steps'])
            except (ValueError, OSError):
                count = 0
        if count >= committed:
            break
        time.sleep(0.02)
    assert child.poll() is None and count >= committed
    # The whole process group dies, so no delegated child retains a lock.
    os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL
    return _checkpoint_steps(work)


def test_window_step_table_extends_the_forward_chain_through_cutover():
    assert WINDOW_STEP_NAMES[:8] == [name for name, _, _ in driver.STEPS[:8]]
    assert WINDOW_STEP_NAMES[8:] == ['window-delta', 'window-suspend', 'window-export',
                                     'window-disposition', 'window-document-reverse',
                                     'window-wiki-absent', 'window-evidence',
                                     'window-rollback']
    assert len(set(WINDOW_STEP_NAMES)) == len(WINDOW_STEP_NAMES)
    for name, roots, body in driver.WINDOW_STEPS:
        assert roots and callable(body)
    assert driver._WINDOW_TOP_LEVEL > driver._KNOWN_TOP_LEVEL


@installed
def test_full_window_rollback_run_rolls_back_with_real_chain_output(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, connection = _legacy_home(base, _documents(corpus))
    assert connection is None
    before = _tree_digest(home)
    work = base / 'work'
    report = _report(_run_window(work, home, corpus))
    assert report['format'] == driver.FORMAT and report['status'] == 'rolled_back'
    assert report['terminal_state'] == 'ROLLED_BACK'
    assert report['operation'] == 'rehearsal-1'
    assert report['window_operation'] == 'rehearsal-1-window'
    assert report['installation_id'] == 'install-rehearsal'
    assert report['activation_allowed'] is False
    assert report['installation_cutover_performed'] is True
    assert report['rollback_completed'] is True and report['production_activated'] is False
    assert [step['name'] for step in report['steps']] == WINDOW_STEP_NAMES
    # A fresh run really executes every step; none is a replay.
    assert [(step['status'], step['idempotent']) for step in report['steps']] == [
        ('executed', False)] * len(WINDOW_STEP_NAMES)
    record_raw = (work / 'run-record.json').read_bytes()
    record = json.loads(record_raw)
    assert driver._encoded(record) == record_raw
    assert record['format'] == driver.RUN_FORMAT
    assert report['run_record_digest'] == 'sha256:' + hashlib.sha256(record_raw).hexdigest()
    assert [step['name'] for step in record['steps']] == WINDOW_STEP_NAMES
    terminal = record['terminal']
    assert terminal['manifest_state'] == 'ROLLED_BACK'
    assert terminal['rollback_window_open'] is True
    assert terminal['manifest_digest'] == report['manifest_digest']
    manifest_raw = (work / 'manifest/manifest.json').read_bytes()
    manifest = json.loads(manifest_raw)
    assert manifest['state'] == 'ROLLED_BACK'
    assert manifest['active_writers'] == {domain: 'puddingclaw' for domain in DOMAINS}
    assert manifest['rollback_window_open'] is True and manifest['completed_at'] is None
    assert 'sha256:' + hashlib.sha256(manifest_raw).hexdigest() == terminal['manifest_digest']
    evidence_raw = (work / 'evidence/evidence.json').read_bytes()
    evidence_sha = hashlib.sha256(evidence_raw).hexdigest()
    assert manifest['rollback_evidence_digest'] == 'sha256:' + evidence_sha
    assert terminal['rollback_evidence_digest'] == 'sha256:' + evidence_sha
    steps = _checkpoint_steps(work)
    # The window operation binds the suspension plans, the evidence and both
    # rollback journals; the cutover operation stays in rev1/rev2 only.
    delta = steps[8]['receipt']
    assert delta['title'] == driver._WINDOW_DELTA_TITLE
    assert steps[14]['receipt']['operation_id'] == 'rehearsal-1-window'
    assert steps[14]['receipt']['evidence_sha256'] == evidence_sha
    # The Knowledge-era edit survived the whole reverse chain: the reversed
    # legacy candidate carries exactly the edited title and the untouched ones.
    reversed_catalog = work / 'document-reverse/reverse/catalog.sqlite3'
    with sqlite3.connect(f'file:{reversed_catalog}?mode=ro', uri=True) as catalog:
        titles = [title for title, in catalog.execute('SELECT title FROM knowledge_documents')]
    assert len(titles) == 3
    assert titles.count(driver._WINDOW_DELTA_TITLE) == 1
    remaining = [title for title in titles if title != driver._WINDOW_DELTA_TITLE]
    assert len(set(remaining)) == 2 and set(remaining) < {'doc-1', 'doc-2', 'doc-3'}
    # The lineage stand-in stayed byte-identical to the real workspace.
    assert hashlib.sha256((work / 'lineage/target-before.sqlite3').read_bytes()).hexdigest() == \
        delta['catalog_sha256_before']
    assert hashlib.sha256((work / 'lineage/target-after.sqlite3').read_bytes()).hexdigest() == \
        hashlib.sha256((work / 'knowledge-home/catalog.sqlite3').read_bytes()).hexdigest() == \
        delta['catalog_sha256_after']
    # Terminal writer state on disk: rolled back, both products fenced, the
    # retired cutover artifacts and the active pointer preserved as history.
    assert (work / 'harness-home/.installation-freeze-v1.json').exists()
    assert (work / 'knowledge-home/.workspace-freeze-v1.json').exists()
    assert (work / 'harness-home/active-installation.json').exists()
    journals = {}
    for side in ('harness-authority', 'knowledge-authority'):
        assert (work / side / 'freeze-marker-rev2.json').exists()
        assert (work / side / 'thaw-receipt-rev2.json').exists()
        assert not (work / side / 'freeze-marker-rev4.json').exists()
        assert not (work / side / 'thaw-receipt-rev4.json').exists()
        journals[side] = json.loads((work / side / 'journal.json').read_bytes())['events']
    for events in journals.values():
        assert len(events) == 5
        assert events[2]['state'] == 'assigned' and events[2]['operation_id'] == 'rehearsal-1'
        assert events[3]['state'] == 'suspended'
        assert events[3]['operation_id'] == 'rehearsal-1-window'
        assert events[3]['previous'] == events[2]['sha256']
        assert events[4]['state'] == 'assigned'
        assert events[4]['operation_id'] == 'rehearsal-1-window'
        assert events[4]['previous'] == events[3]['sha256']
        assert events[4]['freeze_receipt_sha256'] == events[3]['freeze_receipt_sha256']
    assert journals['harness-authority'][4]['writer'] == 'puddingclaw'
    assert journals['knowledge-authority'][4]['writers'] == {
        'knowledge_catalog': 'puddingclaw', 'connector_jobs': 'puddingclaw'}
    for events in journals.values():
        assert events[4]['rollback_evidence_sha256'] == evidence_sha
    cutover_copy = (work / 'window-checkpoint/cutover-manifest.json').read_bytes()
    rolled_copy = (work / 'window-checkpoint/rolled-back-manifest.json').read_bytes()
    assert rolled_copy == manifest_raw
    assert json.loads(cutover_copy)['state'] == 'CUTOVER'
    assert json.loads(cutover_copy)['checkpoint'] == manifest['checkpoint']
    # The work root is exactly the documented window layout: owned,
    # symlink-free, files private, directories sealed to the owner.
    assert {entry.name for entry in work.iterdir()} == driver._WINDOW_TOP_LEVEL
    _assert_sealed_tree(work)
    # The source home is strictly read-only across the whole rehearsal.
    assert _tree_digest(home) == before


@installed
def test_sigkill_mid_reverse_chain_resumes_to_a_byte_identical_record(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, _ = _legacy_home(base, _documents(corpus))
    work = base / 'work'
    killed = _kill_mid_chain(work, home, corpus, 12)
    assert 12 <= len(killed) < len(WINDOW_STEP_NAMES)
    resumed = _report(_run_window(work, home, corpus))
    assert [step['name'] for step in resumed['steps']] == WINDOW_STEP_NAMES
    assert [step['name'] for step in resumed['steps'] if step['status'] == 'verified'] == [
        step['name'] for step in killed]
    assert [step['name'] for step in resumed['steps'] if step['status'] == 'executed'] == \
        WINDOW_STEP_NAMES[len(killed):]
    record_raw = (work / 'run-record.json').read_bytes()
    again = _report(_run_window(work, home, corpus))
    assert [(step['status'], step['idempotent']) for step in again['steps']] == [
        ('verified', True)] * len(WINDOW_STEP_NAMES)
    assert (work / 'run-record.json').read_bytes() == record_raw


@installed
def test_window_reverse_resolves_relative_references_through_layout_aliases(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    deep = corpus / 'imported/deep'
    deep.mkdir()
    body = b'# Note\n\n![figure](../../assets/figure.png)\n'
    (deep / 'note.md').write_bytes(body)
    (corpus / 'assets/figure.png').write_bytes(b'figure')
    documents = _documents(corpus) + [
        {'id': 'doc-4', 'source_path': str(deep / 'note.md'),
         'storage_path': str(deep / 'note.md'),
         'content_sha256': hashlib.sha256(body).hexdigest()},
    ]
    home, connection = _legacy_home(base, documents)
    assert connection is None
    work = base / 'work'
    report = _report(_run_window(work, home, corpus))
    assert report['status'] == 'rolled_back' and report['rollback_completed'] is True
    aliases = json.loads((work / 'document-reverse/resolution-aliases.json').read_bytes())
    assert 'resources/external/knowledge/imported/deep/note.md' in aliases.values()
    # The relative reference rode the alias into the migrated resources tree...
    assert (work / 'knowledge-home/resources/external/knowledge/assets/figure.png'
            ).read_bytes() == b'figure'
    # ...and the reverse bundle materialized it from there.
    assert (work / 'document-reverse/reverse/bodies/resources/external/knowledge/assets/figure.png'
            ).read_bytes() == b'figure'


@installed
def test_duplicate_window_run_verifies_each_step_and_republishes_identical_record(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, _ = _legacy_home(base, _documents(corpus))
    work = base / 'work'
    first = _report(_run_window(work, home, corpus))
    record_raw = (work / 'run-record.json').read_bytes()
    checkpoint_raw = (work / 'driver-checkpoint.json').read_bytes()
    second = _report(_run_window(work, home, corpus))
    assert [(step['name'], step['status'], step['idempotent']) for step in second['steps']] == [
        (name, 'verified', True) for name in WINDOW_STEP_NAMES]
    assert second['run_record_digest'] == first['run_record_digest']
    assert second['manifest_digest'] == first['manifest_digest']
    assert (work / 'run-record.json').read_bytes() == record_raw
    assert (work / 'driver-checkpoint.json').read_bytes() == checkpoint_raw


@installed
def test_tampered_window_evidence_refuses_before_any_step(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, _ = _legacy_home(base, _documents(corpus))
    work = base / 'work'
    killed = _kill_mid_chain(work, home, corpus, 15)
    assert 15 <= len(killed) < len(WINDOW_STEP_NAMES)
    checkpoint_raw = (work / 'driver-checkpoint.json').read_bytes()
    target = work / 'evidence/evidence.json'
    data = target.read_bytes()
    target.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    completed = _run_window(work, home, corpus)
    assert completed.returncode == 1
    assert len(completed.stdout.strip().splitlines()) == 1
    error = json.loads(completed.stdout)
    assert error['format'] == driver.FORMAT and error['status'] == 'error'
    assert error['error_code'] == 'rehearsal_driver_rejected'
    assert error['step'] is None and error['step_error_code'] is None
    assert error['activation_allowed'] is False
    assert error['installation_cutover_performed'] is False
    assert error['rollback_completed'] is False and error['production_activated'] is False
    assert (work / 'driver-checkpoint.json').read_bytes() == checkpoint_raw
    assert not (work / 'run-record.json').exists()


@installed
def test_window_operation_validation_refuses_closed(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, _ = _legacy_home(base, _documents(corpus))
    work = base / 'work'
    # The window operation must differ from the cutover operation.
    completed = _run_window(work, home, corpus, '--window-operation', 'rehearsal-1')
    assert completed.returncode == 1
    error = json.loads(completed.stdout)
    assert error['error_code'] == 'rehearsal_driver_rejected' and error['step'] is None
    assert not (work / 'driver-checkpoint.json').exists()
    # Path A takes no window operation.
    completed = _run_driver(work, home, corpus, '--window-operation', 'other-1')
    assert completed.returncode == 1
    error = json.loads(completed.stdout)
    assert error['error_code'] == 'rehearsal_driver_rejected' and error['step'] is None
    assert not (work / 'driver-checkpoint.json').exists()
    # Changing the window operation after steps committed refuses and leaves
    # the checkpoint untouched.
    killed = _kill_mid_chain(work, home, corpus, 1)
    assert len(killed) >= 1
    checkpoint_raw = (work / 'driver-checkpoint.json').read_bytes()
    completed = _run_window(work, home, corpus, '--window-operation', 'changed-1')
    assert completed.returncode == 1
    error = json.loads(completed.stdout)
    assert error['error_code'] == 'rehearsal_driver_rejected' and error['step'] is None
    assert (work / 'driver-checkpoint.json').read_bytes() == checkpoint_raw


def test_knowledge_interpreter_is_explicit():
    assert KNOWLEDGE is None or os.path.exists(KNOWLEDGE)
