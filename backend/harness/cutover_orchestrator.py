"""Two-phase CUTOVER commit reassigning both independent product writers.

Precondition: both writer journals suspended under one operation by the writer
suspension barrier, and a PREPARED installation migration manifest.  The
Harness rev2 assignment commits first, then the delegated Knowledge CLI commits
its rev2, the manifest advances PREPARED to CUTOVER, the active-installation
pointer is published in the Harness Home root, and only then are both freeze
markers thawed.  A crash anywhere leaves at least one side suspended and
fail-closed; exact retry resumes from the last committed checkpoint and
completed checkpoints never downgrade.  Knowledge commands execute through an
explicitly selected independent interpreter.  This command does not activate
production and performs no rollback.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re

from harness import installation_authority as authority
from harness import installation_manifest as manifests
from harness.home_freeze import _sync_directory
from harness.migration_orchestrator import (
    _delegate, _json, _read_private, _replace_private, _validate_executable,
    _installed_knowledge_identity,
)
from harness.target_freeze import _executable_identity, _record
from harness.knowledge_writer_receipt import (
    FREEZE_NAME, _private_bytes, inspect_binding, validate_receipt,
)

FORMAT = 'puddingharness-cutover-orchestrator/v1'
POINTER_FORMAT = 'puddingharness-active-installation/v1'
POINTER_NAME = 'active-installation.json'
PREPARED_COPY = 'prepared-manifest.json'
_ORDER = ('harness_assigned', 'both_assigned', 'manifest_cutover',
          'active_pointer_published', 'harness_thawed', 'both_thawed')
_HEX64 = re.compile(r'[0-9a-f]{64}')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def cutover(harness_home, knowledge_state, knowledge_python, checkpoint_dir, manifest,
            operation_id, *, timeout_seconds=120, _after_checkpoint=None):
    authority._operation(operation_id)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError('Invalid timeout')
    home, knowledge, stage, manifest_path = map(
        authority._path, (harness_home, knowledge_state, checkpoint_dir, manifest))
    harness_binding = authority.load_binding(home)
    if harness_binding is None:
        raise ValueError('Harness Home must already be enrolled')
    knowledge_binding = inspect_binding(knowledge)
    manifest_directory = authority.identity(manifest_path.parent)
    roots = (home, knowledge, stage, manifest_path.parent,
             Path(harness_binding['authority']['path']),
             Path(knowledge_binding['authority']['path']))
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Cutover roots must be disjoint')
    python = _validate_executable(knowledge_python)
    executable = _executable_identity(python)
    if not stage.exists():
        stage.mkdir(mode=0o700)
        _sync_directory(stage.parent)
    stage_identity = authority.identity(stage)
    with authority.lock(stage, exclusive=True) as fd:
        for entry in stage.iterdir():
            if entry.name in {'.writer-authority.lock', 'plan.json', 'checkpoint.json', PREPARED_COPY}:
                continue
            if re.fullmatch(r'\.(?:plan|checkpoint|prepared-manifest)\.json\.tmp-[0-9a-f]{16}', entry.name):
                authority.read(entry)
                continue
            raise ValueError('Unknown cutover checkpoint entry')
        release_identity = _installed_knowledge_identity(python, stage, fd, timeout_seconds)
        plan = {'format': FORMAT, 'operation_id': operation_id,
                'harness_binding': harness_binding, 'knowledge_binding': knowledge_binding,
                'checkpoint_identity': stage_identity, 'manifest_directory': manifest_directory,
                'manifest_name': manifest_path.name, 'knowledge_python': str(python),
                'knowledge_executable': executable, 'knowledge_release_identity': release_identity}
        import os
        lock_identity = os.fstat(fd)

        def verify_control_files():
            current = (stage/'.writer-authority.lock').lstat()
            if (current.st_dev, current.st_ino) != (lock_identity.st_dev, lock_identity.st_ino):
                raise ValueError('Cutover checkpoint lock changed')
            if authority.identity(stage) != stage_identity or _executable_identity(python) != executable:
                raise ValueError('Cutover checkpoint control changed')
            if authority.identity(manifest_path.parent) != manifest_directory:
                raise ValueError('Cutover manifest directory changed')
            if authority.load_binding(home) != harness_binding or inspect_binding(knowledge) != knowledge_binding:
                raise ValueError('Writer enrollment changed')

        def verify_control():
            verify_control_files()
            if _installed_knowledge_identity(python, stage, fd, timeout_seconds) != release_identity:
                raise ValueError('Installed Knowledge release changed during cutover')
            verify_control_files()

        def manifest_facts():
            raw = _read_private(manifest_path)
            document = _json(raw)
            manifests.validate_manifest(document)
            return raw, _sha(raw), document

        def harness_journal():
            return authority.journal(harness_binding)

        def suspended_harness(journal):
            head = journal['events'][-1]
            if head['state'] != 'suspended':
                raise ValueError('Harness writer is not suspended')
            if head['operation_id'] != operation_id:
                raise ValueError('Harness suspension belongs to another operation')
            _record(home, '.installation-freeze-v1.json', head['freeze_receipt_sha256'])

        def suspended_knowledge(journal):
            head = journal['events'][-1]
            if head['state'] != 'suspended':
                raise ValueError('Knowledge writer is not suspended')
            if head['operation_id'] != operation_id:
                raise ValueError('Knowledge suspension belongs to another operation')

        def assigned(journal, writer, prepared):
            head = manifests._assigned_event(journal, writer)
            if head['operation_id'] != operation_id:
                raise ValueError('Cutover assignment belongs to another operation')
            if head['migration_manifest_sha256'] != prepared:
                raise ValueError('Cutover assignment commits to a different manifest')
            return head

        def knowledge_command(action, *extra):
            command = [str(python), '-m', 'knowledge_platform.local.writer_authority', action,
                       '--state-dir', str(knowledge), *extra]
            raw = _delegate(command, stage, fd, timeout_seconds)
            verify_control()
            return validate_receipt(raw, knowledge, knowledge_binding)

        def knowledge_thaw_verified(journal):
            head = journal['events'][-1]
            root = Path(knowledge_binding['authority']['path'])
            marker = knowledge / FREEZE_NAME
            part = knowledge / (FREEZE_NAME + '.part')
            if marker.exists() or marker.is_symlink() or part.exists() or part.is_symlink():
                raise ValueError('Knowledge freeze marker survives thaw')
            receipt = authority.read(root / f"thaw-receipt-rev{head['revision']}.json")
            expected = {'format': 'puddingknowledge-writer-authority/v1', 'state': 'thawed',
                        'operation_id': operation_id, 'writer': 'puddingknowledge',
                        'revision': head['revision'], 'revision_sha256': head['sha256'],
                        'freeze_receipt_sha256': head['freeze_receipt_sha256'],
                        'migration_manifest_sha256': head['migration_manifest_sha256'],
                        'active_installation_revision': head['active_installation_revision']}
            if receipt != expected:
                raise ValueError('Knowledge thaw receipt changed')
            retired = root / f"freeze-marker-rev{head['revision']}.json"
            if _sha(_private_bytes(retired, 4096)) != head['freeze_receipt_sha256']:
                raise ValueError('Retired Knowledge freeze marker changed')
            return _sha(authority.encoded(receipt))

        verify_control()
        plan_path, checkpoint_path = stage/'plan.json', stage/'checkpoint.json'
        if plan_path.exists() or plan_path.is_symlink():
            if authority.read(plan_path) != plan:
                raise ValueError('Cutover plan changed')
        else:
            if checkpoint_path.exists() or checkpoint_path.is_symlink():
                raise ValueError('Cutover checkpoint has no plan')
            _replace_private(plan_path, authority.encoded(plan))
        # Both actual installations must validate before the first commit.
        harness_before = harness_journal()
        knowledge_before = knowledge_command('status')
        base_keys = {'format', 'operation_id', 'plan_sha256', 'prepared_manifest_sha256',
                     'activation_allowed', 'installation_cutover_performed',
                     'rollback_completed', 'production_activated'}
        old = None
        if checkpoint_path.exists() or checkpoint_path.is_symlink():
            old = authority.read(checkpoint_path)
            if old.get('state') not in _ORDER:
                raise ValueError('Invalid cutover checkpoint state')
            index = _ORDER.index(old['state'])
            extras = {'journals'}
            if index >= 2:
                extras.add('cutover_manifest_sha256')
            if index >= 3:
                extras.add('active_pointer_sha256')
            if index >= 4:
                extras.add('harness_thaw_receipt_sha256')
            if index >= 5:
                extras.add('knowledge_thaw_receipt_sha256')
            if set(old) != base_keys | {'state'} | extras:
                raise ValueError('Invalid cutover checkpoint')
            if not isinstance(old['journals'], dict) or set(old['journals']) != (
                    {'harness'} if old['state'] == 'harness_assigned' else {'harness', 'knowledge'}):
                raise ValueError('Invalid cutover checkpoint journals')
            if old['installation_cutover_performed'] is not (old['state'] == 'both_thawed'):
                raise ValueError('Invalid cutover checkpoint flags')
            prepared = old['prepared_manifest_sha256']
            if not isinstance(prepared, str) or _HEX64.fullmatch(prepared) is None:
                raise ValueError('Invalid cutover checkpoint manifest commitment')
            for key in extras - {'journals'}:
                if not isinstance(old[key], str) or _HEX64.fullmatch(old[key]) is None:
                    raise ValueError('Invalid cutover checkpoint digest')
        prepared_copy = stage / PREPARED_COPY
        if old is None:
            raw, prepared, document = manifest_facts()
            if document['state'] != 'PREPARED':
                raise ValueError('Cutover requires a PREPARED installation manifest')
            if prepared_copy.exists() or prepared_copy.is_symlink():
                if _read_private(prepared_copy) != raw:
                    raise ValueError('Prepared manifest copy changed')
            else:
                _replace_private(prepared_copy, raw)
        else:
            if _sha(_read_private(prepared_copy)) != prepared:
                raise ValueError('Prepared manifest copy changed')
            _, current, document = manifest_facts()
            if old['state'] == 'harness_assigned':
                if document['state'] != 'PREPARED' or current != prepared:
                    raise ValueError('Installation manifest changed before Knowledge assignment')
            elif old['state'] == 'both_assigned':
                # A crash between the manifest advance and its checkpoint leaves
                # a CUTOVER manifest; the advance retry below binds it exactly.
                if document['state'] == 'PREPARED' and current != prepared:
                    raise ValueError('Installation manifest changed before cutover advance')
                if document['state'] not in ('PREPARED', 'CUTOVER'):
                    raise ValueError('Installation manifest changed before cutover advance')
            elif document['state'] != 'CUTOVER' or current != old['cutover_manifest_sha256']:
                raise ValueError('Cutover installation manifest changed')
        base = {'format': FORMAT, 'operation_id': operation_id, 'plan_sha256': authority.digest(plan),
                'prepared_manifest_sha256': prepared, 'activation_allowed': False,
                'installation_cutover_performed': False, 'rollback_completed': False,
                'production_activated': False}
        if old is not None and any(old[key] != value for key, value in base.items()
                                   if key != 'installation_cutover_performed'):
            raise ValueError('Cutover checkpoint changed')
        # Precondition: both writers suspended under this operation, unless the
        # journal already carries this operation's assignment (crash window).
        if old is None:
            if harness_before['events'][-1]['state'] == 'assigned':
                assigned(harness_before, 'puddingharness', prepared)
            else:
                suspended_harness(harness_before)
            if knowledge_before['events'][-1]['state'] == 'assigned':
                raise ValueError('Knowledge assignment has no cutover checkpoint')
            suspended_knowledge(knowledge_before)
        else:
            if old['journals']['harness'] != harness_before:
                raise ValueError('Committed Harness writer revision changed')
            if 'knowledge' in old['journals'] and old['journals']['knowledge'] != knowledge_before:
                raise ValueError('Committed Knowledge writer revision changed')
            if old['state'] == 'harness_assigned' and knowledge_before['events'][-1]['state'] == 'suspended':
                suspended_knowledge(knowledge_before)
        verify_control()
        # Both assignments commit to the preserved PREPARED bytes, never to a
        # manifest that may already have advanced.
        harness_result = authority.assign(home, prepared_copy, operation_id, 'puddingharness')
        harness_head = assigned(harness_result, 'puddingharness', prepared)
        if old is not None and old['journals']['harness'] != harness_result:
            raise ValueError('Committed Harness writer revision changed')

        def reached(state):
            return old is not None and _ORDER.index(old['state']) >= _ORDER.index(state)

        def commit(state, **extra):
            result = dict(base, state=state, **extra)
            if not reached(state):
                _replace_private(checkpoint_path, authority.encoded(result))
                if _after_checkpoint:
                    _after_checkpoint(state)
            return result

        commit('harness_assigned', journals={'harness': harness_result})
        verify_control()
        knowledge_result = knowledge_command('assign', '--operation-id', operation_id,
                                             '--manifest', str(prepared_copy),
                                             '--writer', 'puddingknowledge')
        knowledge_head = assigned(knowledge_result, 'puddingknowledge', prepared)
        if harness_journal() != harness_result:
            raise ValueError('Harness writer changed during Knowledge assignment')
        if old is not None and 'knowledge' in old['journals'] and old['journals']['knowledge'] != knowledge_result:
            raise ValueError('Committed Knowledge writer revision changed')
        journals = {'harness': harness_result, 'knowledge': knowledge_result}
        commit('both_assigned', journals=journals)
        verify_control()
        advance = manifests.cutover_installation(
            manifest_path, harness_journal=harness_result, knowledge_journal=knowledge_result)
        _, cutover_hex, cutover_document = manifest_facts()
        if advance['manifest_digest'] != 'sha256:' + cutover_hex or cutover_document['state'] != 'CUTOVER':
            raise ValueError('Cutover manifest advance changed')
        if reached('manifest_cutover') and old['cutover_manifest_sha256'] != cutover_hex:
            raise ValueError('Committed cutover manifest changed')
        commit('manifest_cutover', journals=journals, cutover_manifest_sha256=cutover_hex)
        verify_control()
        pointer = {'format': POINTER_FORMAT, 'operation_id': operation_id,
                   'cutover_manifest_sha256': cutover_hex, 'prepared_manifest_sha256': prepared,
                   'active_installation_revision': 'sha256:' + prepared,
                   'harness_assigned_event_sha256': harness_head['sha256'],
                   'knowledge_assigned_event_sha256': knowledge_head['sha256'],
                   'active_writers': dict(manifests._CUTOVER_WRITERS)}
        pointer_bytes = authority.encoded(pointer)
        pointer_path = home / POINTER_NAME
        if pointer_path.exists() or pointer_path.is_symlink():
            if _read_private(pointer_path) != pointer_bytes:
                raise ValueError('Active installation pointer changed')
        else:
            _replace_private(pointer_path, pointer_bytes)
        if reached('active_pointer_published') and old['active_pointer_sha256'] != _sha(pointer_bytes):
            raise ValueError('Committed active installation pointer changed')
        commit('active_pointer_published', journals=journals, cutover_manifest_sha256=cutover_hex,
               active_pointer_sha256=_sha(pointer_bytes))
        verify_control()
        # Thaw only after the manifest is CUTOVER and the pointer is published.
        harness_thaw = authority.thaw(home, prepared_copy, operation_id)
        if harness_thaw['journal'] != harness_result:
            raise ValueError('Harness writer journal changed during thaw')
        if reached('harness_thawed') and old['harness_thaw_receipt_sha256'] != harness_thaw['thaw_receipt_sha256']:
            raise ValueError('Committed Harness thaw receipt changed')
        commit('harness_thawed', journals=journals, cutover_manifest_sha256=cutover_hex,
               active_pointer_sha256=_sha(pointer_bytes),
               harness_thaw_receipt_sha256=harness_thaw['thaw_receipt_sha256'])
        verify_control()
        knowledge_thaw = knowledge_command('thaw', '--operation-id', operation_id,
                                           '--manifest', str(prepared_copy))
        if knowledge_thaw != knowledge_result:
            raise ValueError('Knowledge writer journal changed during thaw')
        knowledge_receipt_sha256 = knowledge_thaw_verified(knowledge_thaw)
        result = dict(base, state='both_thawed', journals=journals,
                      cutover_manifest_sha256=cutover_hex,
                      active_pointer_sha256=_sha(pointer_bytes),
                      harness_thaw_receipt_sha256=harness_thaw['thaw_receipt_sha256'],
                      knowledge_thaw_receipt_sha256=knowledge_receipt_sha256,
                      installation_cutover_performed=True)
        if reached('both_thawed') and old != result:
            raise ValueError('Completed cutover changed')
        verify_control()
        if not reached('both_thawed'):
            _replace_private(checkpoint_path, authority.encoded(result))
            if _after_checkpoint:
                _after_checkpoint('both_thawed')
        return result


def finalize_cutover(manifest, checkpoint_dir):
    """Advance a CUTOVER manifest to FINALIZED after a completed cutover checkpoint."""
    stage, manifest_path = map(authority._path, (checkpoint_dir, manifest))
    if not stage.is_dir():
        raise ValueError('Cutover checkpoint directory is missing')
    with authority.lock(stage, exclusive=True):
        checkpoint = authority.read(stage / 'checkpoint.json')
        if (checkpoint.get('format') != FORMAT or checkpoint.get('state') != 'both_thawed'
                or checkpoint.get('installation_cutover_performed') is not True):
            raise ValueError('Cutover checkpoint is not a completed cutover')
        journals = checkpoint.get('journals')
        if not isinstance(journals, dict) or set(journals) != {'harness', 'knowledge'}:
            raise ValueError('Cutover checkpoint journals are invalid')
        plan = authority.read(stage / 'plan.json')
        if authority.digest(plan) != checkpoint['plan_sha256']:
            raise ValueError('Cutover checkpoint plan changed')
        authority._operation(checkpoint['operation_id'])
        raw = _read_private(manifest_path)
        document = _json(raw)
        manifests.validate_manifest(document)
        if document['state'] not in ('CUTOVER', 'FINALIZED'):
            raise ValueError('Finalize requires a CUTOVER installation manifest')
        if document['state'] == 'CUTOVER' and _sha(raw) != checkpoint['cutover_manifest_sha256']:
            raise ValueError('Cutover installation manifest changed')
        for journal, writer in ((journals['harness'], 'puddingharness'),
                                (journals['knowledge'], 'puddingknowledge')):
            head = manifests._assigned_event(journal, writer)
            if head['operation_id'] != checkpoint['operation_id']:
                raise ValueError('Cutover checkpoint assignment operation changed')
            if head['migration_manifest_sha256'] != checkpoint['prepared_manifest_sha256']:
                raise ValueError('Cutover checkpoint manifest commitment changed')
        registration = {'harness_assigned_event_sha256': 'sha256:' + journals['harness']['events'][2]['sha256'],
                        'knowledge_assigned_event_sha256': 'sha256:' + journals['knowledge']['events'][2]['sha256']}
        if any(document['checkpoint'].get(key) != value for key, value in registration.items()):
            raise ValueError('Finalized candidate does not bind the completed cutover')
        if document['active_installation_revision'] != 'sha256:' + checkpoint['prepared_manifest_sha256']:
            raise ValueError('Finalized candidate does not bind the completed cutover')
        result = manifests.finalize_installation(manifest_path)
        return {'format': FORMAT, 'state': result['state'], 'manifest_digest': result['manifest_digest'],
                'idempotent': result['idempotent'], 'activation_allowed': False,
                'installation_cutover_performed': True, 'rollback_completed': False,
                'production_activated': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('cutover', help='Two-phase CUTOVER commit reassigning both product writers')
    for name in ('harness-home', 'knowledge-state', 'knowledge-python', 'checkpoint-dir',
                 'manifest', 'operation-id'):
        run.add_argument('--'+name, required=True)
    run.add_argument('--timeout-seconds', type=int, default=120)
    finalize = commands.add_parser('finalize', help='Advance a CUTOVER manifest to FINALIZED after a completed cutover')
    finalize.add_argument('--manifest', required=True)
    finalize.add_argument('--checkpoint-dir', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'cutover':
            result = cutover(args.harness_home, args.knowledge_state, args.knowledge_python,
                             args.checkpoint_dir, args.manifest, args.operation_id,
                             timeout_seconds=args.timeout_seconds)
        else:
            result = finalize_cutover(args.manifest, args.checkpoint_dir)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error',
                          'error_code': 'installation_cutover_rejected', 'activation_allowed': False,
                          'installation_cutover_performed': False, 'rollback_completed': False,
                          'production_activated': False}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
