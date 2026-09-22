"""Checkpointed end-to-end driver for the spec 11.20 point-10 real-data rehearsal.

This module drives the real migration chains — source-home snapshot,
Knowledge migration request generation, offline migration orchestration,
manifest discover and prepare, product enrollment, writer suspension, cutover,
and then either finalize (path A) or the post-cutover window rollback
(window-rollback scenario) — as ordered REAL subprocesses against one private
work root, so the rehearsal exercises the shipped CLIs exactly as an operator
would.

Two scenarios share the same forward chain (snapshot through cutover):

* ``path-a`` (default) finishes with finalize and proves both products
  reopened on the new writers while the manifest reports FINALIZED.
* ``window-rollback`` stops after cutover, applies one deterministic
  Knowledge-era catalog edit, fences both products under a dedicated window
  operation, produces the frozen export / other-catalog disposition /
  document reverse bundle / absent-wiki attestation / rollback evidence as
  real Knowledge CLIs, and drives ``harness.rollback_orchestrator``
  window-rollback to a ROLLED_BACK manifest with both writers reassigned to
  puddingclaw.

Work-root layout (the work root itself is 0700 and everything in it is owned
by the caller and symlink-free; files are private 0600 single-linked, and
directories are sealed against group/other writes — chain-owned intermediate
directories are admitted with the modes their producers create):

    snapshot/               rehearsal_snapshot envelope (plan/manifest/payload)
    snapshot-receipt.json   producer receipt (lives outside the envelope)
    request/                Knowledge request + generation receipt
    staging/                migration orchestrator staging (harness/, knowledge/)
    knowledge-receipt.json  receipt extracted from the orchestrator result
    manifest/               installation manifest (DISCOVERED..FINALIZED/ROLLED_BACK)
    harness-home/           fresh Harness product Home, enrolled
    harness-authority/      Harness writer authority (journal, retired markers)
    knowledge-home/         Knowledge workspace bootstrapped from the staged
                            document-migration candidate, enrolled
    knowledge-authority/    Knowledge writer authority
    barrier/                writer suspension barrier checkpoint
    checkpoint/             cutover orchestrator checkpoint
    driver-checkpoint.json  this driver's private checkpoint
    run-record.json         machine-readable rehearsal evidence

The window-rollback scenario adds:

    lineage/                target-before/after Catalog copies and frozen
                            export from the actual four-event post-cutover
                            Knowledge workspace
    window-disposition/     other-catalog reverse disposition receipt
    document-reverse/       reverse bindings, attachment bindings and the
                            reversed legacy candidate (manifest + catalog)
    wiki-side/              version-1 wiki-shadow workspace (state, authority,
                            export, attestation) for the absent-wiki proof
    evidence/               assembled rollback evidence (own disjoint root)
    window-checkpoint/      window rollback orchestrator checkpoint
    rollback-activation/    legacy install, probe, pointer retirement and thaw

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
the request directory, the enrollment roots and the reverse-chain roots are
regenerated).

The run record (run-record.json) is canonical JSON listing each step, the
digest of its committed receipt and its output digests, plus the terminal
FINALIZED or ROLLED_BACK bindings.  It carries digests, labels and counts
only — no secrets and no row content.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys

from harness import cutover_orchestrator
from harness import installation_authority as authority
from harness import installation_manifest as manifests
from harness import rollback_activation
from harness import session_import
from harness.home_freeze import _sync_directory
from harness.installation_guard import (
    FREEZE_NAME as HARNESS_FREEZE_NAME,
    AdmissionUnavailable,
    InstallationGuard,
)
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
from harness.source_writer_fence import publish_source_fence

FORMAT = 'puddingharness-rehearsal-driver/v1'
RUN_FORMAT = 'puddingharness-rehearsal-driver-run/v1'
CHECKPOINT_FORMAT = 'puddingharness-rehearsal-driver-checkpoint/v1'
CHECKPOINT_NAME = 'driver-checkpoint.json'
RUN_RECORD_NAME = 'run-record.json'
REQUEST_FORMAT = 'puddingknowledge-claw-migration-request-receipt/v1'
KNOWLEDGE_WRITER_FORMAT = 'puddingknowledge-writer-authority/v1'
FROZEN_EXPORT_FORMAT = 'puddingknowledge-frozen-workspace-export/v1'
DISPOSITION_FORMAT = 'puddingknowledge-other-catalog-reverse/v1'
DOCUMENT_REVERSE_FORMAT = 'puddingknowledge-document-reverse/v5'
WIKI_ABSENT_FORMAT = 'puddingknowledge-wiki-reverse-absent/v1'
EVIDENCE_FORMAT = 'puddingknowledge-rollback-evidence/v1'
WINDOW_FORMAT = 'puddingharness-window-rollback-orchestrator/v1'
DEFAULT_WIKI_ROOT = 'llm-wiki'
DEFAULT_TIMEOUT_SECONDS = 1800
ENROLL_HARNESS = 'enroll-harness'
ENROLL_KNOWLEDGE = 'enroll-knowledge'
ENROLL_KNOWLEDGE_WIKI = 'enroll-knowledge-wiki'
_WINDOW_DELTA_TITLE = 'Knowledge-era rehearsal edit'
_MAX_STEP_OUTPUT = 8 * 1024 * 1024
_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,79}')
_OWNER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_CLAW_IDENTITY_FORMAT = 'puddingclaw-rehearsal-interpreter-identity/v1'

_CLAW_IDENTITY_PROBE = r'''
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
distribution = importlib.metadata.distribution('puddingclaw-backend')
spec = importlib.util.find_spec('cutover_domain_inventory')
if spec is None or spec.origin is None:
    raise SystemExit(2)
root = Path(distribution.locate_file('')).resolve()
module = Path(spec.origin).resolve()
if not module.is_file() or not module.is_relative_to(root):
    raise SystemExit(3)
relative = module.relative_to(root).as_posix()
if relative not in {str(item) for item in (distribution.files or ())}:
    raise SystemExit(4)
print(json.dumps({'format': 'puddingclaw-rehearsal-interpreter-identity/v1',
                  'package': 'puddingclaw-backend',
                  'version': distribution.version,
                  'module': relative,
                  'module_sha256': 'sha256:' + hashlib.sha256(module.read_bytes()).hexdigest()},
                 sort_keys=True))
'''

# The Knowledge workspace bootstrap is library-only upstream; this runs the
# documented persistent-workspace open against the staged document-migration
# candidate through the independent interpreter, so the rehearsal workspace
# holds exactly what the real migration produced (catalog, blobs, resources).
_KNOWLEDGE_WORKSPACE_BOOTSTRAP = '''
from pathlib import Path
import sys
from knowledge_platform.local.workspace import open_persistent_workspace
with open_persistent_workspace(Path(sys.argv[1]), document_migration=Path(sys.argv[2])):
    pass
'''

# The window scenario's Knowledge-era edit: one deterministic title change on
# the first migrated document asset in the real post-cutover workspace.
_KNOWLEDGE_WINDOW_DELTA = '''
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
catalog = Path(sys.argv[1]) / 'catalog.sqlite3'
before = hashlib.sha256(catalog.read_bytes()).hexdigest()
with sqlite3.connect(catalog) as connection:
    asset_id, previous = connection.execute(
        "SELECT id, title FROM knowledge_assets WHERE kind='document'"
        ' ORDER BY id LIMIT 1').fetchone()
    connection.execute('UPDATE knowledge_assets SET title=? WHERE id=?',
                       (sys.argv[2], asset_id))
after = hashlib.sha256(catalog.read_bytes()).hexdigest()
print(json.dumps({'asset_id': asset_id, 'previous_title': previous,
                  'title': sys.argv[2], 'catalog_sha256_before': before,
                  'catalog_sha256_after': after}))
'''

# The window scenario's absent-wiki stand-in.  A frozen export requires an
# exactly-two-event journal and the absent-wiki attestation requires zero
# knowledge domain rows and no wiki-evidence member, so no product bootstrap
# can produce it (every library bootstrap inserts identity rows, and the
# migrated-wiki bootstrap refuses the forward chain's empty wiki archive).
# The driver therefore hand-builds a version-1 workspace — an empty
# migrate_to_latest catalog that was never written to, a one-page wiki and a
# hand-written version-1 manifest — and loads it through the real persistent
# workspace open before enrolling and suspending it with the real CLIs.
_KNOWLEDGE_WIKI_SHADOW = '''
import json
import os
from pathlib import Path
import sys
from sqlalchemy import create_engine
from knowledge_platform.catalog.migrations import migrate_to_latest
from knowledge_platform.local.workspace import open_persistent_workspace
state = Path(sys.argv[1])
engine = create_engine('sqlite:///' + str(state / 'catalog.sqlite3'))
with engine.begin() as connection:
    migrate_to_latest(connection)
engine.dispose()
os.chmod(state / 'catalog.sqlite3', 0o600)
wiki = state / 'wiki'
wiki.mkdir(mode=0o700)
(wiki / 'guide.md').write_bytes(b'# Rehearsal wiki shadow\\n')
os.chmod(wiki / 'guide.md', 0o600)
manifest = {'version': 1, 'owner': 'puddingknowledge-local',
            'catalog': 'catalog.sqlite3', 'wiki_root': 'wiki', 'pages': 1,
            'collection_version': '1',
            'file_bindings': {'asset_wiki_rehearsal_shadow': 'guide.md'}}
path = state / 'workspace.json'
path.write_bytes((json.dumps(manifest, sort_keys=True) + '\\n').encode())
os.chmod(path, 0o600)
with open_persistent_workspace(state) as owned:
    print(json.dumps({'pages': owned['pages']}))
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


def _verify_committed_roots(work, steps, mutable_roots=()):
    """Verify prior outputs except roots owned by the interrupted next step.

    A checkpointed step may atomically mutate a root produced by its
    predecessor before it publishes the driver's next checkpoint.  Its own
    protocol must recover that root; requiring the predecessor's digest first
    would make the documented crash-resume path unreachable.
    """
    mutable = set(mutable_roots)
    for relative, expected in _fold_outputs(steps).items():
        if relative in mutable:
            continue
        if _root_digest(work / relative) != expected:
            raise ValueError('Committed rehearsal output diverged: ' + relative)


_CLEANABLE = re.compile(
    r'\.(?:driver-checkpoint\.json|run-record\.json|knowledge-receipt\.json)\.tmp-[0-9a-f]{16}\Z'
    r'|\.snapshot-receipt\.json\.rehearsal-part\Z')
_KNOWN_TOP_LEVEL = {
    'credential-baseline.json', 'source-freeze-receipt.json', 'snapshot', 'snapshot-receipt.json',
    'request', 'staging', 'knowledge-receipt.json', 'readiness',
    'manifest', 'harness-home', 'harness-authority', 'knowledge-home', 'knowledge-authority',
    'barrier', 'checkpoint', CHECKPOINT_NAME, RUN_RECORD_NAME,
}
_WINDOW_TOP_LEVEL = _KNOWN_TOP_LEVEL | {
    'lineage', 'window-disposition', 'document-reverse', 'wiki-side',
    'evidence', 'window-checkpoint', 'rollback-activation',
}


def _clean_transients(work):
    for entry in work.iterdir():
        if _CLEANABLE.fullmatch(entry.name):
            _read_private(entry)
            entry.unlink()
            _sync_directory(work)


def _check_top_level(work, allowed=None):
    for entry in work.iterdir():
        if entry.is_symlink() or entry.name not in (allowed or _KNOWN_TOP_LEVEL):
            raise ValueError('Unknown rehearsal work-root entry')


def _load_checkpoint(work, table=None):
    table = STEPS if table is None else table
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
    if len(steps) > len(table):
        raise ValueError('Rehearsal driver checkpoint steps are invalid')
    for index, step in enumerate(steps):
        name, roots, _body = table[index]
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
    def __init__(self, args, work, knowledge_python, claw_python, window_operation=None):
        self.work = work
        self.knowledge_python = str(knowledge_python)
        self.claw_python = str(claw_python)
        self.source_home = str(_path(args.source_home))
        self.installation_id = args.installation_id
        self.source_revision = args.source_revision
        self.source_schema_revision = args.source_schema_revision
        self.operation = args.operation
        self.credential_owner = args.credential_owner
        self.scenario = args.scenario
        self.window_operation = window_operation
        self.grafts = list(args.graft)
        self.mappings = list(args.mappings)
        self.virtual_roots = list(args.virtual_roots)
        self.repair_document = args.repair_document
        self.repair_reason = args.repair_reason
        self.recommit_document = args.recommit_document
        self.recommit_reason = args.recommit_reason
        self.exclusions = list(args.exclude)
        self.wiki_root = args.wiki_root_relative
        self.timeout = args.timeout_seconds
        self.committed = {}

    def receipt(self, name):
        return self.committed[name]['receipt']

    def path(self, name):
        return self.work / name


def _step_source_freeze(ctx):
    baseline = rollback_activation.capture_credential_baseline(
        ctx.source_home, ctx.path('credential-baseline.json'), operation_id=ctx.operation)
    result = publish_source_fence(ctx.source_home, ctx.operation)
    if (result.get('format') != 'puddingclaw-source-freeze/v1'
            or result.get('state') != 'source_frozen'
            or result.get('operation_id') != ctx.operation
            or result.get('legacy_writer_fenced') is not True
            or not _is_hex64(result.get('source_home_identity'))
            or not _is_hex64(result.get('admission_capability_sha256'))
            or not _is_hex64(result.get('source_freeze_receipt_sha256'))):
        raise _StepFailure('source-freeze', 'step_receipt_invalid')
    source_receipt = {
        'format': result['format'],
        'operation_id': result['operation_id'],
        'source_home_identity': result['source_home_identity'],
        'admission_capability_sha256': result['admission_capability_sha256'],
        'source_freeze_receipt_sha256': result['source_freeze_receipt_sha256'],
        'legacy_writer_fenced': True,
    }
    path = ctx.path('source-freeze-receipt.json')
    # installation_manifest owns this consumer contract and defines canonical
    # JSON without a trailing newline.
    raw = manifests.encoded(source_receipt)
    if path.exists() or path.is_symlink():
        if _read_private(path) != raw:
            raise ValueError('Source freeze receipt changed')
        idempotent = True
    else:
        _replace_private(path, raw)
        idempotent = False
    return ({**source_receipt,
             'credential_baseline_sha256': baseline['credential_baseline_sha256']},
            idempotent)


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
    if ctx.recommit_document is not None:
        command += ['--recommit-document', ctx.recommit_document, '--recommit-reason', ctx.recommit_reason]
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
    for virtual_root in ctx.virtual_roots:
        command += ['--virtual-root', virtual_root]
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
    # Preserve the exact canonical bytes committed by the orchestrator.  Every
    # downstream readiness receipt binds this byte digest, so adding a newline
    # here would create a second identity for the same semantic receipt.
    _replace_private(ctx.path('knowledge-receipt.json'), canonical)
    return ({'plan_digest': result['plan_digest'], 'harness_plan_digest': result['harness_plan_digest'],
             'knowledge_receipt_digest': receipt_digest}, False)


def _step_readiness(ctx):
    root = ctx.path('readiness')
    if root.is_symlink():
        raise ValueError('Rehearsal readiness path is a symlink')
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    _sync_directory(ctx.work)

    identity = _run_cli([ctx.claw_python, '-I', '-c', _CLAW_IDENTITY_PROBE],
                        ctx.timeout, 'readiness')
    if (set(identity) != {'format', 'package', 'version', 'module', 'module_sha256'}
            or identity.get('format') != _CLAW_IDENTITY_FORMAT
            or identity.get('package') != 'puddingclaw-backend'
            or not _TOKEN.fullmatch(str(identity.get('version', '')))
            or identity.get('module') != 'cutover_domain_inventory.py'
            or not _is_digest(identity.get('module_sha256'))):
        raise _StepFailure('readiness', 'step_receipt_invalid')

    domains = ('session_harness', 'knowledge_catalog', 'connector_jobs')
    source_outputs = {
        'session_harness': root / 'source-session-inventory.json',
        'knowledge_catalog': root / 'source-knowledge-inventory.json',
        'connector_jobs': root / 'source-connector-inventory.json',
    }
    source_digests = {}
    for domain in domains:
        result = _run_cli([
            ctx.claw_python, '-I', '-m', 'cutover_domain_inventory',
            '--source-home-snapshot', str(ctx.path('snapshot')),
            '--domain', domain, '--output', str(source_outputs[domain]),
        ], ctx.timeout, 'readiness')
        if (result.get('format') != 'puddingclaw-cutover-domain-inventory/v1'
                or result.get('domain') != domain
                or not _is_digest(result.get('inventory_sha256'))):
            raise _StepFailure('readiness', 'step_receipt_invalid')
        source_digests[domain] = result['inventory_sha256']

    target_outputs = {
        'session_harness': root / 'target-session-inventory.json',
        'knowledge_catalog': root / 'target-knowledge-inventory.json',
        'connector_jobs': root / 'target-connector-inventory.json',
    }
    session = _run_cli([
        sys.executable, '-m', 'harness.cutover_domain_inventory',
        '--staging', str(ctx.path('staging') / 'harness'),
        '--output', str(target_outputs['session_harness']),
    ], ctx.timeout, 'readiness')
    if (session.get('format') != 'puddingharness-cutover-domain-inventory/v1'
            or not _is_digest(session.get('inventory_sha256'))):
        raise _StepFailure('readiness', 'step_receipt_invalid')
    target_digests = {'session_harness': session['inventory_sha256']}
    for domain in ('knowledge_catalog', 'connector_jobs'):
        result = _run_cli([
            ctx.knowledge_python, '-m', 'knowledge_platform.distribution.cutover_domain_inventory',
            '--migration-receipt', str(ctx.path('knowledge-receipt.json')),
            '--candidate', str(ctx.path('staging') / 'knowledge' / 'candidate'),
            '--domain', domain, '--output', str(target_outputs[domain]),
        ], ctx.timeout, 'readiness')
        if (result.get('format') != 'puddingknowledge-cutover-domain-inventory/v1'
                or result.get('domain') != domain
                or not _is_digest(result.get('inventory_sha256'))):
            raise _StepFailure('readiness', 'step_receipt_invalid')
        target_digests[domain] = result['inventory_sha256']

    coverage_path = root / 'domain-coverage.json'
    coverage = _run_cli([
        ctx.knowledge_python, '-m', 'knowledge_platform.distribution.cutover_domain_coverage',
        '--migration-receipt', str(ctx.path('knowledge-receipt.json')),
        '--candidate', str(ctx.path('staging') / 'knowledge' / 'candidate'),
        '--source-session-inventory', str(source_outputs['session_harness']),
        '--target-session-inventory', str(target_outputs['session_harness']),
        '--source-knowledge-inventory', str(source_outputs['knowledge_catalog']),
        '--target-knowledge-inventory', str(target_outputs['knowledge_catalog']),
        '--source-connector-inventory', str(source_outputs['connector_jobs']),
        '--target-connector-inventory', str(target_outputs['connector_jobs']),
        '--output', str(coverage_path),
    ], ctx.timeout, 'readiness')
    if (coverage.get('format') != 'puddingknowledge-cutover-domain-coverage/v1'
            or coverage.get('domain_count') != 3):
        raise _StepFailure('readiness', 'step_receipt_invalid')

    index_path = root / 'index-readiness.json'
    indexes = _run_cli([
        ctx.knowledge_python, '-m', 'knowledge_platform.distribution.cutover_index_readiness',
        '--migration-receipt', str(ctx.path('knowledge-receipt.json')),
        '--candidate', str(ctx.path('staging') / 'knowledge' / 'candidate'),
        '--domain-coverage', str(coverage_path), '--output', str(index_path),
    ], ctx.timeout, 'readiness')
    if (indexes.get('format') != 'puddingknowledge-cutover-index-readiness/v1'
            or indexes.get('state') not in {'ready', 'explicit_absent'}
            or type(indexes.get('ready_count')) is not int or indexes['ready_count'] < 0):
        raise _StepFailure('readiness', 'step_receipt_invalid')

    credential_path = root / 'credential-rebind.json'
    credentials = _run_cli([
        ctx.knowledge_python, '-m', 'knowledge_platform.distribution.credential_rebind',
        '--source-home', ctx.source_home, '--owner', ctx.credential_owner,
        '--target-root', str(root / 'credential-target'),
        '--receipt', str(credential_path),
    ], ctx.timeout, 'readiness')
    counts = credentials.get('counts')
    if (credentials.get('format') != 'puddingknowledge-credential-rebind/v1'
            or credentials.get('state') != 'completed'
            or credentials.get('credential_continuity_verified') is not True
            or not isinstance(counts, dict) or counts.get('failed') != 0):
        raise _StepFailure('readiness', 'step_receipt_invalid')

    readiness_path = root / 'cutover-readiness.json'
    complete = _run_cli([
        ctx.knowledge_python, '-m', 'knowledge_platform.distribution.cutover_readiness',
        '--migration-receipt', str(ctx.path('knowledge-receipt.json')),
        '--candidate', str(ctx.path('staging') / 'knowledge' / 'candidate'),
        '--credential-rebind-receipt', str(credential_path),
        '--domain-coverage', str(coverage_path), '--index-readiness', str(index_path),
        '--output', str(readiness_path),
    ], ctx.timeout, 'readiness')
    if (complete.get('format') != 'puddingknowledge-cutover-readiness/v1'
            or complete.get('state') != 'verified_inactive_complete'
            or complete.get('covered_domains') != list(domains)
            or complete.get('pending_domains') != []
            or complete.get('cutover_readiness_verified') is not True
            or complete.get('complete_migration_evidence') is not True
            or complete.get('writer_fence_verified') is not False
            or complete.get('activation_allowed') is not False
            or _json(_read_private(readiness_path)) != complete):
        raise _StepFailure('readiness', 'step_receipt_invalid')
    return ({
        'claw_interpreter_identity_sha256': _digest(_encoded(identity)),
        'source_inventory_sha256': source_digests,
        'target_inventory_sha256': target_digests,
        'domain_coverage_sha256': _digest(_read_private(coverage_path)),
        'index_readiness_sha256': _digest(_read_private(index_path)),
        'credential_rebind_sha256': _digest(_read_private(credential_path)),
        'cutover_readiness_sha256': _digest(_read_private(readiness_path)),
        'credential_owner': ctx.credential_owner,
    }, False)


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
               '--knowledge-readiness', str(ctx.path('readiness') / 'cutover-readiness.json'),
               '--source-freeze-receipt', str(ctx.path('source-freeze-receipt.json')),
               '--output', str(ctx.path('manifest') / 'manifest.json')]
    result = _run_cli(command, ctx.timeout, 'prepare')
    if (result.get('format') != manifests.FORMAT or result.get('state') != 'PREPARED'
            or type(result.get('idempotent')) is not bool
            or not _is_digest(result.get('manifest_digest'))):
        raise _StepFailure('prepare', 'step_receipt_invalid')
    return ({'state': result['state'], 'manifest_digest': result['manifest_digest']},
            result['idempotent'])


def _writer_journal(result, expected_format, operation_id, events, step, state):
    """Validate a writer-authority CLI receipt and return its journal."""
    journal = result.get('journal')
    actual = journal.get('events') if isinstance(journal, dict) else None
    head = actual[-1] if isinstance(actual, list) and actual else None
    if (result.get('format') != expected_format or result.get('status') != 'ok'
            or not isinstance(actual, list) or len(actual) != events
            or not isinstance(head, dict) or head.get('state') != state
            or head.get('operation_id') != operation_id
            or head.get('revision') != events - 1
            or (events >= 2 and head.get('previous') != actual[-2].get('sha256'))):
        raise _StepFailure(step, 'step_receipt_invalid')
    return journal


def _step_enroll(ctx):
    for name in ('harness-home', 'harness-authority', 'knowledge-home',
                 'knowledge-authority'):
        path = ctx.path(name)
        if path.is_symlink():
            raise ValueError('Rehearsal enrollment path is a symlink')
        if path.exists():
            shutil.rmtree(path)
    ctx.path('harness-home').mkdir(mode=0o700)
    _sync_directory(ctx.work)
    harness = _run_cli([sys.executable, '-m', 'harness.installation_authority', 'enroll',
                        '--home', str(ctx.path('harness-home')),
                        '--authority', str(ctx.path('harness-authority')),
                        '--operation-id', ENROLL_HARNESS], ctx.timeout, 'enroll')
    _writer_journal(harness, authority.FORMAT, ENROLL_HARNESS, 1, 'enroll', 'existing_writer')
    harness_journal = harness['journal']
    # The cutover target is the real migrated document candidate, bootstrapped
    # through the documented persistent-workspace open.
    _run_cli([ctx.knowledge_python, '-c', _KNOWLEDGE_WORKSPACE_BOOTSTRAP,
              str(ctx.path('knowledge-home')),
              str(ctx.path('staging') / 'knowledge' / 'candidate')],
             ctx.timeout, 'enroll', expect_json=False)
    knowledge = _run_cli([ctx.knowledge_python, '-m', 'knowledge_platform.local.writer_authority',
                          'enroll', '--state-dir', str(ctx.path('knowledge-home')),
                          '--authority', str(ctx.path('knowledge-authority')),
                          '--operation-id', ENROLL_KNOWLEDGE], ctx.timeout, 'enroll')
    _writer_journal(knowledge, KNOWLEDGE_WRITER_FORMAT, ENROLL_KNOWLEDGE, 1,
                    'enroll', 'existing_writer')
    knowledge_journal = knowledge['journal']
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
                       '--source-home', ctx.source_home,
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
            or not _is_digest(result.get('source_freeze_receipt_sha256'))
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
             'source_freeze_receipt_sha256': result['source_freeze_receipt_sha256'],
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


def _step_window_delta(ctx):
    root = ctx.path('lineage')
    if root.is_symlink():
        raise ValueError('Rehearsal lineage path is a symlink')
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    _sync_directory(ctx.work)
    before = root / 'target-before.sqlite3'
    shutil.copyfile(ctx.path('knowledge-home') / 'catalog.sqlite3', before)
    os.chmod(before, 0o600)
    result = _run_cli([ctx.knowledge_python, '-c', _KNOWLEDGE_WINDOW_DELTA,
                       str(ctx.path('knowledge-home')), _WINDOW_DELTA_TITLE],
                      ctx.timeout, 'window-delta')
    if (set(result) != {'asset_id', 'previous_title', 'title',
                        'catalog_sha256_before', 'catalog_sha256_after'}
            or not isinstance(result['asset_id'], str) or not result['asset_id']
            or not isinstance(result['previous_title'], str)
            or result['title'] != _WINDOW_DELTA_TITLE
            or not _is_hex64(result['catalog_sha256_before'])
            or not _is_hex64(result['catalog_sha256_after'])
            or result['catalog_sha256_before'] == result['catalog_sha256_after']):
        raise _StepFailure('window-delta', 'step_receipt_invalid')
    after = root / 'target-after.sqlite3'
    shutil.copyfile(ctx.path('knowledge-home') / 'catalog.sqlite3', after)
    os.chmod(after, 0o600)
    if (_file_digest(before)[0] != result['catalog_sha256_before']
            or _file_digest(after)[0] != result['catalog_sha256_after']):
        raise ValueError('Real post-cutover Catalog delta diverged')
    return (result, False)


def _step_window_suspend(ctx):
    # Fence both products under the window operation with the real suspend
    # CLIs; the window-rollback orchestrator consumes the rev3 journals as an
    # exact retry and never appends a duplicate suspension.
    harness = _run_cli([sys.executable, '-m', 'harness.installation_authority', 'suspend',
                        '--home', str(ctx.path('harness-home')),
                        '--operation-id', ctx.window_operation],
                       ctx.timeout, 'window-suspend')
    harness_journal = _writer_journal(harness, authority.FORMAT, ctx.window_operation,
                                      4, 'window-suspend', 'suspended')
    knowledge = _run_cli([ctx.knowledge_python, '-m', 'knowledge_platform.local.writer_authority',
                          'suspend', '--state-dir', str(ctx.path('knowledge-home')),
                          '--operation-id', ctx.window_operation],
                         ctx.timeout, 'window-suspend')
    knowledge_journal = _writer_journal(knowledge, KNOWLEDGE_WRITER_FORMAT,
                                        ctx.window_operation, 4, 'window-suspend', 'suspended')
    for home, marker, journal in (
            (ctx.path('harness-home'), HARNESS_FREEZE_NAME, harness_journal),
            (ctx.path('knowledge-home'), KNOWLEDGE_FREEZE_NAME, knowledge_journal)):
        digest, _size = _file_digest(home / marker)
        if digest != journal['events'][3]['freeze_receipt_sha256']:
            raise _StepFailure('window-suspend', 'step_receipt_invalid')
    return ({'harness_journal_digest': _digest(_encoded(harness_journal)),
             'knowledge_journal_digest': _digest(_encoded(knowledge_journal))}, False)


def _validate_frozen_export(result, step):
    if (result.get('format') != FROZEN_EXPORT_FORMAT
            or result.get('state') != 'verified_frozen_export'
            or not _is_hex64(result.get('plan_sha256'))
            or type(result.get('file_count')) is not int or result['file_count'] < 0
            or type(result.get('idempotent')) is not bool
            or result.get('workspace_writers_suspended') is not True
            or result.get('activation_allowed') is not False
            or result.get('rollback_completed') is not False):
        raise _StepFailure(step, 'step_receipt_invalid')


def _step_window_export(ctx):
    # Export the actual post-cutover workspace.  Its journal is the real four
    # event chain: enrollment, cutover suspension/assignment, then the window
    # suspension.  frozen_export validates that exact lineage.
    root = ctx.path('lineage')
    if root.is_symlink():
        raise ValueError('Rehearsal lineage path is a symlink')
    if not root.is_dir():
        raise ValueError('Real Catalog lineage evidence is missing')
    delta = ctx.receipt('window-delta')
    if (_file_digest(root / 'target-before.sqlite3')[0] != delta['catalog_sha256_before']
            or _file_digest(root / 'target-after.sqlite3')[0] != delta['catalog_sha256_after']
            or _file_digest(ctx.path('knowledge-home') / 'catalog.sqlite3')[0]
            != delta['catalog_sha256_after']):
        raise ValueError('Post-cutover workspace catalog diverged')
    export = _run_cli([ctx.knowledge_python, '-m', 'knowledge_platform.local.frozen_export',
                       '--state-dir', str(ctx.path('knowledge-home')),
                       '--output', str(root / 'export'),
                       '--operation-id', ctx.window_operation],
                      ctx.timeout, 'window-export')
    _validate_frozen_export(export, 'window-export')
    return ({'state': export['state'], 'plan_sha256': export['plan_sha256'],
             'file_count': export['file_count']}, False)


def _step_window_disposition(ctx):
    root = ctx.path('window-disposition')
    if root.is_symlink():
        raise ValueError('Rehearsal disposition path is a symlink')
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    _sync_directory(ctx.work)
    result = _run_cli([ctx.knowledge_python,
                       '-m', 'knowledge_platform.distribution.other_catalog_reverse',
                       '--target-before', str(ctx.path('lineage') / 'target-before.sqlite3'),
                       '--target-after', str(ctx.path('lineage') / 'target-after.sqlite3'),
                       '--frozen-export', str(ctx.path('lineage') / 'export'),
                       '--output', str(root / 'disposition.json')],
                      ctx.timeout, 'window-disposition')
    digest, _size = _file_digest(root / 'disposition.json')
    if (result.get('format') != DISPOSITION_FORMAT
            or result.get('state') != 'verified_other_catalog_disposition'
            or result.get('schema_versions_identical') is not True
            or type(result.get('idempotent')) is not bool
            or result.get('receipt_sha256') != digest):
        raise _StepFailure('window-disposition', 'step_receipt_invalid')
    return ({'state': result['state'], 'receipt_sha256': digest}, False)


def _step_window_document_reverse(ctx):
    root = ctx.path('document-reverse')
    if root.is_symlink():
        raise ValueError('Rehearsal document reverse path is a symlink')
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    _sync_directory(ctx.work)
    home = ctx.path('knowledge-home')
    after = ctx.path('lineage') / 'target-after.sqlite3'
    with sqlite3.connect(f'file:{after}?mode=ro', uri=True) as catalog:
        rows = catalog.execute(
            "SELECT id, content_digest, metadata_json FROM knowledge_assets"
            " WHERE kind='document' ORDER BY id").fetchall()
    if not rows:
        raise ValueError('Migrated workspace carries no documents')
    bindings = {}
    for asset_id, content_digest, _metadata in rows:
        if not isinstance(content_digest, str) or not content_digest.startswith('sha256:'):
            raise ValueError('Migrated document digest is invalid')
        relative = 'blobs/' + content_digest.removeprefix('sha256:')
        if _file_digest(home / relative)[0] != content_digest.removeprefix('sha256:'):
            raise ValueError('Migrated document body digest mismatch')
        bindings[asset_id] = relative
    grafts = []
    for graft in ctx.grafts:
        source, separator, destination = graft.partition('=')
        if separator:
            grafts.append((str(_path(source)), destination))
    attachments = {}
    for _asset_id, _content_digest, metadata_raw in rows:
        metadata = json.loads(metadata_raw)
        references = [asset['path'] for asset in metadata.get('assets') or []]
        if metadata.get('original_path'):
            references.append(metadata['original_path'])
        multimodal = metadata.get('multimodal') or {}
        if multimodal.get('image_assets_dir'):
            references.append(multimodal['image_assets_dir'])
        for reference in references:
            mapped = None
            for source, destination in grafts:
                if reference == source or reference.startswith(source + '/'):
                    suffix = reference[len(source):].lstrip('/')
                    mapped = 'resources/' + destination + ('/' + suffix if suffix else '')
                    break
            if mapped is None:
                raise ValueError('Attachment reference escapes the grafted corpus')
            if not (home / mapped).exists() or (home / mapped).is_symlink():
                raise ValueError('Attachment reference target is missing')
            attachments[reference] = mapped
    _replace_private(root / 'bindings.json', _encoded(bindings))
    command = [ctx.knowledge_python, '-m', 'knowledge_platform.distribution.document_reverse',
               '--source-snapshot', str(ctx.path('snapshot') / 'payload/db/catalog.sqlite3'),
               '--target-before', str(ctx.path('lineage') / 'target-before.sqlite3'),
               '--target-after', str(after),
               '--body-root', str(home),
               '--bindings', str(root / 'bindings.json'),
               '--output', str(root / 'reverse'),
               '--source-revision', ctx.source_revision]
    if attachments:
        _replace_private(root / 'attachments.json', _encoded(attachments))
        command += ['--attachment-bindings', str(root / 'attachments.json')]
    # Bodies are content-addressed blobs in the migrated home; their relative
    # in-body references were written against the original payload layout.
    # Alias each blob to that layout position (under resources/, where the
    # forward chain stored dependency bytes) so the reverse walk resolves them.
    request = _json(_read_private(ctx.path('request') / 'request.json'))
    payload = ctx.path('snapshot') / 'payload'
    bound_blobs = set(bindings.values())
    aliases = {}
    for relative in (request.get('bindings') or {}).values():
        if not isinstance(relative, str):
            raise TypeError('Request binding is invalid')
        _relative(relative)
        blob = 'blobs/' + _file_digest(payload / relative)[0]
        if blob in bound_blobs:
            aliases[blob] = 'resources/' + relative
    if aliases:
        _replace_private(root / 'resolution-aliases.json', _encoded(aliases))
        command += ['--resolution-aliases', str(root / 'resolution-aliases.json')]
    for virtual_root in ctx.virtual_roots:
        prefix, separator, destination = virtual_root.partition('=')
        if not separator:
            raise ValueError('Virtual roots must be VIRTUAL_PREFIX=DST_RELATIVE')
        # Reverse collection runs against the migrated home layout, where the
        # forward chain stored dependency bytes under resources/<payload path>.
        command += ['--virtual-root', prefix + '=resources/' + destination]
    result = _run_cli(command, ctx.timeout, 'window-document-reverse')
    if (result.get('format') != DOCUMENT_REVERSE_FORMAT
            or result.get('state') != 'verified_inactive_documents'
            or result.get('document_count') != len(bindings)
            or type(result.get('dependency_file_count')) is not int
            or result['dependency_file_count'] < 0
            or not _is_hex64(result.get('catalog_sha256'))
            or result.get('document_bodies_materialized') is not True
            or result.get('activation_allowed') is not False
            or result.get('rollback_completed') is not False):
        raise _StepFailure('window-document-reverse', 'step_receipt_invalid')
    return ({'state': result['state'], 'document_count': result['document_count'],
             'dependency_file_count': result['dependency_file_count'],
             'catalog_sha256': result['catalog_sha256']}, False)


def _step_window_wiki_absent(ctx):
    root = ctx.path('wiki-side')
    if root.is_symlink():
        raise ValueError('Rehearsal wiki side path is a symlink')
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    state = root / 'state'
    state.mkdir(mode=0o700)
    _sync_directory(ctx.work)
    loaded = _run_cli([ctx.knowledge_python, '-c', _KNOWLEDGE_WIKI_SHADOW, str(state)],
                      ctx.timeout, 'window-wiki-absent')
    if loaded != {'pages': 1}:
        raise _StepFailure('window-wiki-absent', 'step_receipt_invalid')
    enroll = _run_cli([ctx.knowledge_python, '-m', 'knowledge_platform.local.writer_authority',
                       'enroll', '--state-dir', str(state), '--authority', str(root / 'authority'),
                       '--operation-id', ENROLL_KNOWLEDGE_WIKI], ctx.timeout, 'window-wiki-absent')
    _writer_journal(enroll, KNOWLEDGE_WRITER_FORMAT, ENROLL_KNOWLEDGE_WIKI, 1,
                    'window-wiki-absent', 'existing_writer')
    suspended = _run_cli([ctx.knowledge_python, '-m', 'knowledge_platform.local.writer_authority',
                          'suspend', '--state-dir', str(state),
                          '--operation-id', ctx.window_operation],
                         ctx.timeout, 'window-wiki-absent')
    _writer_journal(suspended, KNOWLEDGE_WRITER_FORMAT, ctx.window_operation, 2,
                    'window-wiki-absent', 'suspended')
    export = _run_cli([ctx.knowledge_python, '-m', 'knowledge_platform.local.frozen_export',
                       '--state-dir', str(state), '--output', str(root / 'export'),
                       '--operation-id', ctx.window_operation],
                      ctx.timeout, 'window-wiki-absent')
    _validate_frozen_export(export, 'window-wiki-absent')
    attestation = _run_cli([ctx.knowledge_python,
                            '-m', 'knowledge_platform.distribution.wiki_reverse_absent',
                            '--current-workspace', str(root / 'export'),
                            '--source-revision', ctx.source_revision,
                            '--output', str(root / 'attestation')],
                           ctx.timeout, 'window-wiki-absent')
    if (attestation.get('format') != WIKI_ABSENT_FORMAT
            or attestation.get('state') != 'verified_absent_wiki'
            or attestation.get('wiki_domain_absent') is not True
            or type(attestation.get('tables_attested_absent')) is not int
            or attestation['tables_attested_absent'] <= 0
            or type(attestation.get('idempotent')) is not bool
            or attestation.get('activation_allowed') is not False):
        raise _StepFailure('window-wiki-absent', 'step_receipt_invalid')
    return ({'state': attestation['state'],
             'tables_attested_absent': attestation['tables_attested_absent']}, False)


def _step_window_evidence(ctx):
    root = ctx.path('evidence')
    if root.is_symlink():
        raise ValueError('Rehearsal evidence path is a symlink')
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700)
    _sync_directory(ctx.work)
    result = _run_cli([ctx.knowledge_python,
                       '-m', 'knowledge_platform.distribution.rollback_evidence',
                       '--frozen-export-manifest',
                       str(ctx.path('lineage') / 'export/manifest.json'),
                       '--other-catalog-disposition',
                       str(ctx.path('window-disposition') / 'disposition.json'),
                       '--document-reverse-manifest',
                       str(ctx.path('document-reverse') / 'reverse/manifest.json'),
                       '--wiki-reverse-manifest',
                       str(ctx.path('wiki-side') / 'attestation/manifest.json'),
                       '--output', str(root / 'evidence.json')],
                      ctx.timeout, 'window-evidence')
    digest, _size = _file_digest(root / 'evidence.json')
    if (result.get('format') != EVIDENCE_FORMAT
            or result.get('state') != 'verified_rollback_evidence'
            or result.get('operation_id') != ctx.window_operation
            or result.get('source_revision') != ctx.source_revision
            or type(result.get('idempotent')) is not bool
            or result.get('evidence_sha256') != digest):
        raise _StepFailure('window-evidence', 'step_receipt_invalid')
    return ({'state': result['state'], 'operation_id': result['operation_id'],
             'evidence_sha256': digest}, False)


def _step_window_rollback(ctx):
    result = _run_cli([sys.executable, '-m', 'harness.rollback_orchestrator', 'window-rollback',
                       '--harness-home', str(ctx.path('harness-home')),
                       '--knowledge-state', str(ctx.path('knowledge-home')),
                       '--knowledge-python', ctx.knowledge_python,
                       '--checkpoint-dir', str(ctx.path('window-checkpoint')),
                       '--manifest', str(ctx.path('manifest') / 'manifest.json'),
                       '--rollback-evidence', str(ctx.path('evidence') / 'evidence.json'),
                       '--operation-id', ctx.window_operation,
                       '--timeout-seconds', str(ctx.timeout)], ctx.timeout, 'window-rollback')
    journals = result.get('journals')
    heads = {}
    if isinstance(journals, dict):
        for side in ('harness', 'knowledge'):
            journal = journals.get(side)
            events = journal.get('events') if isinstance(journal, dict) else None
            if isinstance(events, list) and len(events) == 5:
                heads[side] = events[4]
    evidence_digest, _size = _file_digest(ctx.path('evidence') / 'evidence.json')
    rolled_back = result.get('rolled_back_manifest_sha256')
    if (result.get('format') != WINDOW_FORMAT
            or result.get('state') != 'both_reassigned'
            or result.get('rollback_completed') is not False
            or result.get('activation_allowed') is not False
            or result.get('installation_cutover_performed') is not False
            or result.get('production_activated') is not False
            or result.get('cutover_manifest_sha256') != ctx.receipt('cutover')['cutover_manifest_sha256']
            or result.get('rollback_evidence_sha256') != evidence_digest
            or not _is_hex64(rolled_back)
            or set(heads) != {'harness', 'knowledge'}):
        raise _StepFailure('window-rollback', 'step_receipt_invalid')
    for side, head in heads.items():
        suspended = journals[side]['events'][3]
        if (suspended.get('state') != 'suspended'
                or suspended.get('operation_id') != ctx.window_operation
                or head.get('state') != 'assigned'
                or head.get('revision') != 4
                or head.get('operation_id') != ctx.window_operation
                or head.get('previous') != suspended.get('sha256')
                or head.get('freeze_receipt_sha256') != suspended.get('freeze_receipt_sha256')
                or head.get('migration_manifest_sha256') != rolled_back
                or head.get('active_installation_revision') != 'sha256:' + rolled_back
                or head.get('rollback_evidence_sha256') != evidence_digest
                or not _is_hex64(head.get('sha256'))):
            raise _StepFailure('window-rollback', 'step_receipt_invalid')
    if (heads['harness'].get('writer') != 'puddingclaw'
            or heads['knowledge'].get('writers') != {'knowledge_catalog': 'puddingclaw',
                                                     'connector_jobs': 'puddingclaw'}):
        raise _StepFailure('window-rollback', 'step_receipt_invalid')
    return ({'state': result['state'], 'rolled_back_manifest_sha256': rolled_back,
             'rollback_evidence_sha256': evidence_digest,
             'harness_window_event_sha256': heads['harness']['sha256'],
             'knowledge_window_event_sha256': heads['knowledge']['sha256']}, False)


def _step_window_activate(ctx):
    stage = ctx.path('rollback-activation')
    if stage.is_symlink():
        raise ValueError('Rollback activation checkpoint is a symlink')
    if not stage.exists():
        stage.mkdir(mode=0o700)
        _sync_directory(ctx.work)
    result = _run_cli([
        sys.executable, '-m', 'harness.rollback_activation', 'activate',
        '--source-home', ctx.source_home,
        '--harness-home', str(ctx.path('harness-home')),
        '--knowledge-home', str(ctx.path('knowledge-home')),
        '--candidate', str(ctx.path('document-reverse') / 'reverse'),
        '--knowledge-python', ctx.knowledge_python,
        '--checkpoint-dir', str(stage),
        '--rollback-checkpoint', str(ctx.path('window-checkpoint') / 'checkpoint.json'),
        '--rollback-evidence', str(ctx.path('evidence') / 'evidence.json'),
        '--credential-baseline', str(ctx.path('credential-baseline.json')),
        '--source-operation-id', ctx.operation,
        '--rollback-operation-id', ctx.window_operation,
    ], ctx.timeout, 'window-activate')
    receipt_path = stage / rollback_activation.ACTIVATION_NAME
    digest, _size = _file_digest(receipt_path)
    if (result.get('format') != rollback_activation.ACTIVATION_FORMAT
            or result.get('state') != 'rollback_completed'
            or result.get('source_operation_id') != ctx.operation
            or result.get('rollback_operation_id') != ctx.window_operation
            or result.get('legacy_writer_thawed') is not True
            or result.get('installation_path_rebound') is not True
            or result.get('indexes_rebuilt') is not True
            or result.get('credential_continuity_verified') is not True
            or result.get('activation_allowed') is not True
            or result.get('rollback_completed') is not True
            or result.get('production_activated') is not False
            or result.get('activation_receipt_sha256') != digest):
        raise _StepFailure('window-activate', 'step_receipt_invalid')
    return ({'state': result['state'], 'activation_receipt_sha256': digest,
             'catalog_sha256': result['catalog_sha256'],
             'active_pointer_retirement_sha256':
                 result['active_pointer_retirement_sha256'],
             'legacy_writer_thawed': True, 'rollback_completed': True},
            result.get('idempotent') is True)


# Ordered path-A steps: (name, owned work-root-relative output roots, body).
# 'reset' steps (snapshot, request, enroll) own their outputs outright and
# rebuild them inside the body whenever the step is uncommitted; every other
# step relies on the step CLI's own crash recovery.  The window-rollback
# scenario shares the forward chain through cutover and appends its own
# steps; each appended step writes only roots no earlier step pins.
STEPS = (
    ('source-freeze', ('credential-baseline.json', 'source-freeze-receipt.json'),
     _step_source_freeze),
    ('snapshot', ('snapshot', 'snapshot-receipt.json'), _step_snapshot),
    ('request', ('request',), _step_request),
    ('orchestrate', ('staging', 'knowledge-receipt.json'), _step_orchestrate),
    ('readiness', ('readiness',), _step_readiness),
    ('discover', ('manifest',), _step_discover),
    ('prepare', ('manifest',), _step_prepare),
    ('enroll', ('harness-home', 'harness-authority', 'knowledge-home',
                'knowledge-authority'), _step_enroll),
    ('suspend', ('barrier', 'harness-home', 'harness-authority',
                 'knowledge-home', 'knowledge-authority'), _step_suspend),
    ('cutover', ('checkpoint', 'manifest', 'harness-home', 'harness-authority',
                 'knowledge-home', 'knowledge-authority'), _step_cutover),
    ('finalize', ('manifest',), _step_finalize),
)


# Ordered window-rollback steps: the forward chain through cutover (no
# finalize), then the Knowledge-era delta, the window fence, the reverse-chain
# production and the window rollback itself.
WINDOW_STEPS = STEPS[:10] + (
    ('window-delta', ('knowledge-home', 'lineage'), _step_window_delta),
    ('window-suspend', ('harness-home', 'harness-authority', 'knowledge-home',
                        'knowledge-authority'), _step_window_suspend),
    ('window-export', ('lineage',), _step_window_export),
    ('window-disposition', ('window-disposition',), _step_window_disposition),
    ('window-document-reverse', ('document-reverse',), _step_window_document_reverse),
    ('window-wiki-absent', ('wiki-side',), _step_window_wiki_absent),
    ('window-evidence', ('evidence',), _step_window_evidence),
    ('window-rollback', ('window-checkpoint', 'manifest', 'harness-home',
                         'harness-authority', 'knowledge-home',
                         'knowledge-authority'), _step_window_rollback),
    ('window-activate', ('rollback-activation', 'harness-home', 'knowledge-home'),
     _step_window_activate),
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
    if _read_private(ctx.path('knowledge-home') / 'active-installation.json') != pointer_raw:
        raise ValueError('Product active installation pointers diverged')
    pointer = _json(pointer_raw)
    if (pointer.get('format') != 'puddingharness-active-installation/v1'
            or pointer.get('cutover_manifest_sha256') != cutover['cutover_manifest_sha256']
            or pointer.get('prepared_manifest_sha256') != prepared
            or pointer.get('active_installation_revision') != 'sha256:' + prepared
            or pointer.get('harness_assigned_event_sha256') != cutover['harness_assigned_event_sha256']
            or pointer.get('knowledge_assigned_event_sha256') != cutover['knowledge_assigned_event_sha256']
            or pointer.get('source_freeze_receipt_sha256') != cutover['source_freeze_receipt_sha256']
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


def _verify_terminal_window(ctx):
    """Mirror the window-rollback orchestrator test assertions on the real run."""
    cutover = ctx.receipt('cutover')
    rolled = ctx.receipt('window-rollback')
    activated = ctx.receipt('window-activate')
    rolled_back = rolled['rolled_back_manifest_sha256']
    evidence_sha = rolled['rollback_evidence_sha256']
    manifest_path = ctx.path('manifest') / 'manifest.json'
    raw = _read_private(manifest_path)
    document = _json(raw)
    manifests.validate_manifest(document)
    if (document['state'] != 'ROLLED_BACK' or document['rollback_window_open'] is not True
            or document.get('completed_at') is not None
            or document['active_writers'] != {domain: 'puddingclaw'
                                              for domain in manifests._DOMAINS}
            or document.get('rollback_evidence_digest') != 'sha256:' + evidence_sha):
        raise ValueError('Rehearsal manifest is not rolled back')
    if _sha256(raw) != rolled_back:
        raise ValueError('Rolled back manifest digest changed')
    prepared = cutover['prepared_manifest_sha256']
    if document['active_installation_revision'] != 'sha256:' + prepared:
        raise ValueError('Rolled back manifest lost the committed prepared revision')
    stage = ctx.path('window-checkpoint')
    preserved_raw = _read_private(stage / 'cutover-manifest.json')
    if _sha256(preserved_raw) != cutover['cutover_manifest_sha256']:
        raise ValueError('Preserved cutover manifest changed')
    if _file_digest(stage / 'rolled-back-manifest.json')[0] != rolled_back:
        raise ValueError('Preserved rolled back manifest changed')
    preserved = _json(preserved_raw)
    for key in ('checkpoint', 'active_installation_revision', 'started_at',
                'staging_namespace'):
        if document.get(key) != preserved.get(key):
            raise ValueError('Rolled back manifest lost cutover history')
    harness_binding = authority.load_binding(ctx.path('harness-home'))
    knowledge_binding = inspect_binding(ctx.path('knowledge-home'))
    if harness_binding is None or knowledge_binding is None:
        raise ValueError('A product enrollment vanished during the rehearsal')
    harness_authority = Path(harness_binding['authority']['path'])
    knowledge_authority = Path(knowledge_binding['authority']['path'])
    journals = {'harness': authority.journal(harness_binding),
                'knowledge': authority.read(knowledge_authority / 'journal.json')}
    for side, journal in journals.items():
        events = journal['events']
        if len(events) != 5:
            raise ValueError('Rolled back writer journal changed')
        assigned, suspended, head = events[2], events[3], events[4]
        if (assigned['state'] != 'assigned' or assigned['operation_id'] != ctx.operation
                or assigned['sha256'] != cutover[side + '_assigned_event_sha256']):
            raise ValueError('Committed cutover journal changed')
        if (suspended['state'] != 'suspended'
                or suspended['operation_id'] != ctx.window_operation
                or suspended['previous'] != assigned['sha256']):
            raise ValueError('Window suspension changed')
        if (head['state'] != 'assigned' or head['operation_id'] != ctx.window_operation
                or head['previous'] != suspended['sha256']
                or head['freeze_receipt_sha256'] != suspended['freeze_receipt_sha256']
                or head['migration_manifest_sha256'] != rolled_back
                or head['active_installation_revision'] != 'sha256:' + rolled_back
                or head['rollback_evidence_sha256'] != evidence_sha
                or head['sha256'] != rolled[side + '_window_event_sha256']):
            raise ValueError('Window rollback assignment changed')
    if (journals['harness']['events'][4]['writer'] != 'puddingclaw'
            or journals['knowledge']['events'][4]['writers'] != {
                'knowledge_catalog': 'puddingclaw', 'connector_jobs': 'puddingclaw'}):
        raise ValueError('Window rollback writers changed')
    # The replacement products remain assigned away and fenced.  Legacy Home
    # activation is a separate transaction and must not thaw either new writer.
    for home, marker, journal in (
            (ctx.path('harness-home'), HARNESS_FREEZE_NAME, journals['harness']),
            (ctx.path('knowledge-home'), KNOWLEDGE_FREEZE_NAME, journals['knowledge'])):
        digest, _size = _file_digest(home / marker)
        if digest != journal['events'][3]['freeze_receipt_sha256']:
            raise ValueError('Window freeze marker changed')
    for root in (harness_authority, knowledge_authority):
        for name in ('freeze-marker-rev2.json', 'thaw-receipt-rev2.json'):
            authority.read(root / name)
        for name in ('freeze-marker-rev4.json', 'thaw-receipt-rev4.json'):
            if (root / name).exists() or (root / name).is_symlink():
                raise ValueError('A retired window artifact appeared')
    for home in (ctx.path('harness-home'), ctx.path('knowledge-home')):
        pointer = home / 'active-installation.json'
        if pointer.exists() or pointer.is_symlink():
            raise ValueError('A CUTOVER active installation pointer survived rollback activation')
    activation = rollback_activation.activate_rollback(
        ctx.source_home, ctx.path('harness-home'), ctx.path('knowledge-home'),
        ctx.path('document-reverse') / 'reverse', ctx.knowledge_python,
        ctx.path('rollback-activation'), ctx.path('window-checkpoint') / 'checkpoint.json',
        ctx.path('evidence') / 'evidence.json', ctx.path('credential-baseline.json'),
        source_operation_id=ctx.operation,
        rollback_operation_id=ctx.window_operation)
    if (activation.get('idempotent') is not True
            or activation.get('rollback_completed') is not True
            or activation.get('activation_receipt_sha256')
            != activated['activation_receipt_sha256']):
        raise ValueError('Legacy rollback activation receipt changed')
    source = Path(ctx.source_home)
    for marker in ('.installation-freeze-v1.json', '.installation-freeze-v1.json.part'):
        if (source / marker).exists() or (source / marker).is_symlink():
            raise ValueError('Legacy writer remained frozen after rollback activation')
    if _file_digest(source / 'db/catalog.sqlite3')[0] != activated['catalog_sha256']:
        raise ValueError('Activated legacy Catalog changed')
    # The rolled back installation stays fenced out for both products.
    guard = InstallationGuard(ctx.path('harness-home'))
    try:
        guard.acquire()
    except AdmissionUnavailable:
        pass
    else:
        guard.close()
        raise ValueError('Harness installation guard admitted a rolled back installation')
    opened = subprocess.run([ctx.knowledge_python, '-c',
                             'from knowledge_platform.local.workspace import '
                             'open_persistent_workspace;import sys;'
                             'open_persistent_workspace(sys.argv[1])',
                             str(ctx.path('knowledge-home'))],
                            stdin=subprocess.DEVNULL, capture_output=True,
                            timeout=ctx.timeout)
    if opened.returncode == 0:
        raise ValueError('Knowledge workspace opened a rolled back installation')
    try:
        authority.thaw(ctx.path('harness-home'), manifest_path, ctx.window_operation)
    except ValueError as error:
        if 'not assigned to this Harness' not in str(error):
            raise
    else:
        raise ValueError('Thaw admitted a rolled back installation')
    # ROLLED_BACK stays fail-closed for finalize and re-cutover.
    try:
        manifests.finalize_installation(manifest_path)
    except ValueError as error:
        if 'FINALIZED' not in str(error):
            raise
    else:
        raise ValueError('Finalize admitted a rolled back manifest')
    try:
        cutover_orchestrator.finalize_cutover(manifest_path, ctx.path('checkpoint'))
    except ValueError as error:
        if 'CUTOVER' not in str(error):
            raise
    else:
        raise ValueError('Cutover finalize admitted a rolled back manifest')
    probe = ctx.work / '.terminal-probe'
    try:
        try:
            cutover_orchestrator.cutover(ctx.path('harness-home'), ctx.path('knowledge-home'),
                                         ctx.knowledge_python, probe, manifest_path,
                                         'terminal-probe', source_home=ctx.source_home)
        except ValueError as error:
            if ('PREPARED' not in str(error)
                    and 'does not bind this operation and Home' not in str(error)
                    and 'not persistently frozen' not in str(error)):
                raise
        else:
            raise ValueError('Re-cutover admitted a rolled back manifest')
        if (probe / 'checkpoint.json').exists() or (probe / 'checkpoint.json').is_symlink():
            raise ValueError('Refused re-cutover published a checkpoint')
    finally:
        if probe.exists() or probe.is_symlink():
            shutil.rmtree(probe)
    if _read_private(manifest_path) != raw:
        raise ValueError('Refusal probes changed the manifest')
    # The final knowledge journal is the one the writer CLI reports.
    status = _run_cli([ctx.knowledge_python, '-m', 'knowledge_platform.local.writer_authority',
                       'status', '--state-dir', str(ctx.path('knowledge-home'))],
                      ctx.timeout, 'window-rollback')
    if status.get('journal') != journals['knowledge']:
        raise ValueError('Knowledge writer status drifted')
    return {'manifest_state': 'ROLLED_BACK', 'manifest_digest': 'sha256:' + rolled_back,
            'prepared_manifest_digest': 'sha256:' + prepared,
            'cutover_manifest_digest': 'sha256:' + cutover['cutover_manifest_sha256'],
            'rollback_evidence_digest': 'sha256:' + evidence_sha,
            'active_pointer_digest': 'sha256:' + cutover['active_pointer_sha256'],
            'harness_window_event_digest': 'sha256:' + rolled['harness_window_event_sha256'],
            'knowledge_window_event_digest': 'sha256:' + rolled['knowledge_window_event_sha256'],
            'rollback_activation_digest': 'sha256:' + activated['activation_receipt_sha256'],
            'rollback_window_open': True}


def _run_record(checkpoint, terminal):
    return {'format': RUN_FORMAT,
            'operation': checkpoint['operation'],
            'installation_id': checkpoint['installation_id'],
            'parameters_digest': checkpoint['parameters_digest'],
            'steps': [{'name': step['name'], 'idempotent': step['idempotent'],
                       'receipt_digest': _digest(_encoded(step['receipt'])),
                       'outputs': step['outputs']} for step in checkpoint['steps']],
            'terminal': terminal}


def _parameters_digest(args, work, knowledge_python, claw_python, window_operation):
    value = {'work_root': str(work), 'source_home': str(_path(args.source_home)),
             'knowledge_python': str(knowledge_python),
             'claw_python': str(claw_python), 'credential_owner': args.credential_owner,
             'installation_id': args.installation_id,
             'source_revision': args.source_revision,
             'source_schema_revision': args.source_schema_revision,
             'operation': args.operation,
             'scenario': args.scenario, 'window_operation': window_operation,
             'grafts': list(args.graft), 'mappings': list(args.mappings),
             'virtual_roots': list(args.virtual_roots),
             'repair_document': args.repair_document, 'repair_reason': args.repair_reason,
             'recommit_document': args.recommit_document, 'recommit_reason': args.recommit_reason,
             'exclusions': list(args.exclude), 'wiki_root_relative': args.wiki_root_relative,
             'timeout_seconds': args.timeout_seconds}
    return _digest(_encoded(value))


def run_rehearsal(args):
    if not _TOKEN.fullmatch(args.installation_id):
        raise ValueError('Invalid rehearsal installation identity')
    for value in (args.source_revision, args.source_schema_revision):
        if not _TOKEN.fullmatch(value):
            raise ValueError('Invalid rehearsal source identity')
    if not _OWNER.fullmatch(args.credential_owner):
        raise ValueError('Invalid rehearsal credential owner')
    authority._operation(args.operation)
    if args.scenario == 'path-a':
        if args.window_operation is not None:
            raise ValueError('--window-operation requires the window-rollback scenario')
        table, allowed, window_operation = STEPS, _KNOWN_TOP_LEVEL, None
    else:
        window_operation = args.window_operation or args.operation + '-window'
        authority._operation(window_operation)
        if window_operation == args.operation:
            raise ValueError('The window operation must differ from the cutover operation')
        table, allowed = WINDOW_STEPS, _WINDOW_TOP_LEVEL
    if type(args.timeout_seconds) is not int or not 1 <= args.timeout_seconds <= 3600:
        raise ValueError('Invalid rehearsal step timeout')
    if (args.repair_document is None) != (args.repair_reason is None):
        raise ValueError('Repair requires both a document id and a reason')
    if (args.recommit_document is None) != (args.recommit_reason is None):
        raise ValueError('Recommit requires both a document id and a reason')
    if args.repair_document is not None and args.repair_document == args.recommit_document:
        raise ValueError('Repair and recommit must target different documents')
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
    for virtual_root in args.virtual_roots:
        prefix, separator, destination = virtual_root.partition('=')
        if not separator or not prefix.startswith('/') or not destination:
            raise ValueError('Virtual roots must be VIRTUAL_PREFIX=DST_RELATIVE')
        _relative(destination)
    if not args.mappings:
        raise ValueError('At least one --map SRC=DST is required')
    knowledge_python = _validate_executable(args.knowledge_python)
    claw_python = _validate_executable(args.claw_python)
    work = _path(args.work_root)
    _mkdir(work)
    _clean_transients(work)
    _check_top_level(work, allowed)
    parameters = _parameters_digest(args, work, knowledge_python, claw_python, window_operation)
    checkpoint = _load_checkpoint(work, table)
    if checkpoint is not None and checkpoint['parameters_digest'] != parameters:
        if checkpoint['steps']:
            raise ValueError('Rehearsal parameters changed after steps committed')
        checkpoint = None
    if checkpoint is None:
        checkpoint = {'format': CHECKPOINT_FORMAT, 'work_root': str(work),
                      'operation': args.operation, 'installation_id': args.installation_id,
                      'parameters_digest': parameters, 'steps': []}
    else:
        next_roots = table[len(checkpoint['steps'])][1] if len(checkpoint['steps']) < len(table) else ()
        _verify_committed_roots(work, checkpoint['steps'], next_roots)
    if (work / RUN_RECORD_NAME).exists() and len(checkpoint['steps']) != len(table):
        raise ValueError('Rehearsal run record survives an incomplete checkpoint')

    ctx = _Context(args, work, knowledge_python, claw_python, window_operation)
    ctx.committed = {step['name']: step for step in checkpoint['steps']}
    report = []
    for name, roots, body in table:
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

    if args.scenario == 'window-rollback':
        terminal = _verify_terminal_window(ctx)
    else:
        terminal = _verify_terminal(ctx)
    record = _encoded(_run_record(checkpoint, terminal))
    run_record = work / RUN_RECORD_NAME
    if run_record.exists() or run_record.is_symlink():
        if _read_private(run_record) != record:
            raise ValueError('Rehearsal run record changed')
    else:
        _replace_private(run_record, record)
    result = {'format': FORMAT, 'operation': args.operation,
              'installation_id': args.installation_id, 'parameters_digest': parameters,
              'steps': report, 'terminal_state': terminal['manifest_state'],
              'manifest_digest': terminal['manifest_digest'],
              'run_record_digest': _digest(record),
              'activation_allowed': False, 'installation_cutover_performed': True,
              'production_activated': False}
    if args.scenario == 'window-rollback':
        return dict(result, status='rolled_back', window_operation=window_operation,
                    rollback_completed=True)
    return dict(result, status='finalized', rollback_completed=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-root', required=True,
                        help='Private rehearsal work root (created 0700 when missing)')
    parser.add_argument('--source-home', required=True,
                        help='Offline legacy PuddingClaw Home to persistently freeze and snapshot')
    parser.add_argument('--knowledge-python',
                        default=os.environ.get('KNOWLEDGE_TEST_PYTHON'),
                        help='Explicit independent Knowledge interpreter '
                             '(default: KNOWLEDGE_TEST_PYTHON)')
    parser.add_argument('--claw-python',
                        default=os.environ.get('CLAW_TEST_PYTHON'),
                        help='Explicit installed PuddingClaw interpreter '
                             '(default: CLAW_TEST_PYTHON)')
    parser.add_argument('--credential-owner', required=True,
                        help='Legacy PuddingClaw credential owner to rebind')
    parser.add_argument('--installation-id', required=True)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--source-schema-revision', required=True)
    parser.add_argument('--operation', required=True,
                        help='Writer-barrier/cutover operation id for this rehearsal')
    parser.add_argument('--scenario', choices=('path-a', 'window-rollback'), default='path-a',
                        help='path-a finalizes; window-rollback stops after cutover and '
                             'drives the real reverse chain to ROLLED_BACK')
    parser.add_argument('--window-operation',
                        help='Rollback-window operation id (window-rollback scenario only; '
                             'default: OPERATION-window)')
    parser.add_argument('--graft', action='append', default=[], metavar='SRC_DIR=DST_RELATIVE')
    parser.add_argument('--map', dest='mappings', action='append', default=[], metavar='SRC=DST')
    parser.add_argument('--virtual-root', dest='virtual_roots', action='append', default=[],
                        metavar='VIRTUAL_PREFIX=DST_RELATIVE',
                        help='Rebind absolute in-body references under VIRTUAL_PREFIX onto a snapshot-relative root')
    parser.add_argument('--repair-document', metavar='DOC_ID')
    parser.add_argument('--repair-reason', metavar='TEXT')
    parser.add_argument('--recommit-document', metavar='DOC_ID')
    parser.add_argument('--recommit-reason', metavar='TEXT')
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
        if not args.claw_python:
            raise ValueError('An explicit PuddingClaw interpreter is required')
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
