"""Reverse-direction rollback reassigning both independent product writers.

This module carries two rollback commands:

- ``rollback`` (abort path): both writer journals suspended under one operation
  by the writer suspension barrier, a PREPARED installation migration manifest,
  and a private rollback evidence file produced out-of-band by the Knowledge
  reverse chain (this orchestrator binds it by digest only).  The manifest first
  advances PREPARED to ROLLED_BACK bound to the evidence digest, then the
  Harness rev2 rollback assignment commits (writer puddingclaw), then the
  delegated Knowledge CLI commits its rev2 rollback assignment (both domains
  puddingclaw), and both freeze markers are verified to still be in place.
- ``window-rollback`` (post-cutover window path, specification 11.20 point 5):
  a completed cutover (both journals rev2 assigned to the new products,
  manifest CUTOVER, both products thawed) is rolled back inside the
  installation rollback window under a NEW operation.  Both writers are
  re-suspended at rev3 (each suspension re-applies that product's freeze
  marker, verified against the rev3 freeze receipts) before anything else
  runs, the manifest advances CUTOVER to ROLLED_BACK bound to the post-cutover
  rollback evidence digest with active writers flipped back to puddingclaw,
  then both writers are assigned back to puddingclaw at rev4 against the
  preserved ROLLED_BACK bytes and the evidence digest.

A crash anywhere leaves both products frozen and fail-closed; exact retry
resumes from the last committed checkpoint and completed checkpoints never
downgrade.  Knowledge commands execute through an explicitly selected
independent interpreter.  These commands never thaw: both new products' freeze
markers must remain, the legacy source Home's thaw is the source product's own
responsibility, and ROLLED_BACK to FINALIZED stays fail-closed in the manifest
layer.  Re-cutover after a window rollback is a new migration operation and is
out of scope here.  Neither command activates production.
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

FORMAT = 'puddingharness-rollback-orchestrator/v1'
WINDOW_FORMAT = 'puddingharness-window-rollback-orchestrator/v1'
PREPARED_COPY = 'prepared-manifest.json'
CUTOVER_COPY = 'cutover-manifest.json'
ROLLED_BACK_COPY = 'rolled-back-manifest.json'
_ORDER = ('manifest_rolled_back', 'harness_reassigned', 'both_reassigned')
_WINDOW_ORDER = ('harness_resuspended', 'both_resuspended', 'manifest_rolled_back',
                 'harness_reassigned', 'both_reassigned')
_HEX64 = re.compile(r'[0-9a-f]{64}')
_JOURNALS = {'manifest_rolled_back': set(), 'harness_reassigned': {'harness'},
             'both_reassigned': {'harness', 'knowledge'}}
_WINDOW_JOURNALS = {'harness_resuspended': {'harness'},
                    'both_resuspended': {'harness', 'knowledge'},
                    'manifest_rolled_back': {'harness', 'knowledge'},
                    'harness_reassigned': {'harness', 'knowledge'},
                    'both_reassigned': {'harness', 'knowledge'}}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _rolled_back_event(journal, side):
    """Pin the assigned rollback head of a writer journal.

    Accepts the 3-event abort chain (rev1 suspended, rev2 assigned) or the
    5-event post-cutover window chain (rev3 suspended, rev4 assigned, where the
    rev3 suspension follows the rev2 cutover assignment head).
    """
    events = journal.get('events') if isinstance(journal, dict) else None
    if not isinstance(events, list) or len(events) not in (3, 5):
        raise ValueError('Rollback journal does not contain an abort or window chain')
    revision = len(events) - 1
    suspended, head = events[-2], events[-1]
    if not isinstance(suspended, dict) or not isinstance(head, dict):
        raise ValueError('Rollback journal events are invalid')
    if revision == 4 and not _follows_cutover_assignment(events):
        raise ValueError('Window rollback suspension does not follow its cutover assignment')
    if (suspended.get('revision') != revision - 1 or suspended.get('state') != 'suspended'
            or suspended.get('operation_id') != head.get('operation_id')):
        raise ValueError('Rollback assignment does not bind its suspension')
    if head.get('revision') != revision or head.get('state') != 'assigned':
        raise ValueError('Rollback journal head is not the assigned rollback revision')
    if side == 'harness':
        if head.get('writer') != 'puddingclaw':
            raise ValueError('Harness rollback assignment writer is invalid')
    elif head.get('writers') != {'knowledge_catalog': 'puddingclaw',
                                 'connector_jobs': 'puddingclaw'}:
        raise ValueError('Knowledge rollback assignment writers are invalid')
    evidence = head.get('rollback_evidence_sha256')
    if not isinstance(evidence, str) or _HEX64.fullmatch(evidence) is None:
        raise ValueError('Rollback assignment does not commit to rollback evidence')
    commitment = head.get('migration_manifest_sha256')
    if not isinstance(commitment, str) or _HEX64.fullmatch(commitment) is None:
        raise ValueError('Rollback assignment manifest commitment is invalid')
    if head.get('active_installation_revision') != 'sha256:' + commitment:
        raise ValueError('Rollback assignment does not bind the active installation revision')
    # Journals normally arrive fully chain-validated from the writer authority
    # layer; the self-digest is re-verified so bare journal files also fail closed.
    for event in (suspended, head):
        _self_digest(event)
    return head


def _follows_cutover_assignment(events):
    """The rev3 window suspension must follow the rev2 cutover assignment head."""
    cutover, suspended = events[2], events[3]
    return (isinstance(cutover, dict) and isinstance(suspended, dict)
            and cutover.get('revision') == 2 and cutover.get('state') == 'assigned'
            and suspended.get('revision') == 3 and suspended.get('state') == 'suspended'
            and suspended.get('previous') == cutover.get('sha256'))


def _self_digest(event):
    payload = {key: item for key, item in event.items() if key != 'sha256'}
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n'
    if event.get('sha256') != hashlib.sha256(canonical.encode()).hexdigest():
        raise ValueError('Rollback journal event digest mismatch')


def _window_suspended_event(journal):
    """Pin the suspended revision 3 head of a post-cutover writer journal."""
    events = journal.get('events') if isinstance(journal, dict) else None
    if not isinstance(events, list) or len(events) not in (4, 5):
        raise ValueError('Window rollback journal does not contain a suspended revision 3')
    if not _follows_cutover_assignment(events):
        raise ValueError('Window rollback suspension does not follow its cutover assignment')
    suspended = events[3]
    _self_digest(suspended)
    return suspended


def rollback(harness_home, knowledge_state, knowledge_python, checkpoint_dir, manifest,
             rollback_evidence, operation_id, *, timeout_seconds=120, _after_checkpoint=None):
    authority._operation(operation_id)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError('Invalid timeout')
    home, knowledge, stage, manifest_path, evidence_path = map(
        authority._path, (harness_home, knowledge_state, checkpoint_dir, manifest, rollback_evidence))
    harness_binding = authority.load_binding(home)
    if harness_binding is None:
        raise ValueError('Harness Home must already be enrolled')
    knowledge_binding = inspect_binding(knowledge)
    manifest_directory = authority.identity(manifest_path.parent)
    evidence_directory = authority.identity(evidence_path.parent)
    roots = (home, knowledge, stage, manifest_path.parent, evidence_path.parent,
             Path(harness_binding['authority']['path']),
             Path(knowledge_binding['authority']['path']))
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Rollback roots must be disjoint')
    python = _validate_executable(knowledge_python)
    executable = _executable_identity(python)
    if not stage.exists():
        stage.mkdir(mode=0o700)
        _sync_directory(stage.parent)
    stage_identity = authority.identity(stage)
    with authority.lock(stage, exclusive=True) as fd:
        for entry in stage.iterdir():
            if entry.name in {'.writer-authority.lock', 'plan.json', 'checkpoint.json',
                              PREPARED_COPY, ROLLED_BACK_COPY}:
                continue
            if re.fullmatch(r'\.(?:plan|checkpoint|prepared-manifest|rolled-back-manifest)\.json\.tmp-[0-9a-f]{16}', entry.name):
                authority.read(entry)
                continue
            raise ValueError('Unknown rollback checkpoint entry')
        release_identity = _installed_knowledge_identity(python, stage, fd, timeout_seconds)
        evidence_bytes = _read_private(evidence_path)
        evidence_sha = _sha(evidence_bytes)
        plan = {'format': FORMAT, 'operation_id': operation_id,
                'harness_binding': harness_binding, 'knowledge_binding': knowledge_binding,
                'checkpoint_identity': stage_identity, 'manifest_directory': manifest_directory,
                'manifest_name': manifest_path.name, 'evidence_directory': evidence_directory,
                'rollback_evidence_sha256': evidence_sha, 'knowledge_python': str(python),
                'knowledge_executable': executable, 'knowledge_release_identity': release_identity}
        import os
        lock_identity = os.fstat(fd)

        def verify_control_files():
            current = (stage/'.writer-authority.lock').lstat()
            if (current.st_dev, current.st_ino) != (lock_identity.st_dev, lock_identity.st_ino):
                raise ValueError('Rollback checkpoint lock changed')
            if authority.identity(stage) != stage_identity or _executable_identity(python) != executable:
                raise ValueError('Rollback checkpoint control changed')
            if authority.identity(manifest_path.parent) != manifest_directory:
                raise ValueError('Rollback manifest directory changed')
            if authority.identity(evidence_path.parent) != evidence_directory:
                raise ValueError('Rollback evidence directory changed')
            if _sha(_read_private(evidence_path)) != evidence_sha:
                raise ValueError('Rollback evidence changed')
            if authority.load_binding(home) != harness_binding or inspect_binding(knowledge) != knowledge_binding:
                raise ValueError('Writer enrollment changed')

        def verify_control():
            verify_control_files()
            if _installed_knowledge_identity(python, stage, fd, timeout_seconds) != release_identity:
                raise ValueError('Installed Knowledge release changed during rollback')
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

        def assigned(journal, side, rolled_back):
            head = _rolled_back_event(journal, side)
            if head['operation_id'] != operation_id:
                raise ValueError('Rollback assignment belongs to another operation')
            if head['migration_manifest_sha256'] != rolled_back:
                raise ValueError('Rollback assignment commits to a different manifest')
            if head['rollback_evidence_sha256'] != evidence_sha:
                raise ValueError('Rollback assignment commits to different evidence')
            return head

        def knowledge_command(action, *extra):
            command = [str(python), '-m', 'knowledge_platform.local.writer_authority', action,
                       '--state-dir', str(knowledge), *extra]
            raw = _delegate(command, stage, fd, timeout_seconds)
            verify_control()
            return validate_receipt(raw, knowledge, knowledge_binding)

        def freeze_markers_held(harness_head, knowledge_head):
            knowledge_marker = knowledge / FREEZE_NAME
            for marker in (home / '.installation-freeze-v1.json', knowledge_marker):
                if not marker.exists() or marker.is_symlink():
                    raise ValueError('Freeze marker vanished during rollback reassignment')
            part = knowledge / (FREEZE_NAME + '.part')
            if part.exists() or part.is_symlink():
                raise ValueError('Knowledge freeze publication is incomplete')
            _record(home, '.installation-freeze-v1.json', harness_head['freeze_receipt_sha256'])
            if _sha(_private_bytes(knowledge_marker, 4096)) != knowledge_head['freeze_receipt_sha256']:
                raise ValueError('Knowledge freeze marker changed during rollback reassignment')

        verify_control()
        plan_path, checkpoint_path = stage/'plan.json', stage/'checkpoint.json'
        if plan_path.exists() or plan_path.is_symlink():
            if authority.read(plan_path) != plan:
                raise ValueError('Rollback plan changed')
        else:
            if checkpoint_path.exists() or checkpoint_path.is_symlink():
                raise ValueError('Rollback checkpoint has no plan')
            _replace_private(plan_path, authority.encoded(plan))
        # Both actual installations must validate before the first commit.
        harness_before = harness_journal()
        knowledge_before = knowledge_command('status')
        base_keys = {'format', 'operation_id', 'plan_sha256', 'prepared_manifest_sha256',
                     'rollback_evidence_sha256', 'activation_allowed',
                     'installation_cutover_performed', 'rollback_completed', 'production_activated'}
        old = None
        if checkpoint_path.exists() or checkpoint_path.is_symlink():
            old = authority.read(checkpoint_path)
            if old.get('state') not in _ORDER:
                raise ValueError('Invalid rollback checkpoint state')
            if set(old) != base_keys | {'state', 'journals', 'rolled_back_manifest_sha256'}:
                raise ValueError('Invalid rollback checkpoint')
            if not isinstance(old['journals'], dict) or set(old['journals']) != _JOURNALS[old['state']]:
                raise ValueError('Invalid rollback checkpoint journals')
            if old['rollback_completed'] is not (old['state'] == 'both_reassigned'):
                raise ValueError('Invalid rollback checkpoint flags')
            for key in ('prepared_manifest_sha256', 'rollback_evidence_sha256',
                        'rolled_back_manifest_sha256'):
                if not isinstance(old[key], str) or _HEX64.fullmatch(old[key]) is None:
                    raise ValueError('Invalid rollback checkpoint digest')
            if old['rollback_evidence_sha256'] != evidence_sha:
                raise ValueError('Rollback checkpoint commits to different evidence')
        prepared_copy = stage / PREPARED_COPY
        rolled_copy = stage / ROLLED_BACK_COPY
        raw, current, document = manifest_facts()
        if old is None:
            if document['state'] == 'PREPARED':
                if rolled_copy.exists() or rolled_copy.is_symlink():
                    raise ValueError('Rolled back manifest copy has no rolled back manifest')
                prepared = current
                if prepared_copy.exists() or prepared_copy.is_symlink():
                    if _read_private(prepared_copy) != raw:
                        raise ValueError('Prepared manifest copy changed')
                else:
                    _replace_private(prepared_copy, raw)
            elif document['state'] == 'ROLLED_BACK':
                # A crash between the manifest advance and its first checkpoint
                # leaves a ROLLED_BACK manifest; the preserved PREPARED copy pins
                # the advance and the retry below binds it exactly.
                if not prepared_copy.exists() or prepared_copy.is_symlink():
                    raise ValueError('Rolled back manifest has no prepared copy')
                prepared_raw = _read_private(prepared_copy)
                prepared_document = _json(prepared_raw)
                manifests.validate_manifest(prepared_document)
                if prepared_document['state'] != 'PREPARED':
                    raise ValueError('Prepared manifest copy is not PREPARED')
                prepared = _sha(prepared_raw)
                if document.get('rollback_evidence_digest') != 'sha256:' + evidence_sha:
                    raise ValueError('Rolled back manifest commits to different evidence')
            else:
                raise ValueError('Rollback requires a PREPARED installation manifest')
        else:
            prepared = old['prepared_manifest_sha256']
            if _sha(_read_private(prepared_copy)) != prepared:
                raise ValueError('Prepared manifest copy changed')
            if document['state'] != 'ROLLED_BACK' or current != old['rolled_back_manifest_sha256']:
                raise ValueError('Rolled back installation manifest changed')
        base = {'format': FORMAT, 'operation_id': operation_id, 'plan_sha256': authority.digest(plan),
                'prepared_manifest_sha256': prepared, 'rollback_evidence_sha256': evidence_sha,
                'activation_allowed': False, 'installation_cutover_performed': False,
                'rollback_completed': False, 'production_activated': False}
        if old is not None and any(old[key] != value for key, value in base.items()
                                   if key != 'rollback_completed'):
            raise ValueError('Rollback checkpoint changed')
        # Precondition: both writers suspended under this operation.  An
        # assignment without its rollback checkpoint is foreign and rejects.
        if old is None:
            if harness_before['events'][-1]['state'] == 'assigned':
                raise ValueError('Harness assignment has no rollback checkpoint')
            suspended_harness(harness_before)
            if knowledge_before['events'][-1]['state'] == 'assigned':
                raise ValueError('Knowledge assignment has no rollback checkpoint')
            suspended_knowledge(knowledge_before)
        else:
            if 'harness' in old['journals'] and old['journals']['harness'] != harness_before:
                raise ValueError('Committed Harness writer revision changed')
            if 'knowledge' in old['journals'] and old['journals']['knowledge'] != knowledge_before:
                raise ValueError('Committed Knowledge writer revision changed')
            if old['state'] == 'manifest_rolled_back':
                # Tolerate the crash window where the Harness journal already
                # carries this operation's rollback assignment.
                if harness_before['events'][-1]['state'] == 'assigned':
                    assigned(harness_before, 'harness', old['rolled_back_manifest_sha256'])
                else:
                    suspended_harness(harness_before)
                if knowledge_before['events'][-1]['state'] == 'assigned':
                    raise ValueError('Knowledge assignment has no rollback checkpoint')
                suspended_knowledge(knowledge_before)
        verify_control()
        # The advance binds the evidence digest, never the evidence content.
        advance = manifests.rollback_installation(manifest_path, rollback_evidence=evidence_path)
        rolled_raw, rolled_back, rolled_document = manifest_facts()
        if advance['manifest_digest'] != 'sha256:' + rolled_back or rolled_document['state'] != 'ROLLED_BACK':
            raise ValueError('Rollback manifest advance changed')
        if rolled_document.get('rollback_evidence_digest') != 'sha256:' + evidence_sha:
            raise ValueError('Rollback manifest commits to different evidence')
        if rolled_copy.exists() or rolled_copy.is_symlink():
            if _read_private(rolled_copy) != rolled_raw:
                raise ValueError('Rolled back manifest copy changed')
        else:
            _replace_private(rolled_copy, rolled_raw)
        if old is not None and old['rolled_back_manifest_sha256'] != rolled_back:
            raise ValueError('Committed rolled back manifest changed')

        def reached(state):
            return old is not None and _ORDER.index(old['state']) >= _ORDER.index(state)

        def commit(state, **extra):
            result = dict(base, state=state, **extra)
            if not reached(state):
                _replace_private(checkpoint_path, authority.encoded(result))
                if _after_checkpoint:
                    _after_checkpoint(state)
            return result

        commit('manifest_rolled_back', journals={}, rolled_back_manifest_sha256=rolled_back)
        verify_control()
        # Both assignments commit to the preserved ROLLED_BACK bytes.
        harness_result = authority.assign(home, rolled_copy, operation_id, 'puddingclaw',
                                          rollback_evidence=evidence_path)
        harness_head = assigned(harness_result, 'harness', rolled_back)
        if old is not None and 'harness' in old['journals'] and old['journals']['harness'] != harness_result:
            raise ValueError('Committed Harness writer revision changed')
        commit('harness_reassigned', journals={'harness': harness_result},
               rolled_back_manifest_sha256=rolled_back)
        verify_control()
        knowledge_result = knowledge_command('assign', '--operation-id', operation_id,
                                             '--manifest', str(rolled_copy),
                                             '--writer', 'puddingclaw',
                                             '--rollback-evidence', str(evidence_path))
        knowledge_head = assigned(knowledge_result, 'knowledge', rolled_back)
        if harness_journal() != harness_result:
            raise ValueError('Harness writer changed during Knowledge assignment')
        if old is not None and 'knowledge' in old['journals'] and old['journals']['knowledge'] != knowledge_result:
            raise ValueError('Committed Knowledge writer revision changed')
        journals = {'harness': harness_result, 'knowledge': knowledge_result}
        # No thaw: rollback returns writer authority to the legacy product, so
        # both new products' freeze markers must still be in place.
        freeze_markers_held(harness_head, knowledge_head)
        result = dict(base, state='both_reassigned', journals=journals,
                      rolled_back_manifest_sha256=rolled_back, rollback_completed=True)
        if reached('both_reassigned') and old != result:
            raise ValueError('Completed rollback changed')
        verify_control()
        if not reached('both_reassigned'):
            _replace_private(checkpoint_path, authority.encoded(result))
            if _after_checkpoint:
                _after_checkpoint('both_reassigned')
        return result


def window_rollback(harness_home, knowledge_state, knowledge_python, checkpoint_dir, manifest,
                    rollback_evidence, operation_id, *, timeout_seconds=120, _after_checkpoint=None):
    authority._operation(operation_id)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError('Invalid timeout')
    home, knowledge, stage, manifest_path, evidence_path = map(
        authority._path, (harness_home, knowledge_state, checkpoint_dir, manifest, rollback_evidence))
    harness_binding = authority.load_binding(home)
    if harness_binding is None:
        raise ValueError('Harness Home must already be enrolled')
    knowledge_binding = inspect_binding(knowledge)
    manifest_directory = authority.identity(manifest_path.parent)
    evidence_directory = authority.identity(evidence_path.parent)
    roots = (home, knowledge, stage, manifest_path.parent, evidence_path.parent,
             Path(harness_binding['authority']['path']),
             Path(knowledge_binding['authority']['path']))
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Window rollback roots must be disjoint')
    python = _validate_executable(knowledge_python)
    executable = _executable_identity(python)
    if not stage.exists():
        stage.mkdir(mode=0o700)
        _sync_directory(stage.parent)
    stage_identity = authority.identity(stage)
    with authority.lock(stage, exclusive=True) as fd:
        for entry in stage.iterdir():
            if entry.name in {'.writer-authority.lock', 'plan.json', 'checkpoint.json',
                              CUTOVER_COPY, ROLLED_BACK_COPY}:
                continue
            if re.fullmatch(r'\.(?:plan|checkpoint|cutover-manifest|rolled-back-manifest)\.json\.tmp-[0-9a-f]{16}', entry.name):
                authority.read(entry)
                continue
            raise ValueError('Unknown window rollback checkpoint entry')
        release_identity = _installed_knowledge_identity(python, stage, fd, timeout_seconds)
        evidence_bytes = _read_private(evidence_path)
        evidence_sha = _sha(evidence_bytes)
        plan = {'format': WINDOW_FORMAT, 'operation_id': operation_id,
                'harness_binding': harness_binding, 'knowledge_binding': knowledge_binding,
                'checkpoint_identity': stage_identity, 'manifest_directory': manifest_directory,
                'manifest_name': manifest_path.name, 'evidence_directory': evidence_directory,
                'rollback_evidence_sha256': evidence_sha, 'knowledge_python': str(python),
                'knowledge_executable': executable, 'knowledge_release_identity': release_identity}
        import os
        lock_identity = os.fstat(fd)

        def verify_control_files():
            current = (stage/'.writer-authority.lock').lstat()
            if (current.st_dev, current.st_ino) != (lock_identity.st_dev, lock_identity.st_ino):
                raise ValueError('Window rollback checkpoint lock changed')
            if authority.identity(stage) != stage_identity or _executable_identity(python) != executable:
                raise ValueError('Window rollback checkpoint control changed')
            if authority.identity(manifest_path.parent) != manifest_directory:
                raise ValueError('Window rollback manifest directory changed')
            if authority.identity(evidence_path.parent) != evidence_directory:
                raise ValueError('Window rollback evidence directory changed')
            if _sha(_read_private(evidence_path)) != evidence_sha:
                raise ValueError('Window rollback evidence changed')
            if authority.load_binding(home) != harness_binding or inspect_binding(knowledge) != knowledge_binding:
                raise ValueError('Writer enrollment changed')

        def verify_control():
            verify_control_files()
            if _installed_knowledge_identity(python, stage, fd, timeout_seconds) != release_identity:
                raise ValueError('Installed Knowledge release changed during window rollback')
            verify_control_files()

        def manifest_facts():
            raw = _read_private(manifest_path)
            document = _json(raw)
            manifests.validate_manifest(document)
            return raw, _sha(raw), document

        def harness_journal():
            return authority.journal(harness_binding)

        def cutover_assigned(journal, side, document):
            # The pre-state is exactly this manifest's registered cutover: the
            # rev2 assigned head (to the new product) whose event digest the
            # CUTOVER manifest registered, under an operation the window
            # rollback must not reuse.
            writer = 'puddingharness' if side == 'harness' else 'puddingknowledge'
            head = manifests._assigned_event(journal, writer)
            if document['checkpoint'].get(side + '_assigned_event_sha256') != 'sha256:' + head['sha256']:
                raise ValueError('Cutover journal is not the registered installation cutover')
            if head['active_installation_revision'] != document.get('active_installation_revision'):
                raise ValueError('Cutover journal commits to a different installation revision')
            if head['operation_id'] == operation_id:
                raise ValueError('Window rollback requires a new operation')
            return head

        def resuspended_harness(journal):
            suspended = _window_suspended_event(journal)
            if suspended['operation_id'] != operation_id:
                raise ValueError('Harness suspension belongs to another operation')
            _record(home, '.installation-freeze-v1.json', suspended['freeze_receipt_sha256'])
            return suspended

        def resuspended_knowledge(journal):
            suspended = _window_suspended_event(journal)
            if suspended['operation_id'] != operation_id:
                raise ValueError('Knowledge suspension belongs to another operation')
            return suspended

        def assigned(journal, side, rolled_back):
            head = _rolled_back_event(journal, side)
            if head['revision'] != 4:
                raise ValueError('Window rollback assignment is not a revision 4')
            if head['operation_id'] != operation_id:
                raise ValueError('Rollback assignment belongs to another operation')
            if head['migration_manifest_sha256'] != rolled_back:
                raise ValueError('Rollback assignment commits to a different manifest')
            if head['rollback_evidence_sha256'] != evidence_sha:
                raise ValueError('Rollback assignment commits to different evidence')
            return head

        def knowledge_command(action, *extra):
            command = [str(python), '-m', 'knowledge_platform.local.writer_authority', action,
                       '--state-dir', str(knowledge), *extra]
            raw = _delegate(command, stage, fd, timeout_seconds)
            verify_control()
            return validate_receipt(raw, knowledge, knowledge_binding)

        def freeze_markers_held(harness_head, knowledge_head):
            knowledge_marker = knowledge / FREEZE_NAME
            for marker in (home / '.installation-freeze-v1.json', knowledge_marker):
                if not marker.exists() or marker.is_symlink():
                    raise ValueError('Freeze marker vanished during rollback reassignment')
            part = knowledge / (FREEZE_NAME + '.part')
            if part.exists() or part.is_symlink():
                raise ValueError('Knowledge freeze publication is incomplete')
            _record(home, '.installation-freeze-v1.json', harness_head['freeze_receipt_sha256'])
            if _sha(_private_bytes(knowledge_marker, 4096)) != knowledge_head['freeze_receipt_sha256']:
                raise ValueError('Knowledge freeze marker changed during rollback reassignment')

        verify_control()
        plan_path, checkpoint_path = stage/'plan.json', stage/'checkpoint.json'
        if plan_path.exists() or plan_path.is_symlink():
            if authority.read(plan_path) != plan:
                raise ValueError('Window rollback plan changed')
        else:
            if checkpoint_path.exists() or checkpoint_path.is_symlink():
                raise ValueError('Window rollback checkpoint has no plan')
            _replace_private(plan_path, authority.encoded(plan))
        # Both actual installations must validate before the first commit.
        harness_before = harness_journal()
        knowledge_before = knowledge_command('status')
        base_keys = {'format', 'operation_id', 'plan_sha256', 'cutover_manifest_sha256',
                     'rollback_evidence_sha256', 'activation_allowed',
                     'installation_cutover_performed', 'rollback_completed', 'production_activated'}
        old = None
        if checkpoint_path.exists() or checkpoint_path.is_symlink():
            old = authority.read(checkpoint_path)
            if old.get('state') not in _WINDOW_ORDER:
                raise ValueError('Invalid window rollback checkpoint state')
            index = _WINDOW_ORDER.index(old['state'])
            extras = {'journals'}
            if index >= _WINDOW_ORDER.index('manifest_rolled_back'):
                extras.add('rolled_back_manifest_sha256')
            if set(old) != base_keys | {'state'} | extras:
                raise ValueError('Invalid window rollback checkpoint')
            if not isinstance(old['journals'], dict) or set(old['journals']) != _WINDOW_JOURNALS[old['state']]:
                raise ValueError('Invalid window rollback checkpoint journals')
            if old['rollback_completed'] is not (old['state'] == 'both_reassigned'):
                raise ValueError('Invalid window rollback checkpoint flags')
            for key in {'cutover_manifest_sha256', 'rollback_evidence_sha256'} | (
                    {'rolled_back_manifest_sha256'} if 'rolled_back_manifest_sha256' in extras else set()):
                if not isinstance(old[key], str) or _HEX64.fullmatch(old[key]) is None:
                    raise ValueError('Invalid window rollback checkpoint digest')
            if old['rollback_evidence_sha256'] != evidence_sha:
                raise ValueError('Window rollback checkpoint commits to different evidence')
        cutover_copy = stage / CUTOVER_COPY
        rolled_copy = stage / ROLLED_BACK_COPY
        raw, current, document = manifest_facts()
        if old is None:
            if document['state'] != 'CUTOVER':
                raise ValueError('Window rollback requires a CUTOVER installation manifest')
            if rolled_copy.exists() or rolled_copy.is_symlink():
                raise ValueError('Rolled back manifest copy has no rolled back manifest')
            cutover = current
            if cutover_copy.exists() or cutover_copy.is_symlink():
                if _read_private(cutover_copy) != raw:
                    raise ValueError('Cutover manifest copy changed')
            else:
                _replace_private(cutover_copy, raw)
        else:
            cutover = old['cutover_manifest_sha256']
            if _sha(_read_private(cutover_copy)) != cutover:
                raise ValueError('Cutover manifest copy changed')
            if old['state'] == 'harness_resuspended':
                if document['state'] != 'CUTOVER' or current != cutover:
                    raise ValueError('Cutover installation manifest changed')
            elif old['state'] == 'both_resuspended':
                # A crash between the manifest advance and its checkpoint
                # leaves a ROLLED_BACK manifest; the preserved CUTOVER copy pins
                # the pre-state and the advance retry below binds it exactly.
                if document['state'] == 'CUTOVER' and current != cutover:
                    raise ValueError('Cutover installation manifest changed')
                if document['state'] not in ('CUTOVER', 'ROLLED_BACK'):
                    raise ValueError('Cutover installation manifest changed')
                if (document['state'] == 'ROLLED_BACK'
                        and document.get('rollback_evidence_digest') != 'sha256:' + evidence_sha):
                    raise ValueError('Rolled back manifest commits to different evidence')
            elif document['state'] != 'ROLLED_BACK' or current != old['rolled_back_manifest_sha256']:
                raise ValueError('Rolled back installation manifest changed')
        base = {'format': WINDOW_FORMAT, 'operation_id': operation_id, 'plan_sha256': authority.digest(plan),
                'cutover_manifest_sha256': cutover, 'rollback_evidence_sha256': evidence_sha,
                'activation_allowed': False, 'installation_cutover_performed': False,
                'rollback_completed': False, 'production_activated': False}
        if old is not None and any(old[key] != value for key, value in base.items()
                                   if key != 'rollback_completed'):
            raise ValueError('Window rollback checkpoint changed')
        # Precondition: each journal carries either this manifest's registered
        # cutover rev2 (fresh) or this operation's rev3 resuspension (crash
        # window).  A rev4 assignment without its checkpoint is foreign.
        if old is None:
            if harness_before['events'][-1]['state'] == 'suspended':
                resuspended_harness(harness_before)
            elif harness_before['events'][-1]['revision'] == 2:
                cutover_assigned(harness_before, 'harness', document)
            else:
                raise ValueError('Harness assignment has no window rollback checkpoint')
            if knowledge_before['events'][-1]['state'] == 'suspended':
                resuspended_knowledge(knowledge_before)
            elif knowledge_before['events'][-1]['revision'] == 2:
                cutover_assigned(knowledge_before, 'knowledge', document)
            else:
                raise ValueError('Knowledge assignment has no window rollback checkpoint')
        else:
            # One committed step ahead of its checkpoint is a legitimate crash
            # window; any other drift against a recorded journal rejects.
            ahead = {'manifest_rolled_back': 'harness', 'harness_reassigned': 'knowledge'}.get(old['state'])
            for side, live in (('harness', harness_before), ('knowledge', knowledge_before)):
                if old['journals'].get(side) is not None and old['journals'][side] != live and ahead != side:
                    raise ValueError('Committed ' + side.capitalize() + ' writer revision changed')
            if old['state'] == 'harness_resuspended':
                resuspended_harness(harness_before)
                if knowledge_before['events'][-1]['state'] == 'suspended':
                    resuspended_knowledge(knowledge_before)
                elif knowledge_before['events'][-1]['revision'] == 2:
                    cutover_assigned(knowledge_before, 'knowledge', document)
                else:
                    raise ValueError('Knowledge assignment has no window rollback checkpoint')
            elif old['state'] == 'both_resuspended':
                resuspended_harness(harness_before)
                resuspended_knowledge(knowledge_before)
            elif old['state'] == 'manifest_rolled_back':
                if harness_before['events'][-1]['state'] == 'assigned':
                    assigned(harness_before, 'harness', old['rolled_back_manifest_sha256'])
                else:
                    resuspended_harness(harness_before)
                if knowledge_before['events'][-1]['state'] == 'assigned':
                    raise ValueError('Knowledge assignment has no window rollback checkpoint')
                resuspended_knowledge(knowledge_before)
            elif old['state'] == 'harness_reassigned':
                assigned(harness_before, 'harness', old['rolled_back_manifest_sha256'])
                if knowledge_before['events'][-1]['state'] == 'assigned':
                    assigned(knowledge_before, 'knowledge', old['rolled_back_manifest_sha256'])
                else:
                    resuspended_knowledge(knowledge_before)
            else:
                assigned(harness_before, 'harness', old['rolled_back_manifest_sha256'])
                assigned(knowledge_before, 'knowledge', old['rolled_back_manifest_sha256'])
        verify_control()

        def reached(state):
            return old is not None and _WINDOW_ORDER.index(old['state']) >= _WINDOW_ORDER.index(state)

        def commit(state, **extra):
            result = dict(base, state=state, **extra)
            if not reached(state):
                _replace_private(checkpoint_path, authority.encoded(result))
                if _after_checkpoint:
                    _after_checkpoint(state)
            return result

        # The fence/freeze step: both writers are re-suspended under the new
        # operation before any reverse migration runs.  Each suspension
        # re-applies that product's freeze marker; once a rev4 assignment
        # exists (crash windows only) the suspension is never re-appended.
        harness_rev4 = (harness_before['events'][-1]['state'] == 'assigned'
                        and harness_before['events'][-1]['revision'] == 4)
        if harness_rev4:
            harness_resuspended = harness_before
        else:
            harness_resuspended = authority.suspend(home, operation_id)
        harness_suspended = resuspended_harness(harness_resuspended)
        commit('harness_resuspended', journals={'harness': harness_resuspended})
        verify_control()
        knowledge_rev4 = (knowledge_before['events'][-1]['state'] == 'assigned'
                          and knowledge_before['events'][-1]['revision'] == 4)
        if knowledge_rev4:
            knowledge_resuspended = knowledge_before
        else:
            knowledge_resuspended = knowledge_command('suspend', '--operation-id', operation_id)
        knowledge_suspended = resuspended_knowledge(knowledge_resuspended)
        if harness_journal() != harness_resuspended:
            raise ValueError('Harness writer changed during Knowledge resuspension')
        suspended_journals = {'harness': harness_resuspended, 'knowledge': knowledge_resuspended}
        commit('both_resuspended', journals=suspended_journals)
        verify_control()
        # Both rev3 freeze receipts must match the re-applied markers before
        # the manifest advance; the same check closes the completed rollback.
        freeze_markers_held(harness_suspended, knowledge_suspended)
        # The advance binds the evidence digest, never the evidence content.
        advance = manifests.rollback_installation(manifest_path, rollback_evidence=evidence_path)
        rolled_raw, rolled_back, rolled_document = manifest_facts()
        if advance['manifest_digest'] != 'sha256:' + rolled_back or rolled_document['state'] != 'ROLLED_BACK':
            raise ValueError('Rollback manifest advance changed')
        if rolled_document.get('rollback_evidence_digest') != 'sha256:' + evidence_sha:
            raise ValueError('Rollback manifest commits to different evidence')
        if any(rolled_document['active_writers'][domain] != 'puddingclaw' for domain in manifests._DOMAINS):
            raise ValueError('Rollback manifest writers changed')
        if rolled_copy.exists() or rolled_copy.is_symlink():
            if _read_private(rolled_copy) != rolled_raw:
                raise ValueError('Rolled back manifest copy changed')
        else:
            _replace_private(rolled_copy, rolled_raw)
        if (old is not None and 'rolled_back_manifest_sha256' in old
                and old['rolled_back_manifest_sha256'] != rolled_back):
            raise ValueError('Committed rolled back manifest changed')
        commit('manifest_rolled_back', journals=suspended_journals,
               rolled_back_manifest_sha256=rolled_back)
        verify_control()
        # Both rev4 assignments commit to the preserved ROLLED_BACK bytes.
        harness_result = authority.assign(home, rolled_copy, operation_id, 'puddingclaw',
                                          rollback_evidence=evidence_path)
        harness_head = assigned(harness_result, 'harness', rolled_back)
        if old is not None and reached('harness_reassigned') and old['journals']['harness'] != harness_result:
            raise ValueError('Committed Harness writer revision changed')
        commit('harness_reassigned', journals={'harness': harness_result,
                                               'knowledge': knowledge_resuspended},
               rolled_back_manifest_sha256=rolled_back)
        verify_control()
        knowledge_result = knowledge_command('assign', '--operation-id', operation_id,
                                             '--manifest', str(rolled_copy),
                                             '--writer', 'puddingclaw',
                                             '--rollback-evidence', str(evidence_path))
        knowledge_head = assigned(knowledge_result, 'knowledge', rolled_back)
        if harness_journal() != harness_result:
            raise ValueError('Harness writer changed during Knowledge assignment')
        if old is not None and reached('both_reassigned') and old['journals']['knowledge'] != knowledge_result:
            raise ValueError('Committed Knowledge writer revision changed')
        journals = {'harness': harness_result, 'knowledge': knowledge_result}
        # No thaw: rollback returns writer authority to the legacy product, so
        # both new products' freeze markers must still be in place.
        freeze_markers_held(harness_head, knowledge_head)
        result = dict(base, state='both_reassigned', journals=journals,
                      rolled_back_manifest_sha256=rolled_back, rollback_completed=True)
        if reached('both_reassigned') and old != result:
            raise ValueError('Completed window rollback changed')
        verify_control()
        if not reached('both_reassigned'):
            _replace_private(checkpoint_path, authority.encoded(result))
            if _after_checkpoint:
                _after_checkpoint('both_reassigned')
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('rollback', help='Reverse-direction rollback reassigning both product writers to puddingclaw')
    for name in ('harness-home', 'knowledge-state', 'knowledge-python', 'checkpoint-dir',
                 'manifest', 'rollback-evidence', 'operation-id'):
        run.add_argument('--'+name, required=True)
    run.add_argument('--timeout-seconds', type=int, default=120)
    window = commands.add_parser('window-rollback', help='Post-cutover window rollback reassigning both product writers to puddingclaw')
    for name in ('harness-home', 'knowledge-state', 'knowledge-python', 'checkpoint-dir',
                 'manifest', 'rollback-evidence', 'operation-id'):
        window.add_argument('--'+name, required=True)
    window.add_argument('--timeout-seconds', type=int, default=120)
    args = parser.parse_args(argv)
    try:
        if args.command == 'rollback':
            result = rollback(args.harness_home, args.knowledge_state, args.knowledge_python,
                              args.checkpoint_dir, args.manifest, args.rollback_evidence,
                              args.operation_id, timeout_seconds=args.timeout_seconds)
        else:
            result = window_rollback(args.harness_home, args.knowledge_state, args.knowledge_python,
                                     args.checkpoint_dir, args.manifest, args.rollback_evidence,
                                     args.operation_id, timeout_seconds=args.timeout_seconds)
    except Exception:
        print(json.dumps({'format': FORMAT if args.command == 'rollback' else WINDOW_FORMAT,
                          'status': 'error',
                          'error_code': 'installation_rollback_rejected', 'activation_allowed': False,
                          'installation_cutover_performed': False, 'rollback_completed': False,
                          'production_activated': False}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
