import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat

import pytest

from harness.source_snapshot import FORMAT, LOCK_NAME, VerifiedSourceSnapshot, _encoded, _inventory
from harness.rehearsal_snapshot import produce_rehearsal_snapshot


def sha(data):
    return hashlib.sha256(data).hexdigest()


def legacy_home(root):
    """Synthetic legacy Home mirroring the measured real shape: 0755 dirs,
    0644 files, .DS_Store litter, a WAL catalog with live sidecars and an
    excluded symlink under sessions/traces."""
    home = root / 'home'
    for rel in ('db/backups', 'sessions/traces', 'cache', 'logs', 'state', 'tmp',
                'data/attachments', '.vault-keys'):
        (home / rel).mkdir(parents=True, mode=0o755)
    files = {
        'config.json': b'{"theme": "dark"}',
        'config.pre-migration-backup.json': b'{}',
        '.DS_Store': b'\x00\x00',
        'sessions/.DS_Store': b'\x00',
        'sessions/s1.json': b'{"messages": []}',
        'sessions/s2.json': b'{"messages": [1]}',
        'sessions/traces/t1.jsonl': b'x' * 1024,
        'sessions/traces/big.bin': b'y' * 2048,
        'cache/cached.bin': b'c' * 64,
        'logs/app.log': b'log\n',
        'state/runtime.json': b'{}',
        'tmp/scratch.bin': b't',
        'db/backups/old.sqlite3': b'old',
        'data/attachments/kept.md': b'# kept\n',
        '.vault-keys/local.key': b'K' * 32,
    }
    for rel, data in files.items():
        path = home / rel
        path.write_bytes(data)
        path.chmod(0o644)
    # A symlink under an excluded prefix is never even inspected.
    (home / 'sessions/traces/evil-link').symlink_to('/etc/passwd')
    connection = sqlite3.connect(home / 'db/catalog.sqlite3')
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('CREATE TABLE knowledge_documents (id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL,'
                       ' title TEXT NOT NULL, storage_path TEXT NOT NULL)')
    connection.execute("INSERT INTO knowledge_documents VALUES ('doc_kept', 'kb1', 'Kept document', 'data/attachments/kept.md')")
    connection.execute("INSERT INTO knowledge_documents VALUES ('doc_dangling', 'kb1', 'Dangling document', 'data/attachments/missing.md')")
    connection.commit()
    # The open connection keeps the -wal/-shm sidecars live, like a real Home.
    for name in ('catalog.sqlite3', 'catalog.sqlite3-wal', 'catalog.sqlite3-shm'):
        (home / 'db' / name).chmod(0o644)
    return home, connection


def graft_tree(root):
    graft = root / 'knowledge'
    (graft / 'sub').mkdir(parents=True, mode=0o755)
    (graft / 'cache').mkdir(mode=0o755)
    for rel, data in {'a.md': b'# A\n', 'sub/b.md': b'# B\n', '.DS_Store': b'\x00', 'cache/c.bin': b'c'}.items():
        path = graft / rel
        path.write_bytes(data)
        path.chmod(0o644)
    return graft


@pytest.fixture
def legacy(tmp_path):
    home, connection = legacy_home(tmp_path.resolve())
    try:
        yield home, connection
    finally:
        connection.close()


def test_snapshot_passes_real_admission_and_receipt_matches(legacy, tmp_path):
    home, _ = legacy
    base = tmp_path.resolve()
    root, receipt = base / 'snapshot', base / 'receipt.json'
    graft = graft_tree(base)
    result = produce_rehearsal_snapshot(home, root, grafts=[(graft, 'external/knowledge')], receipt=receipt)
    assert result['status'] == 'verified' and not result['idempotent'] and not result['activation_allowed']
    with VerifiedSourceSnapshot(root) as snapshot:
        assert snapshot.commitment == result['commitment']
    assert snapshot.commitment['format'] == FORMAT
    raw = receipt.read_bytes()
    assert _encoded(json.loads(raw)) == raw
    document = json.loads(raw)
    assert document['commitment'] == result['commitment']
    assert document['repair'] is None
    assert document['grafts'] == [{'source': str(graft), 'destination': 'external/knowledge',
                                   'files': 2, 'total_bytes': 8}]
    inventory = _inventory(root / 'payload')
    assert document['files'] == len(inventory['files'])
    assert document['directories'] == len(inventory['directories'])
    assert document['total_bytes'] == inventory['total_bytes']
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert {p.name for p in root.iterdir()} == {LOCK_NAME, 'plan.json', 'manifest.json', 'payload'}


