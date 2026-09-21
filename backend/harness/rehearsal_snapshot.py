"""Produce a verified raw legacy PuddingClaw Home snapshot for the real-data rehearsal.

This is the producer side of the offline migration admission contract. It copies
a live legacy PuddingClaw Home (strictly read-only on the source) plus optional
grafted external trees into a private snapshot envelope, then proves the product
with the real harness.source_snapshot.VerifiedSourceSnapshot admission check.
It exists to drive the specification section 11.20 item 10 real-data rehearsal
against a copy of a real legacy Home.

Envelope contract (puddingclaw-source-home-snapshot/v1, consumed by
harness.source_snapshot):

- The snapshot root contains EXACTLY four entries: `.installation-gate-v1.lock`
  (content unconstrained, may be empty), `plan.json`, `manifest.json` and the
  `payload/` directory.
- Every file is regular, mode 0600 (no group/other bits), hardlink count 1 and
  owned by the current uid; every directory is mode 0700 and owned. No symlinks
  anywhere from the root to a leaf.
- plan.json is canonical JSON (json.dumps(value, sort_keys=True,
  separators=(',', ':')) + '\\n') with keys exactly {format, source_identity,
  source_directory_identity, output_identity, inventory}:
    format = 'puddingclaw-source-home-snapshot/v1'
    source_identity = sha256 hexdigest of the absolute source home path (utf-8)
    source_directory_identity = {device, inode} ints of the SOURCE home dir
    output_identity = sha256 hexdigest of str(snapshot_root)
    inventory = recursive inventory of payload/ as
      {files: {relative: {sha256, size}}, directories: [...], total_bytes}
      satisfying source_snapshot._validate_inventory (every parent directory
      listed, at most 50000 entries, per-file at most 2 GiB, total at most
      16 GiB).
- manifest.json is the fully determined dict from source_snapshot.py,
  canonically encoded:
    {'format': FORMAT, 'plan_digest': sha256(plan.json bytes),
     'state': 'verified_raw_home_snapshot', 'inventory': <same inventory>,
     'cooperative_admission_held': True, 'writer_fence_verified': False,
     'catalog_normalization_required': True, 'activation_allowed': False,
     'installation_prepared': False, 'credential_rebind_required': True,
     'requires_domain_migration': True,
     'source_home_kind': 'legacy_puddingclaw_home',
     'catalog': {'relative_path': 'db/catalog.sqlite3',
                 'role': 'legacy_claw_core_catalog',
                 'direct_import_allowed': False,
                 'wal_policy': 'captured_as_sibling_files'}}
- After emission the producer opens its own output with VerifiedSourceSnapshot
  and reports the commitment; any admission failure is a producer bug.

The source home is never modified: files are byte-copied (never moved or
hardlinked), directories are re-created 0700, files 0600 with a single link and
no symlinks. db/catalog.sqlite3 and any -wal/-shm/-journal sidecars are copied
as sibling bytes (never opened with sqlite); a two-pass digest proof around an
fsync barrier refuses a non-quiesced catalog. The optional repair flow operates
on a private working copy only and supports two kinds: --repair-document
deletes one dangling knowledge_documents row plus allowlisted dependents, and
--recommit-document rebinds one document's recorded body digest and size to
the actual source bytes when the body drifted after import (for PDF-converted
rows the body binding is doc_metadata.markdown_sha256 and only it is
recommitted; the original-PDF binding is left untouched). A repair that would
change nothing is refused. Receipts and stdout carry paths, digests and
counts, never secret bytes.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from harness.home_freeze import _sync_directory
from harness.source_snapshot import (
    FORMAT, LOCK_NAME, MAX_ENTRIES, MAX_FILE, MAX_TOTAL, VerifiedSourceSnapshot,
    _encoded, _inventory, _path, _private, _relative, _sha, _validate_inventory,
)

RECEIPT_FORMAT = 'puddingclaw-rehearsal-snapshot/v1'
REPAIR_FORMAT = 'puddingclaw-rehearsal-catalog-repair/v1'
DEFAULT_EXCLUDED_PREFIXES = ('sessions/traces', 'cache', 'logs', 'state', 'db/backups', 'tmp')
EXCLUDED_NAMES = ('.DS_Store',)
EXCLUDED_PATTERNS = ('*.pre-migration-*',)
CATALOG = 'db/catalog.sqlite3'
CATALOG_NAMES = ('catalog.sqlite3', 'catalog.sqlite3-wal', 'catalog.sqlite3-shm', 'catalog.sqlite3-journal')
CATALOG_RELS = frozenset('db/' + name for name in CATALOG_NAMES)
PART_SUFFIX = '.rehearsal-part'
DEPENDENT_DELETE_ALLOWLIST = frozenset({'knowledge_source_items'})
MAX_DEPENDENT_DELETIONS = 16


def _owned_directory(value, label):
    path = _path(value)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise ValueError(f'{label} does not exist: {path}') from None
    # Source trees keep their live modes (real Homes are 0755); only ownership
    # and the directory type are enforced. Output modes are normalized on copy.
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f'{label} must be an owned directory: {path}')
    return path, info


def _exclusion_matcher(extra):
    prefixes = sorted(set(DEFAULT_EXCLUDED_PREFIXES) | set(extra))
    def matches(relative, name):
        if name in EXCLUDED_NAMES:
            return True
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in EXCLUDED_PATTERNS):
            return True
        return any(relative == prefix or relative.startswith(prefix + '/') for prefix in prefixes)
    return matches, prefixes


def _hash_source_file(path, *, follow):
    flags = os.O_RDONLY | os.O_NONBLOCK
    if not follow:
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError('Source files must be regular and owned')
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(fd, 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
            if size > MAX_FILE:
                raise ValueError('Source file exceeds the snapshot file limit')
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('Source file changed during capture')
        return digest.hexdigest(), size
    finally:
        os.close(fd)


def _catalog_names(db_dir):
    names = []
    for name in CATALOG_NAMES:
        try:
            info = (db_dir / name).lstat()
        except FileNotFoundError:
            if name == 'catalog.sqlite3':
                raise ValueError('Source catalog is missing')
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Catalog sidecars must be regular owned files')
        names.append(name)
    return names


def _catalog_digests(db_dir):
    return {name: _hash_source_file(db_dir / name, follow=False)[0] for name in _catalog_names(db_dir)}


def _prove_catalog_quiescent(db_dir, hook=None):
    """Two-pass digest proof around an fsync barrier; the catalog never opens.

    hook is test-only: it runs between the passes to simulate a live writer.
    """
    first = _catalog_digests(db_dir)
    fd = os.open(db_dir / 'catalog.sqlite3', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_directory(db_dir)
    if hook is not None:
        hook(db_dir)
    second = _catalog_digests(db_dir)
    if first != second:
        raise ValueError('Source catalog is not quiesced; stop writers and retry')
    return second


def _copy_raw(source, target):
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Catalog must be a regular owned file')
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            _private(os.fstat(out))
            while chunk := os.read(fd, 1024 * 1024):
                view = memoryview(chunk)
                while view:
                    written = os.write(out, view)
                    if written <= 0:
                        raise OSError('Short catalog working copy write')
                    view = view[written:]
            os.fsync(out)
        finally:
            os.close(out)
    finally:
        os.close(fd)


def _delete_dependents(connection, document_id):
    """Resolve foreign key violations left by the document deletion.

    A violation is resolved only by deleting the referencing row, and only when
    the referenced table is knowledge_documents, the referencing table is in
    DEPENDENT_DELETE_ALLOWLIST and the row itself still exists and points at
    the deleted document. Anything else refuses the repair; at most
    MAX_DEPENDENT_DELETIONS rows may be deleted. Returns the receipt entries.
    """
    violations = connection.execute('PRAGMA foreign_key_check').fetchall()
    deletions = []
    for table, rowid, referenced, fk_index in violations:
        if referenced != 'knowledge_documents':
            raise ValueError('Repair left a foreign key violation outside knowledge_documents')
        if table not in DEPENDENT_DELETE_ALLOWLIST:
            raise ValueError('Repair left a foreign key violation outside the dependent allowlist')
        if len(deletions) >= MAX_DEPENDENT_DELETIONS:
            raise ValueError('Repair exceeded the dependent deletion limit')
        if rowid is None:
            raise ValueError('Repair violation has no addressable row')
        keys = [key for key in connection.execute(f'PRAGMA foreign_key_list({table})').fetchall()
                if key[0] == fk_index and key[2] == 'knowledge_documents']
        columns = sorted({key[3] for key in keys})
        if len(columns) != 1:
            raise ValueError('Repair violation has no single referencing column')
        rows = connection.execute(f'SELECT id, "{columns[0]}" FROM {table} WHERE rowid = ?',
                                  (rowid,)).fetchall()
        if len(rows) != 1:
            raise ValueError('Repair dependent row is missing')
        if rows[0][1] != document_id:
            raise ValueError('Repair violation does not reference the deleted document')
        connection.execute(f'DELETE FROM {table} WHERE rowid = ?', (rowid,))
        deletions.append({'table': table, 'id': rows[0][0]})
    return deletions


def _recommit_document(connection, document_id, reason):
    """Rebind one document's recorded body digest and size to the actual bytes.

    Real Homes drift: a Markdown body edited after import no longer matches
    the digest the catalog recorded, and the migration chain correctly refuses
    it. For PDF-converted rows the content digest binds the original PDF and
    the body digest lives in doc_metadata.markdown_sha256; only that body
    binding is recommitted, leaving the original binding untouched. A document
    that is not drifted refuses the recommit. Returns the receipt entry.
    """
    try:
        rows = connection.execute(
            'SELECT id, title, storage_path, content_sha256, size_bytes, doc_metadata'
            ' FROM knowledge_documents WHERE id = ?', (document_id,)).fetchall()
    except sqlite3.Error as error:
        raise ValueError('Catalog knowledge_documents lookup failed') from error
    if len(rows) != 1:
        raise ValueError('Recommit document is not present in the catalog')
    row_id, title, storage, content_digest, recorded_size, metadata_raw = rows[0]
    if not isinstance(storage, str) or not storage:
        raise ValueError('Recommit document has no storage path')
    digest, size = _hash_source_file(_path(storage), follow=False)
    updates = {}
    metadata = None
    if isinstance(metadata_raw, str) and metadata_raw:
        try:
            metadata = json.loads(metadata_raw)
        except json.JSONDecodeError:
            metadata = None
    if isinstance(metadata, dict) and isinstance(metadata.get('markdown_sha256'), str):
        before = metadata['markdown_sha256'].removeprefix('sha256:')
        if before != digest:
            metadata['markdown_sha256'] = digest
            connection.execute('UPDATE knowledge_documents SET doc_metadata = ? WHERE id = ?',
                               (json.dumps(metadata, ensure_ascii=False, separators=(',', ':')),
                                document_id))
            updates['markdown_sha256'] = {'from': before, 'to': digest}
    else:
        before = content_digest.removeprefix('sha256:') if isinstance(content_digest, str) else content_digest
        if before != digest:
            connection.execute('UPDATE knowledge_documents SET content_sha256 = ? WHERE id = ?',
                               (digest, document_id))
            updates['content_sha256'] = {'from': content_digest, 'to': digest}
    if recorded_size != size:
        connection.execute('UPDATE knowledge_documents SET size_bytes = ? WHERE id = ?',
                           (size, document_id))
        updates['size_bytes'] = {'from': recorded_size, 'to': size}
    if not updates:
        raise ValueError('Recommit document is not drifted')
    return {'document': {'id': row_id, 'title': title, 'storage_path': storage},
            'updates': updates, 'reason': reason}


def _repair_catalog(db_dir, before_digests, delete, recommit, work):
    """Adjust a private working copy of the catalog; the source is never opened.

    delete=(document_id, reason) deletes one knowledge_documents row; rows in
    allowlisted tables left dangling by the deletion (the real Home's
    knowledge_source_items import record of the same content) are deleted as
    recorded dependents, and any other foreign key violation refuses the
    repair. recommit=(document_id, reason) rebinds one drifted body digest and
    size to the actual source bytes. The real catalog is never opened with
    sqlite. Returns the repair receipt, the repaired single-file digest and
    its size.
    """
    for name, digest in before_digests.items():
        target = work / name
        _copy_raw(db_dir / name, target)
        if _hash_source_file(target, follow=False)[0] != digest:
            raise ValueError('Catalog working copy diverged from the source')
    catalog = work / 'catalog.sqlite3'
    connection = sqlite3.connect(catalog)
    try:
        delete_entry = None
        if delete is not None:
            document_id, reason = delete
            try:
                rows = connection.execute(
                    'SELECT id, title, storage_path FROM knowledge_documents WHERE id = ?',
                    (document_id,)).fetchall()
            except sqlite3.Error as error:
                raise ValueError('Catalog knowledge_documents lookup failed') from error
            if len(rows) != 1:
                raise ValueError('Repair document is not present in the catalog')
            row = rows[0]
            connection.execute('DELETE FROM knowledge_documents WHERE id = ?', (document_id,))
            delete_entry = {'document': {'id': row[0], 'title': row[1], 'storage_path': row[2]},
                            'dependent_deletions': _delete_dependents(connection, document_id),
                            'reason': reason}
        recommit_entry = None
        if recommit is not None:
            recommit_entry = _recommit_document(connection, recommit[0], recommit[1])
        if connection.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Catalog foreign key check failed after repair')
        if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Catalog integrity check failed after repair')
        connection.commit()
        checkpoint = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()
        if checkpoint and checkpoint[0][0]:
            raise ValueError('Repaired catalog checkpoint is busy')
    finally:
        connection.close()
    if any((work / name).exists() for name in CATALOG_NAMES[1:]):
        raise ValueError('Repaired catalog still has sidecar files')
    digest, size = _hash_source_file(catalog, follow=False)
    receipt = {'format': REPAIR_FORMAT,
               'delete': delete_entry,
               'recommit': recommit_entry,
               'before_digests': dict(sorted(before_digests.items())),
               'after_digest': digest,
               'size_bytes': size,
               'foreign_key_check': 'clean',
               'integrity_check': 'ok'}
    return receipt, digest, size


def _mkdir(path):
    if path.exists():
        _private(path.lstat(), directory=True)
        return
    _mkdir(path.parent)
    path.mkdir(mode=0o700)
    _sync_directory(path.parent)


def _publish(path, data):
    part = path.with_name('.' + path.name + PART_SUFFIX)
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        _private(os.fstat(fd))
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError('Short snapshot write')
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(part, path)
    _sync_directory(path.parent)


def _copy_planned(target, fact):
    _mkdir(target.parent)
    part = target.with_name('.' + target.name + PART_SUFFIX)
    flags = os.O_RDONLY | os.O_NONBLOCK
    if not fact['follow']:
        flags |= os.O_NOFOLLOW
    fd = os.open(fact['source'], flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Source files must be regular and owned')
        out = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            _private(os.fstat(out))
            digest = hashlib.sha256()
            size = 0
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(out, view)
                    if written <= 0:
                        raise OSError('Short snapshot payload write')
                    view = view[written:]
            os.fsync(out)
        finally:
            os.close(out)
    finally:
        os.close(fd)
    if size != fact['size'] or digest.hexdigest() != fact['sha256']:
        raise ValueError('Source file changed during capture')
    os.replace(part, target)
    _sync_directory(target.parent)


def _manifest(plan_raw, inventory):
    # Fully determined by the admission contract in source_snapshot.py.
    return {'format': FORMAT, 'plan_digest': _sha(plan_raw), 'state': 'verified_raw_home_snapshot',
            'inventory': inventory, 'cooperative_admission_held': True, 'writer_fence_verified': False,
            'catalog_normalization_required': True, 'activation_allowed': False,
            'installation_prepared': False, 'credential_rebind_required': True,
            'requires_domain_migration': True, 'source_home_kind': 'legacy_puddingclaw_home',
            'catalog': {'relative_path': 'db/catalog.sqlite3', 'role': 'legacy_claw_core_catalog',
                        'direct_import_allowed': False, 'wal_policy': 'captured_as_sibling_files'}}


def _publish_receipt(path, data, *, root, source, graft_sources):
    path = _path(path)
    if path == root or root in path.parents:
        raise ValueError('Receipt must not live inside the snapshot root')
    if path == source or source in path.parents:
        raise ValueError('Receipt must not live inside the read-only source home')
    if any(path == graft or graft in path.parents for graft in graft_sources):
        raise ValueError('Receipt must not live inside a graft source')
    if path.exists():
        _private(path.lstat())
    else:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _publish(path, data)


def produce_rehearsal_snapshot(source_home, output, *, grafts=(), exclusions=(),
                               follow_symlinks=False, repair_document=None,
                               repair_reason=None, recommit_document=None,
                               recommit_reason=None, receipt=None, _quiescence_hook=None):
    """Assemble and self-verify a puddingclaw-source-home-snapshot/v1 envelope.

    Re-running into the same output root with identical inputs is byte-identical
    and reports idempotent=True; a root produced from different inputs refuses.
    """
    if type(follow_symlinks) is not bool:
        raise ValueError('Invalid follow_symlinks option')
    if (repair_document is None) != (repair_reason is None):
        raise ValueError('Repair requires both a document id and a reason')
    if repair_document is not None and (not repair_document or not repair_reason):
        raise ValueError('Repair document and reason must be non-empty')
    if (recommit_document is None) != (recommit_reason is None):
        raise ValueError('Recommit requires both a document id and a reason')
    if recommit_document is not None and (not recommit_document or not recommit_reason):
        raise ValueError('Recommit document and reason must be non-empty')
    if repair_document is not None and repair_document == recommit_document:
        raise ValueError('Repair and recommit must target different documents')
    adjustments = repair_document is not None or recommit_document is not None
    source, source_info = _owned_directory(source_home, 'Source home')
    root = _path(output)
    if root == source or root in source.parents or source in root.parents:
        raise ValueError('Output root must be disjoint from the source home')
    graft_specs = []
    for graft in grafts:
        graft_source, _ = _owned_directory(graft[0], 'Graft source')
        destination = _relative(str(graft[1]))
        if root == graft_source or root in graft_source.parents or graft_source in root.parents:
            raise ValueError('Output root must be disjoint from graft sources')
        graft_specs.append((graft_source, destination))
    for index, (_, first) in enumerate(graft_specs):
        for _, second in graft_specs[index + 1:]:
            if first == second or first.startswith(second + '/') or second.startswith(first + '/'):
                raise ValueError('Graft destinations collide')
    extra = [_relative(str(item)) for item in exclusions]
    matches, prefixes = _exclusion_matcher(extra)
    if adjustments and (matches('db', 'db') or matches(CATALOG, 'catalog.sqlite3')):
        raise ValueError('Repair conflicts with a catalog exclusion')

    planned_files = {}
    planned_dirs = set()
    total = 0

    def add_directory(relative):
        if relative in planned_dirs:
            return
        if len(planned_files) + len(planned_dirs) >= MAX_ENTRIES:
            raise ValueError('Snapshot entry limit exceeded')
        planned_dirs.add(relative)

    def add_file(relative, path, *, follow):
        nonlocal total
        digest, size = _hash_source_file(path, follow=follow)
        if len(planned_files) + len(planned_dirs) >= MAX_ENTRIES:
            raise ValueError('Snapshot entry limit exceeded')
        total += size
        if total > MAX_TOTAL:
            raise ValueError('Snapshot total byte limit exceeded')
        planned_files[relative] = {'source': Path(path), 'sha256': digest, 'size': size, 'follow': follow}

    def walk(directory, prefix, match_prefix='', *, skip_catalog=False):
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
        for entry in ordered:
            relative = prefix + entry.name
            match_relative = match_prefix + entry.name
            if skip_catalog and relative in CATALOG_RELS:
                continue
            if matches(match_relative, entry.name):
                continue
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                add_directory(relative)
                walk(entry.path, relative + '/', match_relative + '/', skip_catalog=skip_catalog)
            elif stat.S_ISREG(info.st_mode):
                add_file(relative, entry.path, follow=False)
            elif stat.S_ISLNK(info.st_mode):
                if not follow_symlinks:
                    raise ValueError('Source contains a symlink: ' + relative)
                add_file(relative, entry.path, follow=True)
            else:
                raise ValueError('Source contains a non-regular file: ' + relative)

    walk(source, '', skip_catalog=adjustments)
    graft_summaries = []
    for graft_source, destination in graft_specs:
        paths = set(planned_files) | planned_dirs
        if any(p == destination or p.startswith(destination + '/') or destination.startswith(p + '/')
               for p in paths):
            raise ValueError('Graft destination collides with payload content')
        parts = destination.split('/')
        for depth in range(1, len(parts) + 1):
            add_directory('/'.join(parts[:depth]))
        before_files, before_total = len(planned_files), total
        walk(graft_source, destination + '/')
        graft_summaries.append({'source': str(graft_source), 'destination': destination,
                                'files': len(planned_files) - before_files,
                                'total_bytes': total - before_total})
    graft_summaries.sort(key=lambda graft: graft['destination'])

    catalog_digests = None
    source_db = source / 'db'
    if (adjustments or CATALOG in planned_files) and (source_db / 'catalog.sqlite3').exists():
        catalog_digests = _prove_catalog_quiescent(source_db, _quiescence_hook)

    with ExitStack() as stack:
        if adjustments:
            if catalog_digests is None:
                raise ValueError('Repair requires the source catalog to be present and not excluded')
            work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='puddingclaw-rehearsal-')))
            repair_receipt, digest, size = _repair_catalog(
                source_db, catalog_digests,
                (repair_document, repair_reason) if repair_document is not None else None,
                (recommit_document, recommit_reason) if recommit_document is not None else None,
                work)
            if CATALOG in planned_files or 'db' not in planned_dirs:
                raise ValueError('Repair catalog placement collides with payload content')
            total += size
            if total > MAX_TOTAL:
                raise ValueError('Snapshot total byte limit exceeded')
            planned_files[CATALOG] = {'source': work / 'catalog.sqlite3', 'sha256': digest,
                                      'size': size, 'follow': False}
        else:
            repair_receipt = None

        inventory = {'files': {relative: {'sha256': fact['sha256'], 'size': fact['size']}
                               for relative, fact in planned_files.items()},
                     'directories': sorted(planned_dirs), 'total_bytes': total}
        _validate_inventory(inventory)
        plan = {'format': FORMAT,
                'source_identity': _sha(str(source).encode('utf-8')),
                'source_directory_identity': {'device': source_info.st_dev, 'inode': source_info.st_ino},
                'output_identity': _sha(str(root).encode('utf-8')),
                'inventory': inventory}
        plan_raw = _encoded(plan)
        manifest_raw = _encoded(_manifest(plan_raw, inventory))

        idempotent = False
        commitment = None
        if root.exists():
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError('Output root must be an owned directory')
            if any(root.iterdir()):
                try:
                    with VerifiedSourceSnapshot(root) as snapshot:
                        commitment = snapshot.commitment
                except (ValueError, OSError) as error:
                    raise ValueError('Output root holds an incomplete or foreign snapshot; '
                                     'remove it explicitly before retrying') from error
                if (root / 'plan.json').read_bytes() != plan_raw:
                    raise ValueError('Output root was produced from different inputs; '
                                     'refusing to overwrite')
                idempotent = True
            else:
                os.chmod(root, 0o700)
                _sync_directory(root.parent)
        if commitment is None:
            root.mkdir(mode=0o700)
            _sync_directory(root.parent)
            _mkdir(root / 'payload')
            for relative in sorted(planned_dirs, key=lambda p: (p.count('/'), p)):
                _mkdir(root / 'payload' / relative)
            for relative in sorted(planned_files):
                _copy_planned(root / 'payload' / relative, planned_files[relative])
            actual = _inventory(root / 'payload')
            if (actual['files'] != inventory['files']
                    or sorted(actual['directories']) != inventory['directories']
                    or actual['total_bytes'] != inventory['total_bytes']):
                raise ValueError('Snapshot payload differs from the capture plan')
            _publish(root / 'plan.json', plan_raw)
            _publish(root / 'manifest.json', manifest_raw)
            _publish(root / LOCK_NAME, b'')
            try:
                with VerifiedSourceSnapshot(root) as snapshot:
                    commitment = snapshot.commitment
            except (ValueError, OSError) as error:
                raise ValueError('Producer self-verification failed') from error

    receipt_doc = {'format': RECEIPT_FORMAT,
                   'source_identity': plan['source_identity'],
                   'source_directory_identity': plan['source_directory_identity'],
                   'output': str(root),
                   'follow_symlinks': follow_symlinks,
                   'exclusions': {'names': sorted(EXCLUDED_NAMES), 'patterns': sorted(EXCLUDED_PATTERNS),
                                  'prefixes': prefixes, 'extra': sorted(extra)},
                   'grafts': graft_summaries,
                   'files': len(inventory['files']),
                   'directories': len(inventory['directories']),
                   'total_bytes': inventory['total_bytes'],
                   'repair': repair_receipt,
                   'commitment': commitment}
    if receipt is not None:
        _publish_receipt(receipt, _encoded(receipt_doc), root=root, source=source,
                         graft_sources=[graft for graft, _ in graft_specs])
    return dict(receipt_doc, status='verified', idempotent=idempotent, activation_allowed=False)


def _graft_spec(value):
    source, separator, destination = value.partition('=')
    if not separator or not source or not destination:
        raise ValueError('Grafts must be SRC_DIR=DST_RELATIVE')
    return source, destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-home', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--graft', action='append', default=[], metavar='SRC_DIR=DST_RELATIVE')
    parser.add_argument('--exclude', action='append', default=[], metavar='REL')
    parser.add_argument('--follow-symlinks', action='store_true')
    parser.add_argument('--repair-document', metavar='DOC_ID')
    parser.add_argument('--repair-reason', metavar='TEXT')
    parser.add_argument('--recommit-document', metavar='DOC_ID')
    parser.add_argument('--recommit-reason', metavar='TEXT')
    parser.add_argument('--receipt')
    args = parser.parse_args(argv)
    try:
        grafts = [_graft_spec(value) for value in args.graft]
        result = produce_rehearsal_snapshot(
            args.source_home, args.output, grafts=grafts, exclusions=args.exclude,
            follow_symlinks=args.follow_symlinks, repair_document=args.repair_document,
            repair_reason=args.repair_reason, recommit_document=args.recommit_document,
            recommit_reason=args.recommit_reason, receipt=args.receipt)
    except Exception:
        print(json.dumps({'format': RECEIPT_FORMAT, 'status': 'error',
                          'error_code': 'rehearsal_snapshot_rejected', 'activation_allowed': False},
                         sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


__all__ = ['produce_rehearsal_snapshot', 'RECEIPT_FORMAT', 'REPAIR_FORMAT',
           'DEFAULT_EXCLUDED_PREFIXES', 'EXCLUDED_NAMES', 'EXCLUDED_PATTERNS']
