"""Installation Migration Manifest production and consumption (DISCOVERED/PREPARED).

The manifest contract is
``docs/knowledge-platform/installation-migration-manifest.schema.json``
(format ``agent-knowledge-platform-installation-migration/v1``).  This module is
the first Harness-side producer/consumer and deliberately covers only the
DISCOVERED and PREPARED states of the specification section 11.20 state machine.
CUTOVER, ROLLED_BACK and FINALIZED remain future increments: manifests carrying
those states validate structurally but are never reopened, advanced or
downgraded here.  No Knowledge source is imported; Knowledge evidence enters
only as the verified receipt digests committed by the offline migration
orchestrator's private staging.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import time

from harness.migration_orchestrator import (
    _check_stage as _check_orchestrator_stage,
    _digest,
    _json,
    _path,
    _private_directory,
    _read_private,
    _receipt,
    _replace_private,
)
from harness.session_import import (
    _check_stage as _check_harness_stage,
    digest,
    encoded,
    inventory,
    read_file,
)
from harness.source_snapshot import VerifiedSourceSnapshot

FORMAT = 'puddingharness-installation-manifest/v1'
MANIFEST_FORMAT = 'agent-knowledge-platform-installation-migration/v1'
ORCHESTRATOR_FORMAT = 'puddingharness-migration-orchestrator/v1'
HOME_FORMAT = 'puddingharness-home-import/v1'
LOCK_NAME = '.installation-manifest.lock'
# Mirrors the backend/pyproject.toml package version and moves with it.
HARNESS_TARGET = 'puddingharness-backend@0.1.0'
_HEX = '0123456789abcdef'
_DOMAINS = ('session_harness', 'knowledge_catalog', 'connector_jobs')
_WRITERS = ('puddingclaw', 'puddingharness', 'puddingknowledge')
_STATES = ('DISCOVERED', 'PREPARED', 'CUTOVER', 'ROLLED_BACK', 'FINALIZED')
_STRATEGIES = ('reverse_delta', 'snapshot_restore', 'no_write_until_finalized')
_REQUIRED = {
    'format', 'source', 'targets', 'object_summaries', 'id_resource_mappings',
    'credential_rebinds', 'active_writers', 'checkpoint', 'rollback_strategy',
    'state', 'rollback_window_open',
}
_DIGEST_FIELDS = {
    'snapshot_digest', 'rollback_evidence_digest', 'recovery_evidence_digest',
    'post_cutover_delta_digest', 'rollback_reconciliation_digest',
}
_TEXT_FIELDS = {'staging_namespace', 'active_installation_revision', 'completed_at'}
_OPTIONAL = _DIGEST_FIELDS | _TEXT_FIELDS | {
    'post_cutover_delta_count', 'rollback_delta_reconciled', 'failure_checkpoint',
    'recovery_count', 'started_at',
}
_MANIFEST_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,159}')


def _sha(value):
    return (isinstance(value, str) and len(value) == 71 and value.startswith('sha256:')
            and all(character in _HEX for character in value[7:]))


def _text(value, maximum=160):
    return isinstance(value, str) and 1 <= len(value) <= maximum


def _resource_uri(value):
    return isinstance(value, str) and re.fullmatch(r'(knowledge|harness)://[^\s]+', value) is not None


def _credential_uri(value):
    return isinstance(value, str) and re.fullmatch(r'credential://[^\s]+', value) is not None


def _rebind_shape(item):
    return (isinstance(item, dict) and set(item) == {'slot', 'source_ref_digest', 'target_ref', 'status'}
            and _text(item['slot']) and _sha(item['source_ref_digest']) and _credential_uri(item['target_ref'])
            and item['status'] in ('pending', 'rebound', 'failed'))


def validate_manifest(value):
    """Schema-faithful structural validation of the versioned manifest contract."""
    if not isinstance(value, dict) or not _REQUIRED <= set(value) or not set(value) <= _REQUIRED | _OPTIONAL:
        raise ValueError('Installation manifest keys are invalid')
    if value['format'] != MANIFEST_FORMAT:
        raise ValueError('Installation manifest format is unsupported')
    source = value['source']
    if (not isinstance(source, dict) or set(source) != {'installation_id', 'schema_revision', 'catalog_revision'}
            or not all(_text(source[key]) for key in source)):
        raise ValueError('Installation manifest source is invalid')
    targets = value['targets']
    if not isinstance(targets, dict) or not targets or not all(_text(item) for item in targets.values()):
        raise ValueError('Installation manifest targets are invalid')
    summaries = value['object_summaries']
    if not isinstance(summaries, list):
        raise ValueError('Installation manifest object summaries are invalid')
    domains = []
    for item in summaries:
        if (not isinstance(item, dict) or set(item) != {'domain', 'object_count', 'source_digest'}
                or item['domain'] not in _DOMAINS or type(item['object_count']) is not int
                or item['object_count'] < 0 or not _sha(item['source_digest'])):
            raise ValueError('Installation manifest object summary is invalid')
        domains.append(item['domain'])
    if len(set(domains)) != len(domains):
        raise ValueError('Installation manifest repeats an object summary domain')
    mappings = value['id_resource_mappings']
    if not isinstance(mappings, list) or any(
            not isinstance(item, dict) or set(item) != {'source_id', 'resource_uri'}
            or not _text(item['source_id']) or not _resource_uri(item['resource_uri']) for item in mappings):
        raise ValueError('Installation manifest resource mapping is invalid')
    rebinds = value['credential_rebinds']
    if not isinstance(rebinds, list) or any(not _rebind_shape(item) for item in rebinds):
        raise ValueError('Installation manifest credential rebind is invalid')
    slots = [item['slot'] for item in rebinds]
    if len(set(slots)) != len(slots):
        raise ValueError('Installation manifest repeats a credential slot')
    writers = value['active_writers']
    if (not isinstance(writers, dict) or set(writers) != set(_DOMAINS)
            or any(writers[domain] not in _WRITERS for domain in _DOMAINS)):
        raise ValueError('Installation manifest active writers are invalid')
    checkpoint = value['checkpoint']
    if not isinstance(checkpoint, dict) or not all(_text(item) for item in checkpoint.values()):
        raise ValueError('Installation manifest checkpoint is invalid')
    if value['rollback_strategy'] not in _STRATEGIES or value['state'] not in _STATES:
        raise ValueError('Installation manifest rollback strategy or state is invalid')
    if type(value['rollback_window_open']) is not bool:
        raise ValueError('Installation manifest rollback window flag is invalid')
    for key in _DIGEST_FIELDS & set(value):
        if value[key] is not None and not _sha(value[key]):
            raise ValueError('Installation manifest digest field is invalid')
    for key in _TEXT_FIELDS & set(value):
        if value[key] is not None and not _text(value[key]):
            raise ValueError('Installation manifest text field is invalid')
    if 'started_at' in value and not _text(value['started_at']):
        raise ValueError('Installation manifest start time is invalid')
    if 'failure_checkpoint' in value and value['failure_checkpoint'] is not None and not _text(value['failure_checkpoint'], 64):
        raise ValueError('Installation manifest failure checkpoint is invalid')
    for key in {'post_cutover_delta_count', 'recovery_count'} & set(value):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError('Installation manifest counter is invalid')
    if 'rollback_delta_reconciled' in value and type(value['rollback_delta_reconciled']) is not bool:
        raise ValueError('Installation manifest rollback reconciliation flag is invalid')


def _snapshot_checkpoint(commitment):
    return {'source_snapshot_format': commitment['format'],
            'source_snapshot_plan_sha256': commitment['plan_sha256'],
            'source_snapshot_manifest_sha256': commitment['manifest_sha256']}


def _skeleton(commitment, files, credential_rebinds):
    return {
        'format': MANIFEST_FORMAT,
        'source': {'installation_id': commitment['plan_sha256'],
                   'schema_revision': commitment['format'],
                   'catalog_revision': commitment['format']},
        'targets': {'puddingharness': HARNESS_TARGET},
        'object_summaries': [{'domain': 'session_harness', 'object_count': len(files),
                              'source_digest': digest(encoded(files))}],
        'id_resource_mappings': [],
        'credential_rebinds': [dict(item) for item in credential_rebinds],
        'active_writers': {domain: 'puddingclaw' for domain in _DOMAINS},
        'checkpoint': _snapshot_checkpoint(commitment),
        'rollback_strategy': 'no_write_until_finalized',
        'state': 'DISCOVERED',
        'rollback_window_open': True,
        'snapshot_digest': digest(encoded(commitment)),
        'staging_namespace': None,
        'active_installation_revision': None,
        'completed_at': None,
    }


def _prepared(discovered, orchestrator_checkpoint, staging_namespace, knowledge_target, started_at):
    if not _text(started_at):
        raise ValueError('Prepared installation manifest start time is invalid')
    prepared = dict(discovered)
    prepared['state'] = 'PREPARED'
    prepared['targets'] = {**discovered['targets'], 'puddingknowledge': knowledge_target}
    prepared['checkpoint'] = {
        **discovered['checkpoint'],
        'orchestrator_plan_digest': orchestrator_checkpoint['plan_digest'],
        'orchestrator_harness_plan_digest': orchestrator_checkpoint['harness_plan_digest'],
        'orchestrator_knowledge_receipt_digest': orchestrator_checkpoint['receipt_digest'],
    }
    prepared['staging_namespace'] = staging_namespace
    prepared['started_at'] = started_at
    validate_manifest(prepared)
    return prepared


def _pre_cutover_invariants(value):
    """Evidence-backed invariants this increment can assert before CUTOVER."""
    if any(value['active_writers'][domain] != 'puddingclaw' for domain in _DOMAINS):
        raise ValueError('Active writer drift before CUTOVER')
    if value['rollback_strategy'] != 'no_write_until_finalized' or value['rollback_window_open'] is not True:
        raise ValueError('Rollback policy drift before CUTOVER')
    if value['id_resource_mappings']:
        raise ValueError('Resource URI mappings are not minted before CUTOVER')
    if any(item['status'] != 'pending' for item in value['credential_rebinds']):
        raise ValueError('Credential rebind drift before CUTOVER')


def _result(manifest, idempotent):
    return {'format': FORMAT, 'state': manifest['state'],
            'manifest_digest': digest(encoded(manifest)),
            'snapshot_digest': manifest['snapshot_digest'], 'idempotent': idempotent,
            'activation_allowed': False, 'installation_prepared': False,
            'installation_cutover_performed': False, 'rollback_completed': False,
            'writer_fence_verified': False, 'credential_rebind_required': True}


def _declared_rebinds(credential_rebinds):
    if isinstance(credential_rebinds, (str, bytes)) or not isinstance(credential_rebinds, (list, tuple)):
        raise ValueError('Credential rebind declarations must be a sequence')
    result = []
    for item in credential_rebinds:
        if not _rebind_shape(item):
            raise ValueError('Credential rebind declaration is invalid')
        if item['status'] != 'pending':
            raise ValueError('Only pending credential rebinds can be declared before CUTOVER')
        result.append(dict(item))
    if len({item['slot'] for item in result}) != len(result):
        raise ValueError('Credential rebind declaration repeats a slot')
    return result


def _output_path(value):
    output = _path(value)
    if not _MANIFEST_NAME.fullmatch(output.name):
        raise ValueError('Installation manifest filename is invalid')
    return output


def _check_manifest_directory(directory, name):
    for entry in directory.iterdir():
        if entry.is_symlink():
            raise ValueError('Installation manifest directory entry is symlinked')
        if entry.name in {name, LOCK_NAME}:
            continue
        if re.fullmatch(r'\.' + re.escape(name) + r'\.tmp-[0-9a-f]{16}', entry.name):
            _read_private(entry)
            continue
        raise ValueError('Unknown installation manifest directory entry')


def _clean_temporary(directory, name):
    for entry in directory.iterdir():
        if re.fullmatch(r'\.' + re.escape(name) + r'\.tmp-[0-9a-f]{16}', entry.name):
            _read_private(entry)
            entry.unlink()


def _lock(directory):
    path = directory / LOCK_NAME
    if path.exists():
        _read_private(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('Installation manifest lock is invalid')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _staging_lock(stage):
    path = stage / '.orchestrator.lock'
    if not path.exists() or path.is_symlink():
        raise ValueError('Orchestrator staging is incomplete')
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('Orchestrator staging lock is invalid')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _stored_manifest(output):
    if not output.exists() and not output.is_symlink():
        return None
    value = _json(_read_private(output))
    validate_manifest(value)
    return value


def _verify_orchestrator_staging(snapshot, stage):
    plan = _json(_read_private(stage / 'plan.json'))
    expected = {'format', 'request_digest', 'source_identity', 'knowledge_python_identity',
                'knowledge_executable', 'knowledge_release_identity',
                'source_snapshot_commitment', 'plan_digest'}
    if set(plan) != expected or plan['format'] != ORCHESTRATOR_FORMAT:
        raise ValueError('Orchestrator plan shape is invalid')
    body = {key: plan[key] for key in plan if key != 'plan_digest'}
    plan_digest = _digest(json.dumps(body, sort_keys=True, separators=(',', ':')).encode())
    if plan['plan_digest'] != plan_digest:
        raise ValueError('Orchestrator plan digest mismatch')
    if plan['source_identity'] != _digest(str(snapshot.payload).encode()):
        raise ValueError('Orchestrator plan does not bind this source snapshot')
    if plan['source_snapshot_commitment'] != snapshot.commitment:
        raise ValueError('Orchestrator plan snapshot commitment mismatch')
    if _digest(_read_private(stage / 'knowledge-request.json')) != plan['request_digest']:
        raise ValueError('Orchestrator Knowledge request changed')
    checkpoint = _json(_read_private(stage / 'checkpoint.json'))
    if (set(checkpoint) != {'state', 'plan_digest', 'harness_plan_digest', 'receipt_digest'}
            or checkpoint['state'] != 'verified_inactive_partial'
            or checkpoint['plan_digest'] != plan_digest
            or not _sha(checkpoint['harness_plan_digest'])
            or not _sha(checkpoint['receipt_digest'])):
        raise ValueError('Orchestrator checkpoint is not a verified inactive partial migration')
    return plan, checkpoint


def _file_facts(value):
    if not isinstance(value, dict):
        raise ValueError('Harness Home plan file facts are invalid')
    for relative, fact in value.items():
        if (not isinstance(relative, str) or not relative or Path(relative).as_posix() != relative
                or Path(relative).is_absolute() or any(part in {'', '.', '..'} for part in Path(relative).parts)):
            raise ValueError('Harness Home plan file path is not portable')
        if (not isinstance(fact, dict) or set(fact) != {'digest', 'size'} or not _sha(fact['digest'])
                or type(fact['size']) is not int or fact['size'] < 0):
            raise ValueError('Harness Home plan file facts are invalid')


def _verify_harness_staging(snapshot, stage, checkpoint):
    harness = stage / 'harness'
    manifest = _json(_read_private(harness / 'manifest.json', 32 * 1024**2))
    if (set(manifest) != {'format', 'plan', 'plan_digest', 'state', 'activation_allowed',
                          'writer_fence_verified', 'credential_rebind_required'}
            or manifest['format'] != HOME_FORMAT or manifest['state'] != 'verified_inactive'
            or manifest['activation_allowed'] is not False
            or manifest['writer_fence_verified'] is not False
            or manifest['credential_rebind_required'] is not True):
        raise ValueError('Harness Home staging manifest is invalid')
    if manifest['plan_digest'] != checkpoint['harness_plan_digest']:
        raise ValueError('Harness Home plan does not match the orchestrator checkpoint')
    plan = manifest['plan']
    if not isinstance(plan, dict) or digest(encoded(plan)) != manifest['plan_digest']:
        raise ValueError('Harness Home plan digest mismatch')
    if (set(plan) != {'format', 'source_identity', 'snapshot', 'files'}
            or plan['format'] != HOME_FORMAT
            or plan['source_identity'] != digest(str(snapshot.payload).encode())):
        raise ValueError('Harness Home plan does not bind this source snapshot')
    home_snapshot = plan['snapshot']
    if (not isinstance(home_snapshot, dict)
            or set(home_snapshot) != {'source_config_digest', 'session_files',
                                      'settings_sections', 'untransferred_section_count'}):
        raise ValueError('Harness Home plan snapshot is invalid')
    _file_facts(plan['files'])
    session_files = home_snapshot['session_files']
    _file_facts(session_files)
    _check_harness_stage(harness, plan['files'])
    for relative, fact in plan['files'].items():
        if digest(read_file(harness / 'payload' / relative)) != fact['digest']:
            raise ValueError('Staged Harness Home payload changed')
    return session_files


def _knowledge_target(release):
    if (not isinstance(release, dict)
            or not isinstance(release.get('package'), str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,79}', release['package'])
            or not isinstance(release.get('version'), str)
            or not re.fullmatch(r'[0-9][A-Za-z0-9.!+_-]{0,79}', release['version'])):
        raise ValueError('Orchestrator Knowledge release identity is invalid')
    target = f"{release['package']}@{release['version']}"
    if not _text(target):
        raise ValueError('Knowledge target identity is too long')
    return target


def discover_installation(source_home_snapshot, output, *, credential_rebinds=(), _after_checkpoint=None):
    output = _output_path(output)
    rebinds = _declared_rebinds(credential_rebinds)
    with VerifiedSourceSnapshot(source_home_snapshot) as snapshot:
        directory = output.parent
        if directory == snapshot.root or directory.is_relative_to(snapshot.root) or snapshot.root.is_relative_to(directory):
            raise ValueError('Manifest directory and source snapshot must be disjoint')
        if not directory.exists():
            directory.mkdir(mode=0o700, parents=True)
        _private_directory(directory)
        fd = _lock(directory)
        try:
            _check_manifest_directory(directory, output.name)
            _clean_temporary(directory, output.name)
            files = inventory(snapshot.payload, require_sessions=False)
            skeleton = _skeleton(snapshot.commitment, files, rebinds)
            validate_manifest(skeleton)
            stored = _stored_manifest(output)
            if stored is not None:
                _pre_cutover_invariants(stored)
                if stored != skeleton:
                    raise ValueError('Existing installation manifest changed or already advanced')
                return _result(stored, True)
            snapshot.verify()
            _replace_private(output, encoded(skeleton))
            if _after_checkpoint:
                _after_checkpoint('DISCOVERED')
            return _result(skeleton, False)
        finally:
            os.close(fd)


def prepare_installation(source_home_snapshot, orchestrator_staging, knowledge_receipt, output, *,
                         _after_checkpoint=None):
    output = _output_path(output)
    stage, receipt_path = _path(orchestrator_staging), _path(knowledge_receipt)
    with VerifiedSourceSnapshot(source_home_snapshot) as snapshot:
        directory = output.parent
        roots = (snapshot.root, stage, directory)
        if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
               for index, a in enumerate(roots) for b in roots[index + 1:]):
            raise ValueError('Manifest, snapshot and staging roots must be disjoint')
        if not directory.exists() or directory.is_symlink():
            raise ValueError('Installation manifest is missing; run discover first')
        _private_directory(directory)
        _private_directory(stage)
        _check_orchestrator_stage(stage)
        fd = _lock(directory)
        staging_fd = _staging_lock(stage)
        try:
            _check_manifest_directory(directory, output.name)
            _clean_temporary(directory, output.name)
            stored = _stored_manifest(output)
            if stored is None:
                raise ValueError('Installation manifest is missing; run discover first')
            if stored['state'] not in ('DISCOVERED', 'PREPARED'):
                raise ValueError('Installation manifest state cannot be advanced by this increment')
            _pre_cutover_invariants(stored)
            plan, checkpoint = _verify_orchestrator_staging(snapshot, stage)
            session_files = _verify_harness_staging(snapshot, stage, checkpoint)
            files = inventory(snapshot.payload, require_sessions=False)
            if files != session_files:
                raise ValueError('Source snapshot session domain changed since Harness import')
            receipt = _receipt(_read_private(receipt_path), stage / 'knowledge',
                               plan['request_digest'], plan['source_identity'])
            receipt_digest = _digest(json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode())
            if receipt_digest != checkpoint['receipt_digest']:
                raise ValueError('Knowledge receipt does not match the orchestrator checkpoint')
            discovered = _skeleton(snapshot.commitment, files, stored['credential_rebinds'])
            knowledge_target = _knowledge_target(plan['knowledge_release_identity'])
            staging_namespace = digest(str(stage).encode())
            if stored['state'] == 'DISCOVERED':
                if stored != discovered:
                    raise ValueError('Discovered installation manifest drifted')
                prepared = _prepared(discovered, checkpoint, staging_namespace, knowledge_target,
                                     time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
                snapshot.verify()
                _replace_private(output, encoded(prepared))
                if _after_checkpoint:
                    _after_checkpoint('PREPARED')
                return _result(prepared, False)
            # started_at is stamped once at the PREPARED transition and carried
            # verbatim; every other field is independently re-derived on retry.
            expected = _prepared(discovered, checkpoint, staging_namespace, knowledge_target,
                                 stored.get('started_at'))
            if stored != expected:
                raise ValueError('Prepared installation manifest drifted')
            return _result(stored, True)
        finally:
            os.close(staging_fd)
            os.close(fd)


def _parse_rebind(value):
    slot, separator, rest = value.partition('|')
    source_ref_digest, separator2, target_ref = rest.partition('|')
    if not separator or not separator2:
        raise ValueError('Credential rebind must be SLOT|SHA256-DIGEST|CREDENTIAL-URI')
    return {'slot': slot, 'source_ref_digest': source_ref_digest,
            'target_ref': target_ref, 'status': 'pending'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    discover = commands.add_parser('discover', help='Inventory a verified source snapshot into a DISCOVERED manifest')
    discover.add_argument('--source-snapshot', type=Path, required=True,
                          help='Verified puddingclaw-source-home-snapshot/v1 snapshot envelope')
    discover.add_argument('--output', type=Path, required=True, help='Manifest file inside a private directory')
    discover.add_argument('--credential-rebind', action='append', default=[],
                          metavar='SLOT|SHA256-DIGEST|CREDENTIAL-URI',
                          help='Declare a pending credential rebind slot; repeatable')
    prepare = commands.add_parser('prepare', help='Advance a DISCOVERED manifest to PREPARED from orchestrator staging')
    prepare.add_argument('--source-snapshot', type=Path, required=True,
                         help='Verified puddingclaw-source-home-snapshot/v1 snapshot envelope')
    prepare.add_argument('--orchestrator-staging', type=Path, required=True)
    prepare.add_argument('--knowledge-receipt', type=Path, required=True,
                         help='Private JSON file carrying the orchestrator knowledge_receipt object')
    prepare.add_argument('--output', type=Path, required=True, help='Manifest file inside a private directory')
    args = parser.parse_args(argv)
    try:
        if args.command == 'discover':
            result = discover_installation(args.source_snapshot, args.output,
                credential_rebinds=[_parse_rebind(value) for value in args.credential_rebind])
        else:
            result = prepare_installation(args.source_snapshot, args.orchestrator_staging,
                                          args.knowledge_receipt, args.output)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error',
                          'error_code': 'installation_manifest_rejected',
                          'activation_allowed': False, 'installation_cutover_performed': False},
                         separators=(',', ':')))
        return 1
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
