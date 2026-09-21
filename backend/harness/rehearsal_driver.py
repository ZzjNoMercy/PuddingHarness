"""Checkpointed end-to-end driver for the spec 11.20 point-10 real-data rehearsal.

This module drives the real forward migration chain — source-home snapshot,
Knowledge migration request generation, offline migration orchestration,
manifest discover and prepare, product enrollment, writer suspension, cutover
and finalize — as ordered REAL subprocesses against one private work root, so
the rehearsal exercises the shipped CLIs exactly as an operator would.  This
increment covers PATH A only (forward to PREPARED, CUTOVER, FINALIZED); the
window-rollback path is a separate later increment that extends the ordered
step table — nothing here is path-B specific.

Work-root layout (the work root itself is 0700 and everything in it is owned
by the caller and symlink-free; files are private 0600 single-linked, and
directories are sealed against group/other writes — chain-owned intermediate
directories are admitted with the modes their producers create):

    snapshot/               rehearsal_snapshot envelope (plan/manifest/payload)
    snapshot-receipt.json   producer receipt (lives outside the envelope)
    request/                Knowledge request + generation receipt
    staging/                migration orchestrator staging (harness/, knowledge/)
    knowledge-receipt.json  receipt extracted from the orchestrator result
    manifest/               installation manifest (DISCOVERED..FINALIZED)
    harness-home/           fresh Harness product Home, enrolled
    harness-authority/      Harness writer authority (journal, retired markers)
    knowledge-home/         fresh Knowledge workspace, enrolled
    knowledge-authority/    Knowledge writer authority
    knowledge-setup/        bootstrap fixture catalog/wiki for the workspace
    barrier/                writer suspension barrier checkpoint
    checkpoint/             cutover orchestrator checkpoint
    driver-checkpoint.json  this driver's private checkpoint
    run-record.json         machine-readable rehearsal evidence

Checkpoint contract: driver-checkpoint.json is canonical JSON published
atomically after each committed step, recording {name, idempotent,
receipt, outputs} where outputs maps each owned root to a tree digest taken
after the step committed.  On (re)start every recorded step is re-verified
before it is skipped: each root is re-digested and compared against the latest
committed record for that root (later steps legitimately rewrite the product
homes, authorities and manifest, so the fold takes the newest record).
Divergence, unknown work-root entries, a torn or non-canonical checkpoint, or
a changed parameter set with committed steps all refuse closed.  Steps
themselves are already idempotent: a re-run of a completed scenario verifies
every step and republishes a byte-identical run record; a SIGKILL between
steps resumes at the first uncommitted step, and a SIGKILL mid-step re-runs
that step — the step CLIs recover their own interrupted publications, and the
driver resets only the outputs it owns outright for steps that never
committed (a torn snapshot envelope fails its own admission and is rebuilt;
the request directory and the enrollment roots are regenerated).

The run record (run-record.json) is canonical JSON listing each step, the
digest of its committed receipt and its output digests, plus the terminal
FINALIZED bindings.  It carries digests, labels and counts only — no secrets
and no row content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

from harness import installation_authority as authority
from harness import installation_manifest as manifests
from harness import session_import
from harness.home_freeze import _sync_directory
from harness.installation_guard import FREEZE_NAME as HARNESS_FREEZE_NAME, InstallationGuard
from harness.knowledge_writer_receipt import FREEZE_NAME as KNOWLEDGE_FREEZE_NAME, inspect_binding
from harness.migration_orchestrator import (
    _digest,
    _json,
    _path,
    _read_private,
    _replace_private,
    _validate_executable,
)
from harness.rehearsal_snapshot import _mkdir
from harness.source_snapshot import (
    MAX_ENTRIES, MAX_FILE, MAX_TOTAL, VerifiedSourceSnapshot, _encoded, _relative,
)

FORMAT = 'puddingharness-rehearsal-driver/v1'
RUN_FORMAT = 'puddingharness-rehearsal-driver-run/v1'
CHECKPOINT_FORMAT = 'puddingharness-rehearsal-driver-checkpoint/v1'
CHECKPOINT_NAME = 'driver-checkpoint.json'
RUN_RECORD_NAME = 'run-record.json'
REQUEST_FORMAT = 'puddingknowledge-claw-migration-request-receipt/v1'
DEFAULT_WIKI_ROOT = 'llm-wiki'
DEFAULT_TIMEOUT_SECONDS = 1800
ENROLL_HARNESS = 'enroll-harness'
ENROLL_KNOWLEDGE = 'enroll-knowledge'
_MAX_STEP_OUTPUT = 8 * 1024 * 1024
_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,79}')

# The Knowledge workspace bootstrap is library-only upstream; this mirrors the
# cross-product test incantation (fixture catalog migrated to latest, one
# space/dataset row, a one-page wiki, then a persistent workspace open).
# Fixture bytes are normalized private so the driver tree digest admits them.
_KNOWLEDGE_WORKSPACE_SETUP = '''
import os
from pathlib import Path
import sys
from sqlalchemy import create_engine, text
from knowledge_platform.catalog.migrations import migrate_to_latest
from knowledge_platform.local.workspace import open_persistent_workspace
root = Path(sys.argv[1]); workspace = Path(sys.argv[2])
engine = create_engine('sqlite:///' + str(root / 'source.sqlite3'))
with engine.begin() as connection:
    migrate_to_latest(connection)
    connection.execute(text("INSERT INTO knowledge_spaces (id,name,description,permissions_json,created_at,updated_at) VALUES ('space_kb_default','rehearsal','local','{}','now','now')"))
    connection.execute(text("INSERT INTO knowledge_datasets (id,space_id,name,version,kind,description,asset_ids,semantic_asset_ids,capabilities,freshness,permissions_json,manifest_digest,created_at,updated_at) VALUES ('dataset_kb_default','space_kb_default','rehearsal','1','wiki','local','[]','[]','[]','{}','{}','','now','now')"))
engine.dispose()
for suffix in ('', '-wal', '-shm', '-journal'):
    candidate = root / ('source.sqlite3' + suffix)
    if candidate.exists(): os.chmod(candidate, 0o600)
wiki = root / 'source-wiki'
wiki.mkdir(mode=0o700)
(wiki / 'guide.md').write_text('# Rehearsal')
os.chmod(wiki / 'guide.md', 0o600)
with open_persistent_workspace(workspace, catalog=root / 'source.sqlite3', wiki_root=wiki):
    pass
'''


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _is_digest(value):
    return (isinstance(value, str) and value.startswith('sha256:')
            and len(value) == 71 and all(c in '0123456789abcdef' for c in value[7:]))


def _is_hex64(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


class _StepFailure(Exception):
    def __init__(self, step, error_code):
        super().__init__(step)
        self.step = step
        self.error_code = error_code


def _hash_private_fd(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        raise ValueError('Driver artifact is not a private owned file')
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
        if size > MAX_FILE:
            raise ValueError('Driver artifact exceeds the snapshot file limit')
    after = os.fstat(fd)
    if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('Driver artifact changed during verification')
    return digest.hexdigest(), size


def _file_digest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return _hash_private_fd(fd)
    finally:
        os.close(fd)


def _owned_tree_directory(info):
    # Chain-owned trees are admitted as produced: intermediate writers create
    # parent directories with default modes, so a directory is required to be
    # owned and sealed against group/other writes, not to be mode 0700.  The
    # work root itself is 0700, so traversal is already closed to others.
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('Driver artifact is not an owned sealed directory')


def _tree_inventory(root):
    """Inventory one owned chain tree: names, modes, sizes and byte digests."""
    files = {}
    directories = {}
    total = 0
    count = 0

    def walk(fd, prefix=''):
        nonlocal total, count
        for entry in sorted(os.scandir(fd), key=lambda item: item.name):
            count += 1
            if count > MAX_ENTRIES:
                raise ValueError('Driver artifact entry limit exceeded')
            relative = prefix + entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    _owned_tree_directory(os.fstat(child))
                    directories[relative] = stat.S_IMODE(os.fstat(child).st_mode)
                    walk(child, relative + '/')
                finally:
                    os.close(child)
            else:
                child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    digest, size = _hash_private_fd(child)
                finally:
                    os.close(child)
                total += size
                if total > MAX_TOTAL:
                    raise ValueError('Driver artifact byte limit exceeded')
                files[relative] = {'sha256': digest, 'size': size,
                                   'mode': stat.S_IMODE(info.st_mode)}
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _owned_tree_directory(os.fstat(fd))
        walk(fd)
    finally:
        os.close(fd)
    return {'files': files, 'directories': directories, 'total_bytes': total}


def _root_digest(path):
    """Commit one owned root: a file's bytes or a directory's full inventory."""
    if path.is_symlink():
        raise ValueError('Driver artifact is a symlink')
    if path.is_dir():
        inventory = _tree_inventory(path)
    else:
        digest, size = _file_digest(path)
        inventory = {'files': {'': {'sha256': digest, 'size': size, 'mode': 0o600}},
                     'directories': {}, 'total_bytes': size}
    return _digest(_encoded(inventory))


def _fold_outputs(steps):
    folded = {}
    for step in steps:
        folded.update(step['outputs'])
    return folded


def _verify_committed_roots(work, steps):
    for relative, expected in _fold_outputs(steps).items():
        if _root_digest(work / relative) != expected:
            raise ValueError('Committed rehearsal output diverged: ' + relative)


_CLEANABLE = re.compile(
    r'\.(?:driver-checkpoint\.json|run-record\.json|knowledge-receipt\.json)\.tmp-[0-9a-f]{16}\Z'
    r'|\.snapshot-receipt\.json\.rehearsal-part\Z')
_KNOWN_TOP_LEVEL = {
    'snapshot', 'snapshot-receipt.json', 'request', 'staging', 'knowledge-receipt.json',
    'manifest', 'harness-home', 'harness-authority', 'knowledge-home', 'knowledge-authority',
    'knowledge-setup', 'barrier', 'checkpoint', CHECKPOINT_NAME, RUN_RECORD_NAME,
}


def _clean_transients(work):
    for entry in work.iterdir():
        if _CLEANABLE.fullmatch(entry.name):
            _read_private(entry)
            entry.unlink()
            _sync_directory(work)


def _check_top_level(work):
    for entry in work.iterdir():
        if entry.is_symlink() or entry.name not in _KNOWN_TOP_LEVEL:
            raise ValueError('Unknown rehearsal work-root entry')


def _load_checkpoint(work):
    path = work / CHECKPOINT_NAME
    if not path.exists() and not path.is_symlink():
        return None
    raw = _read_private(path)
    try:
        value = _json(raw)
    except ValueError:
        raise ValueError('Rehearsal driver checkpoint is torn') from None
    if _encoded(value) != raw:
        raise ValueError('Rehearsal driver checkpoint is not canonical')
    if (set(value) != {'format', 'work_root', 'operation', 'installation_id',
                       'parameters_digest', 'steps'}
            or value['format'] != CHECKPOINT_FORMAT or value['work_root'] != str(work)
            or not _is_digest(value['parameters_digest'])
            or not isinstance(value['steps'], list)):
        raise ValueError('Rehearsal driver checkpoint is invalid')
    authority._operation(value['operation'])
    if not isinstance(value['installation_id'], str) or not _TOKEN.fullmatch(value['installation_id']):
        raise ValueError('Rehearsal driver checkpoint identity is invalid')
    steps = value['steps']
    if len(steps) > len(STEPS):
        raise ValueError('Rehearsal driver checkpoint steps are invalid')
    for index, step in enumerate(steps):
        name, roots, _body = STEPS[index]
        if (not isinstance(step, dict)
                or set(step) != {'name', 'idempotent', 'receipt', 'outputs'}
                or step['name'] != name or type(step['idempotent']) is not bool
                or not isinstance(step['receipt'], dict)
                or set(step['outputs']) != set(roots)
                or any(not _is_digest(digest) for digest in step['outputs'].values())):
            raise ValueError('Rehearsal driver checkpoint step is invalid')
    return value


def _run_cli(command, timeout, step, *, expect_json=True):
    try:
        child = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                               timeout=timeout)
    except subprocess.TimeoutExpired:
        raise _StepFailure(step, 'step_timed_out') from None
    except OSError:
        raise _StepFailure(step, 'step_launch_failed') from None
    if len(child.stdout) > _MAX_STEP_OUTPUT:
        raise _StepFailure(step, 'step_output_unbounded')
    if child.returncode != 0:
        code = None
        if child.stdout:
            try:
                report = json.loads(child.stdout)
                if isinstance(report, dict) and isinstance(report.get('error_code'), str):
                    code = report['error_code']
            except (UnicodeError, json.JSONDecodeError):
                code = None
        raise _StepFailure(step, code or 'step_failed')
    if not expect_json:
        if child.stdout.strip():
            raise _StepFailure(step, 'step_receipt_invalid')
        return None
    try:
        result = json.loads(child.stdout)
    except (UnicodeError, json.JSONDecodeError):
        raise _StepFailure(step, 'step_receipt_invalid') from None
    if not isinstance(result, dict):
        raise _StepFailure(step, 'step_receipt_invalid')
    return result


class _Context:
    def __init__(self, args, work, knowledge_python):
        self.work = work
        self.knowledge_python = str(knowledge_python)
        self.source_home = str(_path(args.source_home))
        self.installation_id = args.installation_id
        self.source_revision = args.source_revision
        self.source_schema_revision = args.source_schema_revision
        self.operation = args.operation
        self.grafts = list(args.graft)
        self.mappings = list(args.mappings)
        self.repair_document = args.repair_document
        self.repair_reason = args.repair_reason
        self.exclusions = list(args.exclude)
        self.wiki_root = args.wiki_root_relative
        self.timeout = args.timeout_seconds
        self.committed = {}

    def receipt(self, name):
        return self.committed[name]['receipt']

    def path(self, name):
        return self.work / name


def _step_snapshot(ctx):
    root = ctx.path('snapshot')
    if root.is_symlink():
        raise ValueError('Rehearsal snapshot path is a symlink')
    if root.exists():
        try:
            with VerifiedSourceSnapshot(root):
                complete = True
        except (ValueError, OSError):
            complete = False
        if not complete:
            # A torn envelope is the product of this driver's interrupted run;
            # the producer deliberately refuses to overwrite one, so the
            # driver removes its own partial output before re-producing.
            shutil.rmtree(root)
    command = [sys.executable, '-m', 'harness.rehearsal_snapshot',
               '--source-home', ctx.source_home, '--output', str(root),
               '--receipt', str(ctx.path('snapshot-receipt.json'))]
    for graft in ctx.grafts:
        command += ['--graft', graft]
    for exclusion in ctx.exclusions:
        command += ['--exclude', exclusion]
    if ctx.repair_document is not None:
        command += ['--repair-document', ctx.repair_document, '--repair-reason', ctx.repair_reason]
    result = _run_cli(command, ctx.timeout, 'snapshot')
    commitment = result.get('commitment')
    if (result.get('status') != 'verified' or result.get('activation_allowed') is not False
            or type(result.get('idempotent')) is not bool
            or not isinstance(commitment, dict)
            or commitment.get('format') != 'puddingclaw-source-home-snapshot/v1'
            or not _is_hex64(commitment.get('plan_sha256'))
            or not _is_hex64(commitment.get('manifest_sha256'))):
        raise _StepFailure('snapshot', 'step_receipt_invalid')
    receipt = _json(_read_private(ctx.path('snapshot-receipt.json')))
    if receipt.get('commitment') != commitment or receipt.get('output') != str(root):
        raise _StepFailure('snapshot', 'step_receipt_invalid')
    for key in ('files', 'directories', 'total_bytes'):
        if type(result.get(key)) is not int or result[key] < 0:
            raise _StepFailure('snapshot', 'step_receipt_invalid')
    repair = result.get('repair')
    return ({'commitment': commitment, 'files': result['files'],
             'directories': result['directories'], 'total_bytes': result['total_bytes'],
             'repaired': repair is not None},
            result['idempotent'])


def _step_request(ctx):
    request = ctx.path('request')
    if request.is_symlink():
        raise ValueError('Rehearsal request path is a symlink')
    if request.exists():
        shutil.rmtree(request)
    request.mkdir(mode=0o700)
    snapshot = ctx.path('snapshot')
    command = [ctx.knowledge_python, '-m', 'knowledge_platform.distribution.claw_migration_request',
               '--snapshot-root', str(snapshot),
               '--catalog', str(snapshot / 'payload/db/catalog.sqlite3'),
               '--files-root', str(snapshot / 'payload'),
               '--wiki-root', str(snapshot / 'payload' / ctx.wiki_root),
               '--installation-id', ctx.installation_id,
               '--source-revision', ctx.source_revision,
               '--source-schema-revision', ctx.source_schema_revision,
               '--output', str(request / 'request.json'),
               '--receipt', str(request / 'receipt.json')]
    for mapping in ctx.mappings:
        command += ['--map', mapping]
    result = _run_cli(command, ctx.timeout, 'request')
    counts = result.get('counts')
    if (result.get('format') != REQUEST_FORMAT or result.get('state') != 'verified_inactive_request'
            or not _is_digest(result.get('request_digest')) or not _is_digest(result.get('catalog_digest'))
            or result.get('unmapped_references') != 0
            or result.get('activation_allowed') is not False
            or result.get('installation_prepared') is not False
            or result.get('complete_installation_migration') is not False
            or not isinstance(counts, dict)
            or any(type(counts.get(key)) is not int or counts[key] < 0
                   for key in ('documents', 'originals', 'attachments', 'bytes'))):
        raise _StepFailure('request', 'step_receipt_invalid')
    return ({'request_digest': result['request_digest'], 'catalog_digest': result['catalog_digest'],
             'counts': counts}, False)


def _step_orchestrate(ctx):
    command = [sys.executable, '-m', 'harness.migration_orchestrator',
               '--source-home-snapshot', str(ctx.path('snapshot')),
               '--knowledge-request', str(ctx.path('request') / 'request.json'),
               '--knowledge-python', ctx.knowledge_python,
               '--staging', str(ctx.path('staging')),
               '--timeout-seconds', str(ctx.timeout)]
    result = _run_cli(command, ctx.timeout, 'orchestrate')
    if (result.get('format') != manifests.ORCHESTRATOR_FORMAT
            or result.get('state') != 'verified_inactive_partial'
            or not _is_digest(result.get('plan_digest')) or not _is_digest(result.get('harness_plan_digest'))
            or result.get('activation_allowed') is not False
            or result.get('installation_prepared') is not False
            or result.get('writer_fence_verified') is not False
            or result.get('credential_rebind_required') is not True
            or result.get('source_snapshot_commitment') != ctx.receipt('snapshot')['commitment']
            or not isinstance(result.get('knowledge_receipt'), dict)):
        raise _StepFailure('orchestrate', 'step_receipt_invalid')
    knowledge_receipt = result['knowledge_receipt']
    canonical = json.dumps(knowledge_receipt, sort_keys=True, separators=(',', ':')).encode()
    receipt_digest = _digest(canonical)
    checkpoint = _json(_read_private(ctx.path('staging') / 'checkpoint.json'))
    if checkpoint.get('receipt_digest') != receipt_digest:
        raise _StepFailure('orchestrate', 'step_receipt_invalid')
    _replace_private(ctx.path('knowledge-receipt.json'), canonical + b'\n')
    return ({'plan_digest': result['plan_digest'], 'harness_plan_digest': result['harness_plan_digest'],
             'knowledge_receipt_digest': receipt_digest}, False)


def _step_discover(ctx):
    command = [sys.executable, '-m', 'harness.installation_manifest', 'discover',
               '--source-snapshot', str(ctx.path('snapshot')),
               '--output', str(ctx.path('manifest') / 'manifest.json')]
    result = _run_cli(command, ctx.timeout, 'discover')
    expected_snapshot = session_import.digest(
        session_import.encoded(ctx.receipt('snapshot')['commitment']))
    if (result.get('format') != manifests.FORMAT or result.get('state') != 'DISCOVERED'
            or type(result.get('idempotent')) is not bool
            or not _is_digest(result.get('manifest_digest'))
            or result.get('snapshot_digest') != expected_snapshot
            or result.get('activation_allowed') is not False):
        raise _StepFailure('discover', 'step_receipt_invalid')
    return ({'state': result['state'], 'manifest_digest': result['manifest_digest'],
             'snapshot_digest': result['snapshot_digest']}, result['idempotent'])


def _step_prepare(ctx):
    command = [sys.executable, '-m', 'harness.installation_manifest', 'prepare',
               '--source-snapshot', str(ctx.path('snapshot')),
               '--orchestrator-staging', str(ctx.path('staging')),
               '--knowledge-receipt', str(ctx.path('knowledge-receipt.json')),
               '--output', str(ctx.path('manifest') / 'manifest.json')]
    result = _run_cli(command, ctx.timeout, 'prepare')
    if (result.get('format') != manifests.FORMAT or result.get('state') != 'PREPARED'
            or type(result.get('idempotent')) is not bool
            or not _is_digest(result.get('manifest_digest'))):
        raise _StepFailure('prepare', 'step_receipt_invalid')
    return ({'state': result['state'], 'manifest_digest': result['manifest_digest']},
            result['idempotent'])


def _step_enroll(ctx):
    for name in ('harness-home', 'harness-authority', 'knowledge-home',
                 'knowledge-authority', 'knowledge-setup'):
        path = ctx.path(name)
        if path.is_symlink():
            raise ValueError('Rehearsal enrollment path is a symlink')
        if path.exists():
            shutil.rmtree(path)
    ctx.path('harness-home').mkdir(mode=0o700)
    _sync_directory(ctx.work)
    ctx.path('knowledge-setup').mkdir(mode=0o700)
    _sync_directory(ctx.work)
    harness = _run_cli([sys.executable, '-m', 'harness.installation_authority', 'enroll',
                        '--home', str(ctx.path('harness-home')),
                        '--authority', str(ctx.path('harness-authority')),
                        '--operation-id', ENROLL_HARNESS], ctx.timeout, 'enroll')
    harness_journal = harness.get('journal')
    harness_events = harness_journal.get('events') if isinstance(harness_journal, dict) else None
    if (harness.get('format') != authority.FORMAT or harness.get('status') != 'ok'
            or not isinstance(harness_events, list) or len(harness_events) != 1
            or harness_events[0].get('state') != 'existing_writer'
            or harness_events[0].get('operation_id') != ENROLL_HARNESS):
        raise _StepFailure('enroll', 'step_receipt_invalid')
    _run_cli([ctx.knowledge_python, '-c', _KNOWLEDGE_WORKSPACE_SETUP,
              str(ctx.path('knowledge-setup')), str(ctx.path('knowledge-home'))],
             ctx.timeout, 'enroll', expect_json=False)
    knowledge = _run_cli([ctx.knowledge_python, '-m', 'knowledge_platform.local.writer_authority',
                          'enroll', '--state-dir', str(ctx.path('knowledge-home')),
                          '--authority', str(ctx.path('knowledge-authority')),
                          '--operation-id', ENROLL_KNOWLEDGE], ctx.timeout, 'enroll')
    knowledge_journal = knowledge.get('journal')
    knowledge_events = knowledge_journal.get('events') if isinstance(knowledge_journal, dict) else None
    if (knowledge.get('format') != 'puddingknowledge-writer-authority/v1'
            or knowledge.get('status') != 'ok'
            or not isinstance(knowledge_events, list) or len(knowledge_events) != 1
            or knowledge_events[0].get('state') != 'existing_writer'
            or knowledge_events[0].get('operation_id') != ENROLL_KNOWLEDGE):
        raise _StepFailure('enroll', 'step_receipt_invalid')
    return ({'harness_journal_digest': _digest(_encoded(harness_journal)),
             'knowledge_journal_digest': _digest(_encoded(knowledge_journal))}, False)


def _step_suspend(ctx):
    result = _run_cli([sys.executable, '-m', 'harness.writer_barrier',
                       '--harness-home', str(ctx.path('harness-home')),
                       '--knowledge-state', str(ctx.path('knowledge-home')),
                       '--knowledge-python', ctx.knowledge_python,
                       '--checkpoint-dir', str(ctx.path('barrier')),
                       '--operation-id', ctx.operation], ctx.timeout, 'suspend')
    journals = result.get('journals')
    if (result.get('format') != 'puddingharness-writer-suspension-barrier/v1'
            or result.get('state') != 'both_writers_suspended'
            or result.get('activation_allowed') is not False
            or result.get('installation_cutover_performed') is not False
            or result.get('rollback_completed') is not False
            or not isinstance(journals, dict)
            or any(journals[side]['events'][-1].get('state') != 'suspended'
                   or journals[side]['events'][-1].get('operation_id') != ctx.operation
                   for side in ('harness', 'knowledge'))):
        raise _StepFailure('suspend', 'step_receipt_invalid')
    return ({'state': result['state']}, False)


def _step_cutover(ctx):
    result = _run_cli([sys.executable, '-m', 'harness.cutover_orchestrator', 'cutover',
                       '--harness-home', str(ctx.path('harness-home')),
                       '--knowledge-state', str(ctx.path('knowledge-home')),
                       '--knowledge-python', ctx.knowledge_python,
                       '--checkpoint-dir', str(ctx.path('checkpoint')),
                       '--manifest', str(ctx.path('manifest') / 'manifest.json'),
                       '--operation-id', ctx.operation,
                       '--timeout-seconds', str(ctx.timeout)], ctx.timeout, 'cutover')
    journals = result.get('journals')
    heads = {}
    if isinstance(journals, dict):
        for side in ('harness', 'knowledge'):
            journal = journals.get(side)
            events = journal.get('events') if isinstance(journal, dict) else None
            if isinstance(events, list) and len(events) == 3:
                heads[side] = events[2]
    prepared = result.get('prepared_manifest_sha256')
    if (result.get('format') != 'puddingharness-cutover-orchestrator/v1'
            or result.get('state') != 'both_thawed'
            or result.get('installation_cutover_performed') is not True
            or result.get('activation_allowed') is not False
            or result.get('rollback_completed') is not False
            or result.get('production_activated') is not False
            or set(heads) != {'harness', 'knowledge'}
            or not _is_hex64(prepared)
            or 'sha256:' + prepared != ctx.receipt('prepare')['manifest_digest']
            or not _is_hex64(result.get('cutover_manifest_sha256'))
            or not _is_hex64(result.get('active_pointer_sha256'))
            or not _is_hex64(result.get('harness_thaw_receipt_sha256'))
            or not _is_hex64(result.get('knowledge_thaw_receipt_sha256'))):
        raise _StepFailure('cutover', 'step_receipt_invalid')
    for side, head in heads.items():
        if (head.get('operation_id') != ctx.operation
                or head.get('migration_manifest_sha256') != prepared
                or head.get('active_installation_revision') != 'sha256:' + prepared
                or head.get('rollback_evidence_sha256') is not None
                or not _is_hex64(head.get('sha256'))):
            raise _StepFailure('cutover', 'step_receipt_invalid')
    if (heads['harness'].get('writer') != 'puddingharness'
            or heads['knowledge'].get('writers') != {'knowledge_catalog': 'puddingknowledge',
                                                     'connector_jobs': 'puddingknowledge'}):
        raise _StepFailure('cutover', 'step_receipt_invalid')
    return ({'prepared_manifest_sha256': prepared,
             'cutover_manifest_sha256': result['cutover_manifest_sha256'],
             'active_pointer_sha256': result['active_pointer_sha256'],
             'harness_thaw_receipt_sha256': result['harness_thaw_receipt_sha256'],
             'knowledge_thaw_receipt_sha256': result['knowledge_thaw_receipt_sha256'],
             'harness_assigned_event_sha256': heads['harness']['sha256'],
             'knowledge_assigned_event_sha256': heads['knowledge']['sha256']}, False)


def _step_finalize(ctx):
    result = _run_cli([sys.executable, '-m', 'harness.cutover_orchestrator', 'finalize',
                       '--manifest', str(ctx.path('manifest') / 'manifest.json'),
                       '--checkpoint-dir', str(ctx.path('checkpoint'))], ctx.timeout, 'finalize')
    if (result.get('format') != 'puddingharness-cutover-orchestrator/v1'
            or result.get('state') != 'FINALIZED'
            or type(result.get('idempotent')) is not bool
            or not _is_digest(result.get('manifest_digest'))
            or result.get('installation_cutover_performed') is not True
            or result.get('activation_allowed') is not False
            or result.get('rollback_completed') is not False
            or result.get('production_activated') is not False):
        raise _StepFailure('finalize', 'step_receipt_invalid')
    return ({'state': result['state'], 'manifest_digest': result['manifest_digest']},
            result['idempotent'])


# Ordered path-A steps: (name, owned work-root-relative output roots, body).
# 'reset' steps (snapshot, request, enroll) own their outputs outright and
# rebuild them inside the body whenever the step is uncommitted; every other
# step relies on the step CLI's own crash recovery.  The window-rollback
# increment extends this table.
STEPS = (
    ('snapshot', ('snapshot', 'snapshot-receipt.json'), _step_snapshot),
    ('request', ('request',), _step_request),
    ('orchestrate', ('staging', 'knowledge-receipt.json'), _step_orchestrate),
    ('discover', ('manifest',), _step_discover),
    ('prepare', ('manifest',), _step_prepare),
    ('enroll', ('harness-home', 'harness-authority', 'knowledge-home',
                'knowledge-authority', 'knowledge-setup'), _step_enroll),
    ('suspend', ('barrier', 'harness-home', 'harness-authority',
                 'knowledge-home', 'knowledge-authority'), _step_suspend),
    ('cutover', ('checkpoint', 'manifest', 'harness-home', 'harness-authority',
                 'knowledge-home', 'knowledge-authority'), _step_cutover),
    ('finalize', ('manifest',), _step_finalize),
)


def _verify_terminal(ctx):
    """Mirror the cutover/finalize test assertions against the real run."""
    cutover = ctx.receipt('cutover')
    finalize = ctx.receipt('finalize')
    raw = _read_private(ctx.path('manifest') / 'manifest.json')
    document = _json(raw)
    manifests.validate_manifest(document)
    if (document['state'] != 'FINALIZED' or document['rollback_window_open'] is not False
            or not isinstance(document.get('completed_at'), str) or not document['completed_at']):
        raise ValueError('Rehearsal manifest is not finalized')
    if finalize['manifest_digest'] != 'sha256:' + _sha256(raw):
        raise ValueError('Finalized manifest digest changed')
    if document['active_writers'] != manifests._CUTOVER_WRITERS:
        raise ValueError('Finalized manifest writers drifted')
    prepared = cutover['prepared_manifest_sha256']
    if document['active_installation_revision'] != 'sha256:' + prepared:
        raise ValueError('Finalized manifest lost the committed prepared revision')
    for key, side in (('harness_assigned_event_sha256', 'harness'),
                      ('knowledge_assigned_event_sha256', 'knowledge')):
        if document['checkpoint'].get(key) != 'sha256:' + cutover[side + '_assigned_event_sha256']:
            raise ValueError('Finalized manifest lost an assigned event registration')
    harness_binding = authority.load_binding(ctx.path('harness-home'))
    knowledge_binding = inspect_binding(ctx.path('knowledge-home'))
    if harness_binding is None:
        raise ValueError('Harness Home enrollment is missing')
    harness_authority = Path(harness_binding['authority']['path'])
    knowledge_authority = Path(knowledge_binding['authority']['path'])
    harness_journal = authority.journal(harness_binding)
    knowledge_journal = authority.read(knowledge_authority / 'journal.json')
    for journal, writer, side in ((harness_journal, 'puddingharness', 'harness'),
                                  (knowledge_journal, 'puddingknowledge', 'knowledge')):
        head = manifests._assigned_event(journal, writer)
        if (head['sha256'] != cutover[side + '_assigned_event_sha256']
                or head['operation_id'] != ctx.operation
                or head['migration_manifest_sha256'] != prepared
                or head['active_installation_revision'] != 'sha256:' + prepared):
            raise ValueError('Committed writer journal head changed')
    for home, marker in ((ctx.path('harness-home'), HARNESS_FREEZE_NAME),
                         (ctx.path('knowledge-home'), KNOWLEDGE_FREEZE_NAME)):
        if (home / marker).exists() or (home / marker).is_symlink():
            raise ValueError('A writer freeze marker survived the rehearsal thaw')
    for root in (harness_authority, knowledge_authority):
        for name in ('freeze-marker-rev2.json', 'thaw-receipt-rev2.json'):
            authority.read(root / name)
    harness_thaw = authority.read(harness_authority / 'thaw-receipt-rev2.json')
    if _sha256(authority.encoded(harness_thaw)) != cutover['harness_thaw_receipt_sha256']:
        raise ValueError('Harness thaw receipt changed')
    thaw = authority.read(knowledge_authority / 'thaw-receipt-rev2.json')
    if _sha256(authority.encoded(thaw)) != cutover['knowledge_thaw_receipt_sha256']:
        raise ValueError('Knowledge thaw receipt changed')
    pointer_raw = _read_private(ctx.path('harness-home') / 'active-installation.json')
    if _sha256(pointer_raw) != cutover['active_pointer_sha256']:
        raise ValueError('Active installation pointer changed')
    pointer = _json(pointer_raw)
    if (pointer.get('format') != 'puddingharness-active-installation/v1'
            or pointer.get('cutover_manifest_sha256') != cutover['cutover_manifest_sha256']
            or pointer.get('prepared_manifest_sha256') != prepared
            or pointer.get('active_installation_revision') != 'sha256:' + prepared
            or pointer.get('harness_assigned_event_sha256') != cutover['harness_assigned_event_sha256']
            or pointer.get('knowledge_assigned_event_sha256') != cutover['knowledge_assigned_event_sha256']
            or pointer.get('active_writers') != manifests._CUTOVER_WRITERS):
        raise ValueError('Active installation pointer is invalid')
    with InstallationGuard(ctx.path('harness-home')):
        pass
    return {'manifest_state': 'FINALIZED', 'manifest_digest': finalize['manifest_digest'],
            'prepared_manifest_digest': 'sha256:' + prepared,
            'cutover_manifest_digest': 'sha256:' + cutover['cutover_manifest_sha256'],
            'active_pointer_digest': 'sha256:' + cutover['active_pointer_sha256'],
            'harness_assigned_event_digest': 'sha256:' + cutover['harness_assigned_event_sha256'],
            'knowledge_assigned_event_digest': 'sha256:' + cutover['knowledge_assigned_event_sha256'],
            'rollback_window_open': False}


def _run_record(checkpoint, terminal):
    return {'format': RUN_FORMAT,
            'operation': checkpoint['operation'],
            'installation_id': checkpoint['installation_id'],
            'parameters_digest': checkpoint['parameters_digest'],
            'steps': [{'name': step['name'], 'idempotent': step['idempotent'],
                       'receipt_digest': _digest(_encoded(step['receipt'])),
                       'outputs': step['outputs']} for step in checkpoint['steps']],
            'terminal': terminal}


def _parameters_digest(args, work, knowledge_python):
    value = {'work_root': str(work), 'source_home': str(_path(args.source_home)),
             'knowledge_python': str(knowledge_python),
             'installation_id': args.installation_id,
             'source_revision': args.source_revision,
             'source_schema_revision': args.source_schema_revision,
             'operation': args.operation,
             'grafts': list(args.graft), 'mappings': list(args.mappings),
             'repair_document': args.repair_document, 'repair_reason': args.repair_reason,
             'exclusions': list(args.exclude), 'wiki_root_relative': args.wiki_root_relative,
             'timeout_seconds': args.timeout_seconds}
    return _digest(_encoded(value))


def run_rehearsal(args):
    if not _TOKEN.fullmatch(args.installation_id):
        raise ValueError('Invalid rehearsal installation identity')
    for value in (args.source_revision, args.source_schema_revision):
        if not _TOKEN.fullmatch(value):
            raise ValueError('Invalid rehearsal source identity')
    authority._operation(args.operation)
    if type(args.timeout_seconds) is not int or not 1 <= args.timeout_seconds <= 3600:
        raise ValueError('Invalid rehearsal step timeout')
    if (args.repair_document is None) != (args.repair_reason is None):
        raise ValueError('Repair requires both a document id and a reason')
    _relative(args.wiki_root_relative)
    for graft in args.graft:
        source, separator, destination = graft.partition('=')
        if not separator or not source or not destination:
            raise ValueError('Grafts must be SRC_DIR=DST_RELATIVE')
        _relative(destination)
    for mapping in args.mappings:
        source, separator, destination = mapping.partition('=')
        if not separator or not source or not destination:
            raise ValueError('Mappings must be SRC=DST')
        _relative(destination)
    if not args.mappings:
        raise ValueError('At least one --map SRC=DST is required')
    knowledge_python = _validate_executable(args.knowledge_python)
    work = _path(args.work_root)
    _mkdir(work)
    _clean_transients(work)
    _check_top_level(work)
    parameters = _parameters_digest(args, work, knowledge_python)
    checkpoint = _load_checkpoint(work)
    if checkpoint is not None and checkpoint['parameters_digest'] != parameters:
        if checkpoint['steps']:
            raise ValueError('Rehearsal parameters changed after steps committed')
        checkpoint = None
    if checkpoint is None:
        checkpoint = {'format': CHECKPOINT_FORMAT, 'work_root': str(work),
                      'operation': args.operation, 'installation_id': args.installation_id,
                      'parameters_digest': parameters, 'steps': []}
    else:
        _verify_committed_roots(work, checkpoint['steps'])
    if (work / RUN_RECORD_NAME).exists() and len(checkpoint['steps']) != len(STEPS):
        raise ValueError('Rehearsal run record survives an incomplete checkpoint')

    ctx = _Context(args, work, knowledge_python)
    ctx.committed = {step['name']: step for step in checkpoint['steps']}
    report = []
    for name, roots, body in STEPS:
        if name in ctx.committed:
            record = ctx.committed[name]
            report.append({'name': name, 'status': 'verified', 'idempotent': True,
                           'receipt_digest': _digest(_encoded(record['receipt']))})
            continue
        try:
            receipt, idempotent = body(ctx)
            outputs = {relative: _root_digest(work / relative) for relative in roots}
            record = {'name': name, 'idempotent': idempotent, 'receipt': receipt,
                      'outputs': outputs}
            checkpoint['steps'].append(record)
            _replace_private(work / CHECKPOINT_NAME, _encoded(checkpoint))
        except _StepFailure:
            raise
        except Exception:
            raise _StepFailure(name, 'step_rejected') from None
        ctx.committed[name] = record
        report.append({'name': name, 'status': 'executed', 'idempotent': idempotent,
                       'receipt_digest': _digest(_encoded(receipt))})

    terminal = _verify_terminal(ctx)
    record = _encoded(_run_record(checkpoint, terminal))
    run_record = work / RUN_RECORD_NAME
    if run_record.exists() or run_record.is_symlink():
        if _read_private(run_record) != record:
            raise ValueError('Rehearsal run record changed')
    else:
        _replace_private(run_record, record)
    return {'format': FORMAT, 'status': 'finalized', 'operation': args.operation,
            'installation_id': args.installation_id, 'parameters_digest': parameters,
            'steps': report, 'terminal_state': terminal['manifest_state'],
            'manifest_digest': terminal['manifest_digest'],
            'run_record_digest': _digest(record),
            'activation_allowed': False, 'installation_cutover_performed': True,
            'rollback_completed': False, 'production_activated': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', required=True,
                        help='Private rehearsal work root (created 0700 when missing)')
    parser.add_argument('--source-home', required=True, help='Live legacy PuddingClaw Home (read-only)')
    parser.add_argument('--knowledge-python',
                        default=os.environ.get('KNOWLEDGE_TEST_PYTHON'),
                        help='Explicit independent Knowledge interpreter '
                             '(default: KNOWLEDGE_TEST_PYTHON)')
    parser.add_argument('--installation-id', required=True)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--source-schema-revision', required=True)
    parser.add_argument('--operation', required=True,
                        help='Writer-barrier/cutover operation id for this rehearsal')
    parser.add_argument('--graft', action='append', default=[], metavar='SRC_DIR=DST_RELATIVE')
    parser.add_argument('--map', dest='mappings', action='append', default=[], metavar='SRC=DST')
    parser.add_argument('--repair-document', metavar='DOC_ID')
    parser.add_argument('--repair-reason', metavar='TEXT')
    parser.add_argument('--exclude', action='append', default=[], metavar='REL')
    parser.add_argument('--wiki-root-relative', default=DEFAULT_WIKI_ROOT, metavar='REL',
                        help='Wiki root inside the snapshot payload (default: llm-wiki)')
    parser.add_argument('--timeout-seconds', type=int, default=DEFAULT_TIMEOUT_SECONDS,
                        help='Per-step subprocess budget (default: 1800)')
    args = parser.parse_args(argv)
    step = None
    try:
        if not args.knowledge_python:
            raise ValueError('An explicit Knowledge interpreter is required')
        result = run_rehearsal(args)
    except _StepFailure as failure:
        step, code = failure.step, failure.error_code
        print(json.dumps({'format': FORMAT, 'status': 'error',
                          'error_code': 'rehearsal_driver_rejected', 'step': step,
                          'step_error_code': code,
                          'activation_allowed': False, 'installation_cutover_performed': False,
                          'rollback_completed': False, 'production_activated': False},
                         sort_keys=True))
        return 1
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error',
                          'error_code': 'rehearsal_driver_rejected', 'step': step,
                          'step_error_code': None,
                          'activation_allowed': False, 'installation_cutover_performed': False,
                          'rollback_completed': False, 'production_activated': False},
                         sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
