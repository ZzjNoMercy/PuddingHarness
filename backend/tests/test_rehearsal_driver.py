"""Rehearsal driver tests: the real forward chain as ordered subprocesses.

The cross-product tests drive the shipped chain CLIs against a synthetic
legacy Home and require an explicit independent Knowledge interpreter
(KNOWLEDGE_TEST_PYTHON); the checkpoint and hygiene unit tests are
Harness-only and never touch a Knowledge installation.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from harness import rehearsal_driver as driver

KNOWLEDGE = os.environ.get('KNOWLEDGE_TEST_PYTHON')
CLAW = os.environ.get('CLAW_TEST_PYTHON')
OWNER = 'rehearsal-owner'
installed = pytest.mark.skipif(
    not KNOWLEDGE or not CLAW,
    reason='Explicit independent Knowledge and PuddingClaw installations required',
)

BACKEND = Path(__file__).resolve().parents[1]
STEP_NAMES = [name for name, _, _ in driver.STEPS]

BODY = b'# Readme\n\nportable content\n'
PDF_BODY = b'# Parsed paper\n\nconverted body\n'
VISION = b'# Vision note\n'
CHART = b'chart-bytes'
EXTRA = b'extra\n'


def _pdf_bytes():
    # A complete one-page PDF with a valid xref table; no parser/model service.
    # Mirrors the Knowledge test helper, which cannot be imported cross-repo.
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>']
    data = b'%PDF-1.4\n'
    offsets = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(number).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(data)
    data += b'xref\n0 4\n0000000000 65535 f \n'
    for offset in offsets:
        data += f'{offset:010d} 00000 n \n'.encode()
    return data + b'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'


PDF = _pdf_bytes()


def _env():
    env = dict(os.environ)
    if os.environ.get('HARNESS_TEST_INSTALLED') == '1':
        env.pop('PYTHONPATH', None)
    else:
        env['PYTHONPATH'] = str(BACKEND)
    return env


def _corpus(root):
    """External knowledge corpus, grafted into the payload as external/knowledge."""
    corpus = root / 'knowledge-corpus'
    (corpus / 'imported').mkdir(parents=True)
    (corpus / 'assets/empty').mkdir(parents=True)
    for relative, data in {'imported/readme.md': BODY, 'imported/paper.md': PDF_BODY,
                           'imported/paper.pdf': PDF, 'imported/vision.md': VISION,
                           'assets/chart.png': CHART, 'assets/extra.txt': EXTRA}.items():
        (corpus / relative).write_bytes(data)
    return corpus


def _documents(corpus):
    """The Knowledge request fixture shape: one plain document, one
    pdf-representation document and one document with file and directory
    attachment references, all bound into the grafted corpus."""
    legacy = str(corpus)
    return [
        {'id': 'doc-1', 'source_path': legacy + '/imported/readme.md',
         'storage_path': legacy + '/imported/readme.md',
         'content_sha256': hashlib.sha256(BODY).hexdigest()},
        {'id': 'doc-2', 'source_type': 'pdf_mineru', 'source_path': legacy + '/imported/paper.pdf',
         'storage_path': legacy + '/imported/paper.md',
         'content_sha256': hashlib.sha256(PDF).hexdigest(), 'size_bytes': len(PDF_BODY),
         'doc_metadata': {'mode': 'multimodal_pdf', 'original_path': legacy + '/imported/paper.pdf',
                          'original_sha256': hashlib.sha256(PDF).hexdigest(),
                          'markdown_sha256': hashlib.sha256(PDF_BODY).hexdigest()}},
        {'id': 'doc-3', 'source_path': legacy + '/imported/vision.md',
         'storage_path': legacy + '/imported/vision.md',
         'content_sha256': hashlib.sha256(VISION).hexdigest(),
         'doc_metadata': {'assets': [{'path': legacy + '/assets/chart.png',
                                      'sha256': hashlib.sha256(CHART).hexdigest(),
                                      'size_bytes': len(CHART)}],
                          'multimodal': {'image_assets_dir': legacy + '/assets'}}},
    ]


def _catalog(path, documents, *, live):
    connection = sqlite3.connect(path)
    if live:
        connection.execute('PRAGMA journal_mode=WAL')
    connection.executescript(
        """
        CREATE TABLE knowledge_bases (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            created_at DATETIME, updated_at DATETIME
        );
        CREATE TABLE knowledge_documents (
            id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL,
            title TEXT NOT NULL, source_type TEXT, source_path TEXT,
            storage_path TEXT, virtual_path TEXT, mime_type TEXT,
            content_sha256 TEXT, size_bytes INTEGER, status TEXT,
            publish_targets JSON, doc_metadata JSON,
            origin_url TEXT, created_at DATETIME, updated_at DATETIME
        );
        INSERT INTO knowledge_bases VALUES
          ('kb-1', 'Docs', 'migrated', '2026-09-11 00:00:00.000000', '2026-09-11 00:00:00.000000');
        """
    )
    for document in documents:
        connection.execute(
            """INSERT INTO knowledge_documents
            (id, knowledge_base_id, title, source_type, source_path, storage_path,
             virtual_path, mime_type, content_sha256, size_bytes, status,
             publish_targets, doc_metadata, origin_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (document['id'], 'kb-1', document.get('title', document['id']),
             document.get('source_type', 'local'), document.get('source_path'),
             document['storage_path'], document.get('virtual_path', document['id'] + '.md'),
             document.get('mime_type', 'text/markdown'), document['content_sha256'],
             document.get('size_bytes'), 'published', '[]',
             json.dumps(document.get('doc_metadata') or {}), '',
             '2026-09-11 00:00:00.000000', '2026-09-11 00:00:00.000000'),
        )
    connection.commit()
    if not live:
        connection.close()
        return None
    # The open connection keeps the -wal/-shm sidecars live, like a real Home.
    return connection