def test_plan_and_manifest_bytes_are_canonical_and_exact(legacy, tmp_path):
    home, _ = legacy
    root = tmp_path.resolve() / 'snapshot'
    produce_rehearsal_snapshot(home, root)
    info = home.stat()
    inventory = _inventory(root / 'payload')
    plan = {'format': FORMAT, 'source_identity': sha(str(home).encode('utf-8')),
            'source_directory_identity': {'device': info.st_dev, 'inode': info.st_ino},
            'output_identity': sha(str(root).encode('utf-8')), 'inventory': inventory}
    assert (root / 'plan.json').read_bytes() == _encoded(plan)
    manifest = {'format': FORMAT, 'plan_digest': sha(_encoded(plan)), 'state': 'verified_raw_home_snapshot',
                'inventory': inventory, 'cooperative_admission_held': True, 'writer_fence_verified': False,
                'catalog_normalization_required': True, 'activation_allowed': False,
                'installation_prepared': False, 'credential_rebind_required': True,
                'requires_domain_migration': True, 'source_home_kind': 'legacy_puddingclaw_home',
                'catalog': {'relative_path': 'db/catalog.sqlite3', 'role': 'legacy_claw_core_catalog',
                            'direct_import_allowed': False, 'wal_policy': 'captured_as_sibling_files'}}
    assert (root / 'manifest.json').read_bytes() == _encoded(manifest)
    # The catalog and its live WAL siblings are captured as plain bytes.
    for name in ('catalog.sqlite3', 'catalog.sqlite3-wal', 'catalog.sqlite3-shm'):
        relative = 'db/' + name
        assert inventory['files'][relative] == {'sha256': sha((home / 'db' / name).read_bytes()),
                                                'size': (home / 'db' / name).stat().st_size}


def test_default_and_extra_exclusions_prune_transient_domains(legacy, tmp_path):
    home, _ = legacy
    base = tmp_path.resolve()
    graft = graft_tree(base)
    produce_rehearsal_snapshot(home, base / 'snapshot', grafts=[(graft, 'external/knowledge')])
    inventory = _inventory(base / 'snapshot/payload')
    paths = set(inventory['files']) | set(inventory['directories'])
    assert 'sessions' in inventory['directories']
    assert 'sessions/s1.json' in inventory['files']
    assert not any(path == 'sessions/traces' or path.startswith('sessions/traces/') for path in paths)
    assert not any(PurePosixPath(path).name == '.DS_Store' for path in paths)
    for prefix in ('cache', 'logs', 'state', 'tmp', 'db/backups'):
        assert not any(path == prefix or path.startswith(prefix + '/') for path in paths)
    assert 'config.json' in inventory['files']
    assert 'config.pre-migration-backup.json' not in paths
    assert 'external/knowledge/sub/b.md' in inventory['files']
    assert not any(path.startswith('external/knowledge/cache') for path in paths)
    result = produce_rehearsal_snapshot(home, base / 'snapshot-extra',
                                        exclusions=['data/attachments', 'sessions'])
    inventory = _inventory(base / 'snapshot-extra/payload')
    paths = set(inventory['files']) | set(inventory['directories'])
    assert 'sessions' not in paths and 'data/attachments' not in paths
    assert 'data' in inventory['directories']
    assert result['exclusions']['extra'] == ['data/attachments', 'sessions']


