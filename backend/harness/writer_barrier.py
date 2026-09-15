"""Suspend both independent product writers before installation reverse migration.

This is a durable revocation barrier, not permission to activate or thaw a writer.
Knowledge commands execute through an explicitly selected independent interpreter.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import json
import re

from harness import installation_authority as authority
from harness.home_freeze import _sync_directory
from harness.migration_orchestrator import _delegate, _replace_private, _validate_executable, _installed_knowledge_identity
from harness.target_freeze import _executable_identity, _record
from harness.knowledge_writer_receipt import inspect_binding, validate_receipt

FORMAT = 'puddingharness-writer-suspension-barrier/v1'


def suspend_writers(harness_home, knowledge_state, knowledge_python, checkpoint_dir,
                    operation_id, *, timeout_seconds=120, _after_checkpoint=None):
    authority._operation(operation_id)
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError('Invalid timeout')
    home, knowledge, stage = map(authority._path, (harness_home, knowledge_state, checkpoint_dir))
    harness_binding = authority.load_binding(home)
    if harness_binding is None:
        raise ValueError('Harness Home must already be enrolled')
    knowledge_binding = inspect_binding(knowledge)
    roots = (home, knowledge, stage, Path(harness_binding['authority']['path']),
             Path(knowledge_binding['authority']['path']))
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Writer barrier roots must be disjoint')
    python = _validate_executable(knowledge_python)
    executable = _executable_identity(python)
    if not stage.exists():
        stage.mkdir(mode=0o700)
        _sync_directory(stage.parent)
    stage_identity = authority.identity(stage)
    with authority.lock(stage, exclusive=True) as fd:
        for entry in stage.iterdir():
            if entry.name in {'.writer-authority.lock', 'plan.json', 'checkpoint.json'}:
                continue
            if re.fullmatch(r'\.(?:plan|checkpoint)\.json\.tmp-[0-9a-f]{16}', entry.name):
                authority.read(entry)
                continue
            raise ValueError('Unknown writer barrier entry')
        release_identity = _installed_knowledge_identity(python, stage, fd, timeout_seconds)
        plan = {'format': FORMAT, 'operation_id': operation_id,
                'harness_binding': harness_binding, 'knowledge_binding': knowledge_binding,
                'checkpoint_identity': stage_identity, 'knowledge_python': str(python),
                'knowledge_executable': executable, 'knowledge_release_identity': release_identity}
        import os
        lock_identity = os.fstat(fd)

        def verify_control_files():
            current = (stage/'.writer-authority.lock').lstat()
            if (current.st_dev, current.st_ino) != (lock_identity.st_dev, lock_identity.st_ino):
                raise ValueError('Writer barrier lock changed')
            if authority.identity(stage) != stage_identity or _executable_identity(python) != executable:
                raise ValueError('Writer barrier control changed')
            if authority.load_binding(home) != harness_binding or inspect_binding(knowledge) != knowledge_binding:
                raise ValueError('Writer enrollment changed')

        def verify_control():
            verify_control_files()
            if _installed_knowledge_identity(python, stage, fd, timeout_seconds) != release_identity:
                raise ValueError('Installed Knowledge release changed during writer suspension')
            verify_control_files()

        def harness_status():
            result = authority.journal(harness_binding)
            last = result['events'][-1]
            if last['state'] == 'suspended':
                if last['operation_id'] != operation_id:
                    raise ValueError('Harness suspension belongs to another operation')
                _record(home, '.installation-freeze-v1.json', last['freeze_receipt_sha256'])
            return result

        def knowledge_command(action):
            command = [str(python), '-m', 'knowledge_platform.local.writer_authority', action,
                       '--state-dir', str(knowledge)]
            if action == 'suspend':
                command += ['--operation-id', operation_id]
            raw = _delegate(command, stage, fd, timeout_seconds)
            verify_control()
            result = validate_receipt(raw, knowledge, knowledge_binding,
                                      operation_id if action == 'suspend' else None)
            if len(result['events']) == 2 and result['events'][-1]['operation_id'] != operation_id:
                raise ValueError('Knowledge suspension belongs to another operation')
            return result

        verify_control()
        plan_path, checkpoint_path = stage/'plan.json', stage/'checkpoint.json'
        if plan_path.exists() or plan_path.is_symlink():
            if authority.read(plan_path) != plan:
                raise ValueError('Writer barrier plan changed')
        else:
            if checkpoint_path.exists() or checkpoint_path.is_symlink():
                raise ValueError('Writer checkpoint has no plan')
            _replace_private(plan_path, authority.encoded(plan))
        base = {'format': FORMAT, 'operation_id': operation_id, 'plan_sha256': authority.digest(plan),
                'activation_allowed': False, 'installation_cutover_performed': False,
                'rollback_completed': False, 'external_writers_fenced': False}
        old = None
        if checkpoint_path.exists() or checkpoint_path.is_symlink():
            old = authority.read(checkpoint_path)
            if set(old) != set(base) | {'state', 'journals'} or any(old[k] != v for k, v in base.items()):
                raise ValueError('Invalid writer checkpoint')
            expected = {'harness'} if old['state'] == 'harness_suspended' else {'harness', 'knowledge'} if old['state'] == 'both_writers_suspended' else None
            if expected is None or not isinstance(old['journals'], dict) or set(old['journals']) != expected:
                raise ValueError('Invalid writer checkpoint state')
        # Both actual installations must validate before the first destructive step.
        harness_before = harness_status()
        knowledge_before = knowledge_command('status')
        if old is not None:
            if old['journals']['harness'] != harness_before:
                raise ValueError('Committed Harness writer revision changed')
            if 'knowledge' in old['journals'] and old['journals']['knowledge'] != knowledge_before:
                raise ValueError('Committed Knowledge writer revision changed')
            for journal in old['journals'].values():
                if len(journal['events']) != 2 or journal['events'][-1]['state'] != 'suspended':
                    raise ValueError('Checkpoint contains unsuspended writer')
        verify_control()
        harness_result = authority.suspend(home, operation_id)
        if harness_result != harness_status():
            raise ValueError('Harness revision changed after suspension')
        if old is None:
            partial = dict(base, state='harness_suspended', journals={'harness': harness_result})
            _replace_private(checkpoint_path, authority.encoded(partial))
            if _after_checkpoint:
                _after_checkpoint('harness_suspended')
        verify_control()
        knowledge_result = knowledge_command('suspend')
        if harness_status() != harness_result:
            raise ValueError('Harness writer changed during Knowledge suspension')
        result = dict(base, state='both_writers_suspended',
                      journals={'harness': harness_result, 'knowledge': knowledge_result})
        if old is not None and old['state'] == 'both_writers_suspended' and old != result:
            raise ValueError('Completed writer barrier changed')
        verify_control()
        _replace_private(checkpoint_path, authority.encoded(result))
        if _after_checkpoint:
            _after_checkpoint('both_writers_suspended')
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('harness-home', 'knowledge-state', 'knowledge-python', 'checkpoint-dir', 'operation-id'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args(argv)
    try:
        result = suspend_writers(**vars(args))
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'activation_allowed': False,
                          'error_code': 'writer_suspension_rejected'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