def _source_capability(home):
    lock = home / '.installation-gate-v1.lock'
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    root_info, lock_info = home.stat(), lock.stat()
    value = {
        'format': 'puddingclaw-installation-admission/v1',
        'home_identity': hashlib.sha256(str(home).encode()).hexdigest(),
        'directory_identity': {'device': root_info.st_dev, 'inode': root_info.st_ino},
        'lock_identity': {'device': lock_info.st_dev, 'inode': lock_info.st_ino},
        'participating_process_admission': True,
        'persistent_source_freeze': True,
    }
    path = home / '.installation-admission-capability-v1.json'
    path.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n')
    path.chmod(0o600)


def _empty_credential_authority(home):
    key = b'\x19' * 32
    key_id = 'sha256:' + hashlib.sha256(key).hexdigest()[:32]
    authority = home / '.vault-keys'
    authority.mkdir(mode=0o700)
    key_path = authority / f'{OWNER}.key'
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    manifest = authority / f'{OWNER}.provider.json'
    manifest.write_text(json.dumps({
        'schema_version': 1, 'owner_user_id': OWNER, 'provider': 'file',
        'key_id': key_id, 'created_at': 1757000000,
    }, sort_keys=True) + '\n')
    manifest.chmod(0o600)
    credentials = home / 'users' / OWNER / 'credentials'
    credentials.mkdir(parents=True, mode=0o700)
    os.chmod(credentials.parent, 0o700)
    nonce = hashlib.sha256(OWNER.encode()).digest()[:12]
    payload = json.dumps({'version': 1, 'credentials': {}},
                         sort_keys=True, separators=(',', ':')).encode()
    aad = f'puddingclaw:v1:{OWNER}:provider-registry:default'.encode()
    envelope = {
        'version': 2, 'algorithm': 'AES-256-GCM', 'key_id': key_id,
        'nonce': base64.b64encode(nonce).decode('ascii'),
        'ciphertext': base64.b64encode(AESGCM(key).encrypt(nonce, payload, aad)).decode('ascii'),
    }
    registry = credentials / 'provider-registry.enc'
    registry.write_text(json.dumps(envelope, sort_keys=True, separators=(',', ':')))
    registry.chmod(0o600)


def _legacy_home(root, documents, *, live=False):
    home = root / 'legacy-home'
    (home / 'db').mkdir(parents=True)
    os.chmod(home, 0o700)
    _source_capability(home)
    _empty_credential_authority(home)
    (home / 'sessions').mkdir()
    (home / 'llm-wiki').mkdir()
    (home / 'config.json').write_text(json.dumps({'cache': {'enabled': False}, 'theme': 'dark'}))
    (home / 'sessions/s1.json').write_bytes(b'{"messages": []}')
    connection = _catalog(home / 'db/catalog.sqlite3', documents, live=live)
    return home, connection


def _tree_digest(root):
    digest = hashlib.sha256()
    for current, directories, names in os.walk(root):
        directories.sort()
        for name in sorted(names):
            if name in {'.installation-gate-v1.lock', '.installation-freeze-v1.json'}:
                continue
            path = Path(current) / name
            digest.update(path.relative_to(root).as_posix().encode() + b'\0')
            digest.update(path.read_bytes() + b'\0')
    return digest.hexdigest()


def _assert_sealed_tree(root):
    # Mirrors the driver tree admission: owned and symlink-free everywhere,
    # files private 0600 single-linked, directories sealed to the owner.
    assert root.lstat().st_mode & 0o777 == 0o700
    for current, directories, names in os.walk(root):
        for name in directories:
            info = (Path(current) / name).lstat()
            assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            assert not info.st_mode & 0o022
        for name in names:
            path = Path(current) / name
            info = path.lstat()
            assert not path.is_symlink()
            assert stat.S_ISREG(info.st_mode) and info.st_mode & 0o777 == 0o600
            assert info.st_nlink == 1 and info.st_uid == os.getuid()