def test_permissions_are_normalized_private(legacy, tmp_path):
    home, _ = legacy
    root = tmp_path.resolve() / 'snapshot'
    produce_rehearsal_snapshot(home, root)
    info = root.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_mode & 0o777 == 0o700
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in dirs:
            info = (Path(current) / name).lstat()
            assert stat.S_ISDIR(info.st_mode) and info.st_mode & 0o777 == 0o700
            assert info.st_uid == os.getuid()
        for name in names:
            path = Path(current) / name
            info = path.lstat()
            assert not path.is_symlink()
            assert stat.S_ISREG(info.st_mode) and info.st_mode & 0o777 == 0o600
            assert info.st_nlink == 1 and info.st_uid == os.getuid()


def test_symlinks_refuse_by_default_and_copy_with_follow(legacy, tmp_path):
    home, _ = legacy
    base = tmp_path.resolve()
    (home / 'session-link').symlink_to('sessions/s1.json')
    with pytest.raises((ValueError, OSError)):
        produce_rehearsal_snapshot(home, base / 'refused')
    assert not (base / 'refused').exists()
    produce_rehearsal_snapshot(home, base / 'followed', follow_symlinks=True)
    copied = base / 'followed/payload/session-link'
    assert not copied.is_symlink() and stat.S_ISREG(copied.lstat().st_mode)
    assert copied.read_bytes() == (home / 'sessions/s1.json').read_bytes()
    (home / 'dir-link').symlink_to('sessions')
    with pytest.raises((ValueError, OSError)):
        produce_rehearsal_snapshot(home, base / 'refused-dir', follow_symlinks=True)
    assert not (base / 'refused-dir').exists()


def test_catalog_quiescence_refusal_when_bytes_change(legacy, tmp_path):
    home, connection = legacy

    def tamper(db_dir):
        connection.execute("INSERT INTO knowledge_documents VALUES ('doc_late', 'kb1', 'Late', 'x')")
        connection.commit()

    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, tmp_path.resolve() / 'snapshot', _quiescence_hook=tamper)
    assert not (tmp_path.resolve() / 'snapshot').exists()


def test_repair_removes_dangling_row_and_receipts_digests(legacy, tmp_path):
    home, connection = legacy
    base = tmp_path.resolve()
    sidecars = ('catalog.sqlite3', 'catalog.sqlite3-wal', 'catalog.sqlite3-shm')
    before = {name: sha((home / 'db' / name).read_bytes()) for name in sidecars}
    root, receipt = base / 'snapshot', base / 'receipt.json'
    result = produce_rehearsal_snapshot(home, root, repair_document='doc_dangling',
                                        repair_reason='dangling storage_path target is missing',
                                        receipt=receipt)
    catalog = root / 'payload/db/catalog.sqlite3'
    assert catalog.exists()
    assert {path.name for path in (root / 'payload/db').iterdir()} == {'catalog.sqlite3'}
    with VerifiedSourceSnapshot(root) as snapshot:
        assert snapshot.commitment == result['commitment']
    repair = json.loads(receipt.read_bytes())['repair']
    assert repair['document'] == {'id': 'doc_dangling', 'title': 'Dangling document',
                                  'storage_path': 'data/attachments/missing.md'}
    assert repair['before_digests'] == before
    assert repair['after_digest'] == sha(catalog.read_bytes())
    assert repair['foreign_key_check'] == 'clean' and repair['integrity_check'] == 'ok'
    assert repair['reason'] == 'dangling storage_path target is missing'
    # The source catalog is untouched; only the payload copy was repaired.
    assert {name: sha((home / 'db' / name).read_bytes()) for name in sidecars} == before
    # The payload copy is a clean single-file catalog without the dangling row.
    payload_digest = sha(catalog.read_bytes())
    check = sqlite3.connect(catalog)
    try:
        assert check.execute('SELECT id FROM knowledge_documents ORDER BY id').fetchall() == [('doc_kept',)]
        assert check.execute('PRAGMA foreign_key_check').fetchall() == []
        assert check.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    finally:
        check.close()
    assert sha(catalog.read_bytes()) == payload_digest
    assert {path.name for path in (root / 'payload/db').iterdir()} == {'catalog.sqlite3'}
    # A re-run with identical inputs is byte-identical and idempotent.
    plan_raw = (root / 'plan.json').read_bytes()
    again = produce_rehearsal_snapshot(home, root, repair_document='doc_dangling',
                                       repair_reason='dangling storage_path target is missing',
                                       receipt=receipt)
    assert again['idempotent'] and again['commitment'] == result['commitment']
    assert (root / 'plan.json').read_bytes() == plan_raw