def _driver_command(work, home, corpus, *extra):
    return [sys.executable, '-m', 'harness.rehearsal_driver',
            '--work-root', str(work), '--source-home', str(home),
            '--knowledge-python', KNOWLEDGE,
            '--claw-python', CLAW, '--credential-owner', OWNER,
            '--installation-id', 'install-rehearsal',
            '--source-revision', 'legacy-1', '--source-schema-revision', 'claw-schema-v1',
            '--operation', 'rehearsal-1',
            '--graft', str(corpus) + '=external/knowledge',
            '--map', str(corpus) + '=external/knowledge',
            '--timeout-seconds', '180', *extra]


def _run_driver(work, home, corpus, *extra):
    return subprocess.run(_driver_command(work, home, corpus, *extra),
                          env=_env(), capture_output=True, text=True, timeout=300)


def _report(completed):
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert len(completed.stdout.strip().splitlines()) == 1
    return json.loads(completed.stdout)


def _checkpoint_steps(work):
    return json.loads((work / 'driver-checkpoint.json').read_bytes())['steps']


@installed
def test_full_path_a_run_finalizes_with_real_chain_output(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, connection = _legacy_home(base, _documents(corpus))
    assert connection is None
    before = _tree_digest(home)
    work = base / 'work'
    report = _report(_run_driver(work, home, corpus))
    assert report['format'] == driver.FORMAT and report['status'] == 'finalized'
    assert report['terminal_state'] == 'FINALIZED'
    assert report['operation'] == 'rehearsal-1' and report['installation_id'] == 'install-rehearsal'
    assert report['activation_allowed'] is False
    assert report['installation_cutover_performed'] is True
    assert report['rollback_completed'] is False and report['production_activated'] is False
    assert [step['name'] for step in report['steps']] == STEP_NAMES
    # A fresh run really executes every step; none is a replay.
    assert [(step['status'], step['idempotent']) for step in report['steps']] == [
        ('executed', False)] * len(STEP_NAMES)
    record_raw = (work / 'run-record.json').read_bytes()
    record = json.loads(record_raw)
    assert driver._encoded(record) == record_raw
    assert record['format'] == driver.RUN_FORMAT
    assert report['run_record_digest'] == 'sha256:' + hashlib.sha256(record_raw).hexdigest()
    assert [step['name'] for step in record['steps']] == STEP_NAMES
    terminal = record['terminal']
    assert terminal['manifest_state'] == 'FINALIZED' and terminal['rollback_window_open'] is False
    assert terminal['manifest_digest'] == report['manifest_digest']
    manifest_raw = (work / 'manifest/manifest.json').read_bytes()
    manifest = json.loads(manifest_raw)
    assert manifest['state'] == 'FINALIZED' and manifest['rollback_window_open'] is False
    assert isinstance(manifest['completed_at'], str) and manifest['completed_at']
    assert 'sha256:' + hashlib.sha256(manifest_raw).hexdigest() == terminal['manifest_digest']
    # The Knowledge request counted the full document fixture.
    request = _checkpoint_steps(work)[2]['receipt']
    assert request['counts']['documents'] == 3
    assert request['counts']['originals'] == 1 and request['counts']['attachments'] == 3
    assert request['counts']['bytes'] > 0
    # Terminal writer state on disk: both thawed, pointer published, markers retired.
    assert not (work / 'harness-home/.installation-freeze-v1.json').exists()
    assert not (work / 'knowledge-home/.workspace-freeze-v1.json').exists()
    assert (work / 'harness-home/active-installation.json').exists()
    for side in ('harness-authority', 'knowledge-authority'):
        assert (work / side / 'freeze-marker-rev2.json').exists()
        assert (work / side / 'thaw-receipt-rev2.json').exists()
    # The work root is exactly the documented layout: owned, symlink-free,
    # files private, directories sealed to the owner.
    assert {entry.name for entry in work.iterdir()} == driver._KNOWN_TOP_LEVEL
    _assert_sealed_tree(work)
    # Business data in the source Home stays byte-identical; only the shared
    # admission lock and persistent freeze marker are added.
    assert _tree_digest(home) == before


@installed
def test_sigkill_between_steps_resumes_to_a_byte_identical_record(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, _ = _legacy_home(base, _documents(corpus))
    work = base / 'work'
    child = subprocess.Popen(_driver_command(work, home, corpus), env=_env(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    checkpoint = work / 'driver-checkpoint.json'
    deadline = time.monotonic() + 120
    committed = 0
    while child.poll() is None and time.monotonic() < deadline:
        if checkpoint.exists():
            try:
                committed = len(json.loads(checkpoint.read_bytes())['steps'])
            except (ValueError, OSError):
                committed = 0
        if committed >= 2:
            break
        time.sleep(0.02)
    assert child.poll() is None and committed >= 2
    # The whole process group dies, so no delegated child retains a lock.
    os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL
    killed = _checkpoint_steps(work)
    assert 2 <= len(killed) < len(STEP_NAMES)
    resumed = _report(_run_driver(work, home, corpus))
    assert [step['name'] for step in resumed['steps']] == STEP_NAMES
    assert [step['name'] for step in resumed['steps'] if step['status'] == 'verified'] == [
        step['name'] for step in killed]
    assert [step['name'] for step in resumed['steps'] if step['status'] == 'executed'] == \
        STEP_NAMES[len(killed):]
    record_raw = (work / 'run-record.json').read_bytes()
    again = _report(_run_driver(work, home, corpus))
    assert [(step['status'], step['idempotent']) for step in again['steps']] == [
        ('verified', True)] * len(STEP_NAMES)
    assert (work / 'run-record.json').read_bytes() == record_raw


@installed
def test_duplicate_run_verifies_each_step_and_republishes_identical_record(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, _ = _legacy_home(base, _documents(corpus))
    work = base / 'work'
    first = _report(_run_driver(work, home, corpus))
    record_raw = (work / 'run-record.json').read_bytes()
    checkpoint_raw = (work / 'driver-checkpoint.json').read_bytes()
    second = _report(_run_driver(work, home, corpus))
    assert [(step['name'], step['status'], step['idempotent']) for step in second['steps']] == [
        (name, 'verified', True) for name in STEP_NAMES]
    assert second['run_record_digest'] == first['run_record_digest']
    assert second['manifest_digest'] == first['manifest_digest']
    assert (work / 'run-record.json').read_bytes() == record_raw
    assert (work / 'driver-checkpoint.json').read_bytes() == checkpoint_raw


@installed
def test_tampered_committed_output_refuses_before_any_step(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, _ = _legacy_home(base, _documents(corpus))
    work = base / 'work'
    _report(_run_driver(work, home, corpus))
    record_raw = (work / 'run-record.json').read_bytes()
    checkpoint_raw = (work / 'driver-checkpoint.json').read_bytes()
    target = next(path for path in (work / 'staging/knowledge').rglob('*')
                  if path.is_file() and path.stat().st_size > 0)
    data = target.read_bytes()
    target.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    completed = _run_driver(work, home, corpus)
    assert completed.returncode == 1
    assert len(completed.stdout.strip().splitlines()) == 1
    error = json.loads(completed.stdout)
    assert error['format'] == driver.FORMAT and error['status'] == 'error'
    assert error['error_code'] == 'rehearsal_driver_rejected'
    assert error['step'] is None and error['step_error_code'] is None
    assert error['activation_allowed'] is False
    assert error['installation_cutover_performed'] is False
    assert error['rollback_completed'] is False and error['production_activated'] is False
    assert (work / 'run-record.json').read_bytes() == record_raw
    assert (work / 'driver-checkpoint.json').read_bytes() == checkpoint_raw


@installed
def test_live_catalog_sidecars_fail_at_the_request_step(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    home, connection = _legacy_home(base, _documents(corpus), live=True)
    try:
        assert (home / 'db/catalog.sqlite3-wal').exists()
        work = base / 'work'
        completed = _run_driver(work, home, corpus)
        assert completed.returncode == 1
        error = json.loads(completed.stdout)
        assert error['error_code'] == 'rehearsal_driver_rejected'
        assert error['step'] == 'request'
        assert error['step_error_code'] == 'claw_migration_request_rejected'
        # The snapshot committed before the refusal; no run record survives.
        assert [step['name'] for step in _checkpoint_steps(work)] == ['source-freeze', 'snapshot']
        assert not (work / 'run-record.json').exists()
    finally:
        connection.close()


@installed
def test_live_catalog_with_repair_finalizes_and_preserves_source(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    documents = [
        {'id': 'doc-kept', 'source_path': str(corpus) + '/imported/readme.md',
         'storage_path': str(corpus) + '/imported/readme.md',
         'content_sha256': hashlib.sha256(BODY).hexdigest()},
        {'id': 'doc-dangling', 'source_path': str(corpus) + '/imported/missing.md',
         'storage_path': str(corpus) + '/imported/missing.md',
         'content_sha256': hashlib.sha256(b'missing').hexdigest()},
    ]
    home, connection = _legacy_home(base, documents, live=True)
    try:
        work = base / 'work'
        report = _report(_run_driver(work, home, corpus,
                                     '--repair-document', 'doc-dangling',
                                     '--repair-reason', 'dangling storage_path target is missing'))
        assert report['terminal_state'] == 'FINALIZED'
        steps = _checkpoint_steps(work)
        assert steps[1]['receipt']['repaired'] is True
        counts = steps[2]['receipt']['counts']
        assert counts['documents'] == 1 and counts['originals'] == 0
        assert counts['attachments'] == 0 and counts['bytes'] > 0
        # The source catalog is untouched; only the payload copy was repaired.
        assert connection.execute('SELECT COUNT(*) FROM knowledge_documents').fetchone()[0] == 2
        assert (home / 'db/catalog.sqlite3-wal').exists()
    finally:
        connection.close()


@installed
def test_live_catalog_with_recommit_finalizes_and_preserves_source(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    documents = _documents(corpus)
    # A body edited after import: the recorded digest and size are stale.
    documents[0]['content_sha256'] = '0' * 64
    documents[0]['size_bytes'] = 1
    home, connection = _legacy_home(base, documents, live=True)
    try:
        work = base / 'work'
        report = _report(_run_driver(work, home, corpus,
                                     '--recommit-document', 'doc-1',
                                     '--recommit-reason', 'body edited after import'))
        assert report['terminal_state'] == 'FINALIZED'
        steps = _checkpoint_steps(work)
        assert steps[1]['receipt']['repaired'] is True
        assert steps[2]['receipt']['counts']['documents'] == 3
        # The source catalog is untouched; only the payload copy was recommitted.
        assert connection.execute('SELECT content_sha256 FROM knowledge_documents'
                                  " WHERE id = 'doc-1'").fetchone()[0] == '0' * 64
    finally:
        connection.close()


@installed
def test_virtual_root_run_finalizes_and_requires_the_flag(tmp_path):
    base = tmp_path.resolve()
    corpus = _corpus(base)
    body = b'# Note\n\n![figure](/knowledge/assets/figure.png)\n'
    (corpus / 'imported/note.md').write_bytes(body)
    (corpus / 'assets/figure.png').write_bytes(b'figure')
    documents = _documents(corpus) + [
        {'id': 'doc-4', 'source_path': str(corpus) + '/imported/note.md',
         'storage_path': str(corpus) + '/imported/note.md',
         'content_sha256': hashlib.sha256(body).hexdigest()},
    ]
    home, connection = _legacy_home(base, documents)
    assert connection is None
    # Without a rebinding rule the absolute in-body reference refuses.
    refused = _run_driver(base / 'work-refused', home, corpus)
    assert refused.returncode == 1
    error = json.loads(refused.stdout)
    assert error['step'] == 'request' and error['step_error_code'] == 'claw_migration_request_rejected'
    work = base / 'work'
    report = _report(_run_driver(work, home, corpus, '--virtual-root', '/knowledge=external/knowledge'))
    assert report['terminal_state'] == 'FINALIZED'
    request = _checkpoint_steps(work)[2]['receipt']
    assert request['counts']['documents'] == 4
    # The rebound dependency bytes landed in the migrated home resources tree.
    assert (work / 'knowledge-home/resources/external/knowledge/assets/figure.png').read_bytes() == b'figure'
    again = _report(_run_driver(work, home, corpus, '--virtual-root', '/knowledge=external/knowledge'))
    assert again['run_record_digest'] == report['run_record_digest']


def test_step_table_is_the_ordered_path_a_chain():
    assert STEP_NAMES == ['source-freeze', 'snapshot', 'request', 'orchestrate', 'readiness',
                          'discover', 'prepare', 'enroll', 'suspend', 'cutover', 'finalize']
    assert len(set(STEP_NAMES)) == len(STEP_NAMES)
    for name, roots, body in driver.STEPS:
        assert roots and callable(body)


def test_readiness_step_runs_real_producer_chain_in_order(tmp_path, monkeypatch):
    work = tmp_path.resolve() / 'work'
    work.mkdir(mode=0o700)
    commands = []

    def publish(path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(driver._encoded(value))
        path.chmod(0o600)

    def fake_run(command, _timeout, step, **_kwargs):
        assert step == 'readiness'
        commands.append(command)
        if '-c' in command:
            return {'format': driver._CLAW_IDENTITY_FORMAT, 'package': 'puddingclaw-backend',
                    'version': '0.1.19', 'module': 'cutover_domain_inventory.py',
                    'module_sha256': 'sha256:' + '1' * 64}
        module = command[command.index('-m') + 1]
        if module == 'knowledge_platform.distribution.credential_rebind':
            target = Path(command[command.index('--target-root') + 1])
            target.mkdir(mode=0o700)
            publish(target / 'empty-vault.json', {})
            publish(Path(command[command.index('--receipt') + 1]), {})
            return {'format': 'puddingknowledge-credential-rebind/v1', 'state': 'completed',
                    'credential_continuity_verified': True, 'counts': {'failed': 0}}
        output = Path(command[command.index('--output') + 1])
        if module == 'cutover_domain_inventory':
            domain = command[command.index('--domain') + 1]
            publish(output, {})
            return {'format': 'puddingclaw-cutover-domain-inventory/v1',
                    'domain': domain, 'inventory_sha256': 'sha256:' + '2' * 64}
        if module == 'harness.cutover_domain_inventory':
            publish(output, {})
            return {'format': 'puddingharness-cutover-domain-inventory/v1',
                    'inventory_sha256': 'sha256:' + '3' * 64}
        if module == 'knowledge_platform.distribution.cutover_domain_inventory':
            domain = command[command.index('--domain') + 1]
            publish(output, {})
            return {'format': 'puddingknowledge-cutover-domain-inventory/v1',
                    'domain': domain, 'inventory_sha256': 'sha256:' + '4' * 64}
        if module == 'knowledge_platform.distribution.cutover_domain_coverage':
            publish(output, {})
            return {'format': 'puddingknowledge-cutover-domain-coverage/v1', 'domain_count': 3}
        if module == 'knowledge_platform.distribution.cutover_index_readiness':
            publish(output, {})
            return {'format': 'puddingknowledge-cutover-index-readiness/v1',
                    'state': 'ready', 'ready_count': 1}
        assert module == 'knowledge_platform.distribution.cutover_readiness'
        complete = {'format': 'puddingknowledge-cutover-readiness/v1',
                    'state': 'verified_inactive_complete',
                    'covered_domains': ['session_harness', 'knowledge_catalog', 'connector_jobs'],
                    'pending_domains': [], 'cutover_readiness_verified': True,
                    'complete_migration_evidence': True, 'writer_fence_verified': False,
                    'activation_allowed': False}
        publish(output, complete)
        return complete

    monkeypatch.setattr(driver, '_run_cli', fake_run)
    ctx = SimpleNamespace(
        work=work, claw_python='/claw/python', knowledge_python='/knowledge/python',
        timeout=30, source_home='/legacy/home', credential_owner=OWNER,
        path=lambda name: work / name,
    )
    receipt, idempotent = driver._step_readiness(ctx)
    assert idempotent is False and receipt['credential_owner'] == OWNER
    modules = [command[command.index('-m') + 1] for command in commands if '-m' in command]
    assert modules == [
        'cutover_domain_inventory', 'cutover_domain_inventory', 'cutover_domain_inventory',
        'harness.cutover_domain_inventory',
        'knowledge_platform.distribution.cutover_domain_inventory',
        'knowledge_platform.distribution.cutover_domain_inventory',
        'knowledge_platform.distribution.cutover_domain_coverage',
        'knowledge_platform.distribution.cutover_index_readiness',
        'knowledge_platform.distribution.credential_rebind',
        'knowledge_platform.distribution.cutover_readiness',
    ]
    assert driver._root_digest(work / 'readiness').startswith('sha256:')


def test_prepare_consumes_complete_readiness_and_source_freeze(tmp_path, monkeypatch):
    work = tmp_path.resolve() / 'work'
    work.mkdir(mode=0o700)
    captured = []

    def fake_run(command, _timeout, _step):
        captured.extend(command)
        return {'format': driver.manifests.FORMAT, 'state': 'PREPARED',
                'idempotent': False, 'manifest_digest': 'sha256:' + 'a' * 64}

    monkeypatch.setattr(driver, '_run_cli', fake_run)
    ctx = SimpleNamespace(work=work, timeout=30, path=lambda name: work / name)
    driver._step_prepare(ctx)
    assert captured[captured.index('--knowledge-readiness') + 1] == str(
        work / 'readiness/cutover-readiness.json')
    assert captured[captured.index('--source-freeze-receipt') + 1] == str(
        work / 'source-freeze-receipt.json')


def test_source_freeze_file_is_the_canonical_manifest_consumer_contract(tmp_path):
    base = tmp_path.resolve()
    home = _minimal_home(base)
    work = base / 'work'
    work.mkdir(mode=0o700)
    ctx = SimpleNamespace(
        work=work, source_home=str(home), operation='rehearsal-1',
        path=lambda name: work / name,
    )
    receipt, idempotent = driver._step_source_freeze(ctx)
    assert idempotent is False and len(receipt['credential_baseline_sha256']) == 64
    persisted = json.loads((work / 'source-freeze-receipt.json').read_bytes())
    assert set(persisted) == {
        'format', 'operation_id', 'source_home_identity', 'source_freeze_receipt_sha256',
        'legacy_writer_fenced', 'admission_capability_sha256',
    }
    assert driver.manifests.encoded(persisted) == (work / 'source-freeze-receipt.json').read_bytes()


def test_root_digest_commits_bytes_and_private_modes(tmp_path):
    root = tmp_path.resolve() / 'tree'
    (root / 'sub').mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    payload = root / 'sub/file.json'
    payload.write_bytes(b'{}')
    os.chmod(payload, 0o600)
    first = driver._root_digest(root)
    assert driver._root_digest(root) == first
    payload.write_bytes(b'{"x": 1}')
    assert driver._root_digest(root) != first
    os.chmod(payload, 0o644)
    with pytest.raises(ValueError):
        driver._root_digest(root)
    os.chmod(payload, 0o600)
    link = root / 'sub/link'
    link.symlink_to(payload)
    with pytest.raises((ValueError, OSError)):
        driver._root_digest(root)


def test_load_checkpoint_roundtrip_and_fail_closed_validation(tmp_path):
    work = tmp_path.resolve() / 'work'
    work.mkdir(mode=0o700)
    assert driver._load_checkpoint(work) is None
    path = work / driver.CHECKPOINT_NAME
    base = {'format': driver.CHECKPOINT_FORMAT, 'work_root': str(work),
            'operation': 'rehearsal-1', 'installation_id': 'install-rehearsal',
            'parameters_digest': 'sha256:' + 'a' * 64, 'steps': []}
    driver._replace_private(path, driver._encoded(base))
    assert driver._load_checkpoint(work) == base
    path.write_bytes(b'{')
    with pytest.raises(ValueError, match='torn'):
        driver._load_checkpoint(work)
    driver._replace_private(path, json.dumps(base, indent=2).encode())
    with pytest.raises(ValueError, match='canonical'):
        driver._load_checkpoint(work)
    wrong_order = dict(base, steps=[{'name': 'request', 'idempotent': False, 'receipt': {},
                                     'outputs': {'request': 'sha256:' + 'b' * 64}}])
    driver._replace_private(path, driver._encoded(wrong_order))
    with pytest.raises(ValueError, match='step'):
        driver._load_checkpoint(work)
    driver._replace_private(path, driver._encoded(dict(base, operation='bad operation')))
    with pytest.raises(ValueError):
        driver._load_checkpoint(work)
    driver._replace_private(path, driver._encoded(dict(base, work_root=str(work) + 'x')))
    with pytest.raises(ValueError, match='invalid'):
        driver._load_checkpoint(work)
    driver._replace_private(path, driver._encoded(base))
    os.chmod(path, 0o644)
    with pytest.raises((ValueError, OSError)):
        driver._load_checkpoint(work)


def test_verify_committed_roots_folds_latest_record_and_detects_divergence(tmp_path):
    work = tmp_path.resolve() / 'work'
    root = work / 'root'
    (root / 'sub').mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    payload = root / 'sub/f.json'
    payload.write_bytes(b'{}')
    os.chmod(payload, 0o600)
    stale = {'name': 'a', 'idempotent': False, 'receipt': {},
             'outputs': {'root': 'sha256:' + '0' * 64}}
    current = {'name': 'b', 'idempotent': False, 'receipt': {},
               'outputs': {'root': driver._root_digest(root)}}
    # A later step legitimately rewrote the root: the newest record wins.
    driver._verify_committed_roots(work, [stale, current])
    with pytest.raises(ValueError, match='diverged'):
        driver._verify_committed_roots(work, [current, stale])
    payload.write_bytes(b'{"x": 1}')
    with pytest.raises(ValueError, match='diverged'):
        driver._verify_committed_roots(work, [stale, current])


def _main_argv(work, home, *extra):
    return ['--work-root', str(work), '--source-home', str(home),
            '--knowledge-python', sys.executable,
            '--claw-python', sys.executable, '--credential-owner', OWNER,
            '--installation-id', 'install-rehearsal',
            '--source-revision', 'legacy-1', '--source-schema-revision', 'claw-schema-v1',
            '--operation', 'rehearsal-1', '--map', '/nonexistent=payload', *extra]


def _error_line(capsys):
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    return json.loads(out)


def test_work_root_hygiene_refuses_unknown_entries_and_cleans_transients(tmp_path, capsys):
    base = tmp_path.resolve()
    home = base / 'home'
    home.mkdir()
    os.chmod(home, 0o700)
    _source_capability(home)
    work = base / 'work'
    work.mkdir(mode=0o700)
    (work / 'stray.txt').write_bytes(b'x')
    assert driver.main(_main_argv(work, home)) == 1
    assert _error_line(capsys)['step'] is None
    (work / 'stray.txt').unlink()
    (work / 'link').symlink_to(home)
    assert driver.main(_main_argv(work, home)) == 1
    assert _error_line(capsys)['step'] is None
    (work / 'link').unlink()
    for name in ('.driver-checkpoint.json.tmp-0123456789abcdef',
                 '.run-record.json.tmp-0123456789abcdef',
                 '.knowledge-receipt.json.tmp-0123456789abcdef',
                 '.snapshot-receipt.json.rehearsal-part'):
        transient = work / name
        transient.write_bytes(b'{}')
        os.chmod(transient, 0o600)
    # Transients are validated and removed; the run then proceeds into the
    # chain and fails closed at the request step because sys.executable
    # carries no Knowledge installation.
    assert driver.main(_main_argv(work, home)) == 1
    error = _error_line(capsys)
    assert error['step'] == 'request' and error['step_error_code'] == 'step_failed'
    assert not any(entry.name.startswith('.') for entry in work.iterdir())


def _minimal_home(base):
    home = base / 'home'
    (home / 'db').mkdir(parents=True)
    os.chmod(home, 0o700)
    _source_capability(home)
    (home / 'sessions').mkdir()
    (home / 'llm-wiki').mkdir()
    (home / 'config.json').write_bytes(b'{"cache": {"enabled": false}}')
    (home / 'sessions/s1.json').write_bytes(b'{}')
    connection = sqlite3.connect(home / 'db/catalog.sqlite3')
    connection.execute('CREATE TABLE knowledge_documents (id TEXT PRIMARY KEY)')
    connection.commit()
    connection.close()
    return home


def test_parameter_change_refuses_with_committed_steps(tmp_path, capsys):
    base = tmp_path.resolve()
    home = _minimal_home(base)
    work = base / 'work'
    receipt = work / 'source-freeze-receipt.json'
    baseline = work / 'credential-baseline.json'
    work.mkdir(mode=0o700)
    receipt.write_bytes(b'{}\n')
    os.chmod(receipt, 0o600)
    baseline.write_bytes(b'{}\n')
    os.chmod(baseline, 0o600)
    record = {'name': 'source-freeze', 'idempotent': False, 'receipt': {},
              'outputs': {'credential-baseline.json': driver._root_digest(baseline),
                          'source-freeze-receipt.json': driver._root_digest(receipt)}}
    checkpoint = {'format': driver.CHECKPOINT_FORMAT, 'work_root': str(work),
                  'operation': 'rehearsal-1', 'installation_id': 'install-rehearsal',
                  'parameters_digest': 'sha256:' + 'a' * 64, 'steps': [record]}
    driver._replace_private(work / driver.CHECKPOINT_NAME, driver._encoded(checkpoint))
    assert driver.main(_main_argv(work, home)) == 1
    error = _error_line(capsys)
    assert error['step'] is None and error['error_code'] == 'rehearsal_driver_rejected'
    # The committed step verified cleanly, the change refused, nothing moved.
    assert driver._load_checkpoint(work)['steps'] == [record]


def test_parameter_change_reinitializes_an_uncommitted_checkpoint(tmp_path, capsys, monkeypatch):
    base = tmp_path.resolve()
    home = _minimal_home(base)
    work = base / 'work'
    work.mkdir(mode=0o700)
    checkpoint = {'format': driver.CHECKPOINT_FORMAT, 'work_root': str(work),
                  'operation': 'old-operation', 'installation_id': 'install-rehearsal',
                  'parameters_digest': 'sha256:' + 'a' * 64, 'steps': []}
    driver._replace_private(work / driver.CHECKPOINT_NAME, driver._encoded(checkpoint))
    if os.environ.get('HARNESS_TEST_INSTALLED') == '1':
        monkeypatch.delenv('PYTHONPATH', raising=False)
    else:
        monkeypatch.setenv('PYTHONPATH', str(BACKEND))
    # Zero committed steps: the checkpoint rebinds to the new parameters and
    # the chain restarts; the request step then fails closed because
    # sys.executable carries no Knowledge installation.
    assert driver.main(_main_argv(work, home)) == 1
    error = _error_line(capsys)
    assert error['step'] == 'request' and error['step_error_code'] == 'step_failed'
    committed = driver._load_checkpoint(work)
    assert committed['operation'] == 'rehearsal-1'
    assert committed['parameters_digest'] != 'sha256:' + 'a' * 64
    assert [step['name'] for step in committed['steps']] == ['source-freeze', 'snapshot']


def test_interrupted_next_step_may_resume_its_owned_mutated_root(tmp_path):
    work = tmp_path.resolve() / 'work'
    work.mkdir(mode=0o700)
    stable = work / 'stable'
    mutable = work / 'mutable'
    stable.write_bytes(b'stable')
    mutable.write_bytes(b'before')
    for path in (stable, mutable):
        path.chmod(0o600)
    steps = [{'outputs': {'stable': driver._root_digest(stable),
                          'mutable': driver._root_digest(mutable)}}]
    mutable.write_bytes(b'interrupted next-step state')
    driver._verify_committed_roots(work, steps, ('mutable',))
    with pytest.raises(ValueError, match='mutable'):
        driver._verify_committed_roots(work, steps)


def test_run_record_surviving_an_incomplete_checkpoint_refuses(tmp_path, capsys):
    base = tmp_path.resolve()
    home = _minimal_home(base)
    work = base / 'work'
    work.mkdir(mode=0o700)
    record = {'format': driver.RUN_FORMAT, 'operation': 'rehearsal-1',
              'installation_id': 'install-rehearsal',
              'parameters_digest': 'sha256:' + 'a' * 64, 'steps': [], 'terminal': {}}
    driver._replace_private(work / driver.RUN_RECORD_NAME, driver._encoded(record))
    assert driver.main(_main_argv(work, home)) == 1
    assert _error_line(capsys)['step'] is None
    assert (work / driver.RUN_RECORD_NAME).read_bytes() == driver._encoded(record)