def test_repair_requires_an_existing_document(legacy, tmp_path):
    home, _ = legacy
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, tmp_path.resolve() / 'snapshot',
                                   repair_document='doc_missing', repair_reason='no such row')
    assert not (tmp_path.resolve() / 'snapshot').exists()


def add_source_items(connection):
    connection.execute('CREATE TABLE knowledge_source_items ('
                       'id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL,'
                       ' source_connection_id TEXT NOT NULL, external_id TEXT NOT NULL,'
                       ' document_id TEXT REFERENCES knowledge_documents(id))')


def test_repair_deletes_dependent_source_items_and_receipts_them(legacy, tmp_path):
    home, connection = legacy
    base = tmp_path.resolve()
    sidecars = ('catalog.sqlite3', 'catalog.sqlite3-wal', 'catalog.sqlite3-shm')
    add_source_items(connection)
    # Mirrors the real rehearsal Home: the source item is the import record of
    # the same dangling content; the kept item tracks the surviving document.
    connection.execute("INSERT INTO knowledge_source_items VALUES"
                       " ('sitem_dangling', 'kb1', 'conn1', 'document:doc_dangling', 'doc_dangling')")
    connection.execute("INSERT INTO knowledge_source_items VALUES"
                       " ('sitem_kept', 'kb1', 'conn1', 'document:doc_kept', 'doc_kept')")
    connection.commit()
    before = {name: sha((home / 'db' / name).read_bytes()) for name in sidecars}
    root, receipt = base / 'snapshot', base / 'receipt.json'
    result = produce_rehearsal_snapshot(home, root, repair_document='doc_dangling',
                                        repair_reason='dangling storage_path target is missing',
                                        receipt=receipt)
    with VerifiedSourceSnapshot(root) as snapshot:
        assert snapshot.commitment == result['commitment']
    repair = json.loads(receipt.read_bytes())['repair']
    assert repair['document']['id'] == 'doc_dangling'
    assert repair['dependent_deletions'] == [{'table': 'knowledge_source_items', 'id': 'sitem_dangling'}]
    assert repair['before_digests'] == before
    assert repair['foreign_key_check'] == 'clean' and repair['integrity_check'] == 'ok'
    catalog = root / 'payload/db/catalog.sqlite3'
    payload_digest = sha(catalog.read_bytes())
    assert repair['after_digest'] == payload_digest
    check = sqlite3.connect(catalog)
    try:
        assert check.execute('SELECT id FROM knowledge_documents ORDER BY id').fetchall() == [('doc_kept',)]
        assert check.execute('SELECT id FROM knowledge_source_items ORDER BY id').fetchall() == [('sitem_kept',)]
        assert check.execute('PRAGMA foreign_key_check').fetchall() == []
        assert check.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
    finally:
        check.close()
    assert sha(catalog.read_bytes()) == payload_digest
    # The source catalog is untouched: both rows are still present there.
    assert {name: sha((home / 'db' / name).read_bytes()) for name in sidecars} == before
    assert connection.execute('SELECT COUNT(*) FROM knowledge_documents').fetchall() == [(2,)]
    assert connection.execute('SELECT COUNT(*) FROM knowledge_source_items').fetchall() == [(2,)]


def test_repair_refuses_non_allowlisted_dependent_violations(legacy, tmp_path):
    home, connection = legacy
    connection.execute('CREATE TABLE knowledge_attachments (id TEXT PRIMARY KEY,'
                       ' document_id TEXT REFERENCES knowledge_documents(id))')
    connection.execute("INSERT INTO knowledge_attachments VALUES ('att_1', 'doc_dangling')")
    connection.commit()
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, tmp_path.resolve() / 'snapshot',
                                   repair_document='doc_dangling',
                                   repair_reason='dangling storage_path target is missing')
    assert not (tmp_path.resolve() / 'snapshot').exists()


def test_repair_refuses_beyond_the_dependent_deletion_limit(legacy, tmp_path):
    home, connection = legacy
    add_source_items(connection)
    for index in range(17):
        connection.execute("INSERT INTO knowledge_source_items VALUES (?, 'kb1', 'conn1', ?, 'doc_dangling')",
                           (f'sitem_{index:02d}', f'document:doc_dangling:{index}'))
    connection.commit()
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, tmp_path.resolve() / 'snapshot',
                                   repair_document='doc_dangling',
                                   repair_reason='dangling storage_path target is missing')
    assert not (tmp_path.resolve() / 'snapshot').exists()


def test_repair_refuses_unrelated_violations_that_remain(legacy, tmp_path):
    home, connection = legacy
    add_source_items(connection)
    # An allowlisted row dangling on a DIFFERENT document is a pre-existing
    # violation the repair must not clear or mask: it remains and refuses.
    connection.execute("INSERT INTO knowledge_source_items VALUES"
                       " ('sitem_other', 'kb1', 'conn1', 'document:doc_other', 'doc_other')")
    connection.commit()
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, tmp_path.resolve() / 'snapshot',
                                   repair_document='doc_dangling',
                                   repair_reason='dangling storage_path target is missing')
    assert not (tmp_path.resolve() / 'snapshot').exists()


def test_rerun_is_byte_identical_and_idempotent(legacy, tmp_path):
    home, _ = legacy
    base = tmp_path.resolve()
    graft = graft_tree(base)
    root, receipt = base / 'snapshot', base / 'receipt.json'
    kwargs = {'grafts': [(graft, 'external/knowledge')], 'exclusions': ['logs'], 'receipt': receipt}
    first = produce_rehearsal_snapshot(home, root, **kwargs)
    plan_raw = (root / 'plan.json').read_bytes()
    manifest_raw = (root / 'manifest.json').read_bytes()
    receipt_raw = receipt.read_bytes()
    second = produce_rehearsal_snapshot(home, root, **kwargs)
    assert not first['idempotent'] and second['idempotent']
    assert second['commitment'] == first['commitment']
    assert (root / 'plan.json').read_bytes() == plan_raw
    assert (root / 'manifest.json').read_bytes() == manifest_raw
    assert receipt.read_bytes() == receipt_raw


def test_conflicting_or_incomplete_rerun_refuses(legacy, tmp_path):
    home, _ = legacy
    base = tmp_path.resolve()
    root = base / 'snapshot'
    produce_rehearsal_snapshot(home, root)
    plan_raw = (root / 'plan.json').read_bytes()
    (home / 'sessions/s3.json').write_bytes(b'{"messages": [2]}')
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, root)
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, root, exclusions=['logs'])
    assert (root / 'plan.json').read_bytes() == plan_raw
    partial = base / 'partial'
    (partial / 'payload').mkdir(parents=True, mode=0o700)
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, partial)


def test_graft_destination_collisions_refuse(legacy, tmp_path):
    home, _ = legacy
    base = tmp_path.resolve()
    graft = graft_tree(base)
    other = base / 'other'
    other.mkdir()
    (other / 'x.md').write_bytes(b'x')
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, base / 'a',
                                   grafts=[(graft, 'external'), (other, 'external/knowledge')])
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, base / 'b', grafts=[(graft, 'sessions/extra')])
    with pytest.raises(ValueError):
        produce_rehearsal_snapshot(home, base / 'c', grafts=[(graft, 'sessions')])
    assert not (base / 'a').exists() and not (base / 'b').exists() and not (base / 'c').exists()
