"""Build an inactive legacy Home candidate preserving post-cutover session files.

Both enrolled targets are suspended before capture. Other source domains remain
byte-identical; their reverse migrations and credentials are separate gates.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
import stat
from pathlib import Path

from harness import installation_authority as authority
from harness.source_snapshot import VerifiedSourceSnapshot, _inventory, _read, _encoded, _private
from harness.session_import import inventory, read_file
from harness.session_reverse_plan import plan_session_reverse
from harness.installation_guard import InstallationGuard
from harness.writer_barrier import suspend_writers
from harness.knowledge_writer_receipt import validate_receipt
from harness.migration_orchestrator import _replace_private
from harness.home_freeze import _sync_directory
from harness.target_freeze import _record

FORMAT = 'puddingharness-session-reverse-candidate/v1'


def _mkdir(path):
    if path.exists():
        _private(path.lstat(), directory=True)
        return
    _mkdir(path.parent)
    path.mkdir(mode=0o700)
    _sync_directory(path.parent)


def _copy(source, target, fact):
    _mkdir(target.parent)
    part = Path(str(target)+'.reverse-part')
    fd = os.open(source, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('Reverse input must be owned, regular and unlinked')
        out = os.open(part, os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW|os.O_NONBLOCK, 0o600)
        try:
            _private(os.fstat(out)); sha = hashlib.sha256(); size = 0
            while chunk := os.read(fd, 1024*1024):
                size += len(chunk)
                if size > fact['size']:
                    raise ValueError('Reverse input size changed')
                sha.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(out, view)
                    if written <= 0: raise OSError('Short reverse candidate write')
                    view = view[written:]
            if size != fact['size'] or sha.hexdigest() != fact['sha256']:
                raise ValueError('Reverse input digest changed')
            os.fsync(out)
        finally: os.close(out)
    finally: os.close(fd)
    os.replace(part, target); _sync_directory(target.parent)


def _check_stage(stage, expected):
    allowed_files = {'manifest.json','.writer-authority.lock'}
    allowed_files.update('payload/'+p for p in expected['files'])
    allowed_files.update('payload/'+p+'.reverse-part' for p in expected['files'])
    allowed_dirs = {'payload'} | {'payload/'+p for p in expected['directories']}
    import re
    for parent, dirs, names in os.walk(stage, followlinks=False):
        for name in dirs:
            p = Path(parent)/name; _private(p.lstat(), directory=True)
            if p.relative_to(stage).as_posix() not in allowed_dirs:
                raise ValueError('Unknown reverse candidate directory')
        for name in names:
            p = Path(parent)/name; _private(p.lstat())
            rel = p.relative_to(stage).as_posix()
            if rel not in allowed_files and not (parent == str(stage) and re.fullmatch(r'\.manifest.json\.tmp-[0-9a-f]{16}', name)):
                raise ValueError('Unknown reverse candidate file')


def prepare_session_reverse(source_snapshot, target_before, harness_home, knowledge_state,
                            knowledge_python, checkpoint_dir, operation_id, staging,
                            *, include_generic_settings=False, _after_copy=None):
    paths = list(map(authority._path, (source_snapshot,target_before,harness_home,knowledge_state,checkpoint_dir,staging)))
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(paths) for b in paths[i+1:]):
        raise ValueError('Reverse migration roots must be disjoint')
    source, before, home, knowledge, barrier_dir, stage = paths
    if type(include_generic_settings) is not bool: raise ValueError('Invalid settings option')
    authority.identity(before)
    with ExitStack() as stack:
        snapshot = stack.enter_context(VerifiedSourceSnapshot(source))
        source_inventory = _inventory(snapshot.payload)
        baseline = inventory(before, require_sessions=False)
        settings_inputs = None
        if include_generic_settings:
            from harness.settings_reverse import reverse_generic_settings
            settings_inputs = [_read_settings(snapshot.payload/'config.json'),_read_settings(before/'config.json')]
            reverse_generic_settings(*settings_inputs,settings_inputs[1])
        # Reject a conflicting baseline before suspending any writer.
        plan_session_reverse(source_inventory, baseline, baseline)
        barrier = suspend_writers(home, knowledge, knowledge_python, barrier_dir, operation_id)
        barrier_plan = authority.read(barrier_dir/'plan.json')
        authority_roots = [Path(barrier_plan[k]['authority']['path']) for k in ('harness_binding','knowledge_binding')]
        if any(stage == p or stage.is_relative_to(p) or p.is_relative_to(stage) for p in authority_roots):
            raise ValueError('Reverse stage overlaps writer authority')
        guard = stack.enter_context(InstallationGuard(home, exclusive=True, allow_frozen=True))
        stack.enter_context(authority.lock(authority_roots[0], exclusive=False))

        def verify_inputs():
            snapshot.verify(); guard.verify()
            if authority.read(barrier_dir/'checkpoint.json') != barrier or authority.read(barrier_dir/'plan.json') != barrier_plan:
                raise ValueError('Writer barrier changed')
            binding = authority.load_binding(home)
            if binding != barrier_plan['harness_binding'] or authority.journal(binding) != barrier['journals']['harness']:
                raise ValueError('Harness writer revocation changed')
            event = barrier['journals']['harness']['events'][-1]
            _record(home,'.installation-freeze-v1.json',event['freeze_receipt_sha256'])
            raw = json.dumps({'format':'puddingknowledge-writer-authority/v1','status':'ok',
                              'journal':barrier['journals']['knowledge'],'installation_cutover_performed':False}).encode()
            validate_receipt(raw,knowledge,barrier_plan['knowledge_binding'],operation_id)
            if settings_inputs is not None:
                if [_read_settings(snapshot.payload/'config.json'),_read_settings(before/'config.json')] != settings_inputs:
                    raise ValueError('Settings baseline changed')
            if inventory(before,require_sessions=False) != baseline:
                raise ValueError('Session baseline changed')

        verify_inputs()
        after = inventory(home,require_sessions=False)
        planned = plan_session_reverse(source_inventory,baseline,after)
        expected = planned['inventory']
        settings_payload = settings_receipt = target_settings = None
        if settings_inputs is not None:
            target_settings = _read_settings(home/'config.json')
            settings_payload,settings_receipt = reverse_generic_settings(*settings_inputs,target_settings)
            old_size = expected['files']['config.json']['size']
            expected['files']['config.json'] = {'sha256':settings_receipt['payload_sha256'],'size':len(settings_payload)}
            expected['total_bytes'] += len(settings_payload)-old_size
            from harness.source_snapshot import _validate_inventory
            _validate_inventory(expected)
        if not stage.exists(): stage.mkdir(mode=0o700); _sync_directory(stage.parent)
        stage_identity = authority.identity(stage)
        stack.enter_context(authority.lock(stage,exclusive=True))
        plan = {'format':FORMAT,'source_snapshot':snapshot.commitment,
                'baseline_identity':authority.identity(before),'baseline':baseline,
                'harness_identity':authority.identity(home),'after':after,
                'barrier_sha256':authority.digest(barrier),'stage_identity':stage_identity,
                'inventory':expected,'changes':planned['changes']}
        if settings_receipt is not None: plan['generic_settings'] = settings_receipt
        manifest = {'format':FORMAT,'plan':plan,'plan_sha256':hashlib.sha256(_encoded(plan)).hexdigest(),
                    'state':'copying','activation_allowed':False,'rollback_completed':False,
                    'other_domains_reversed':False,'credential_continuity_verified':False}
        _check_stage(stage,expected)
        complete = False
        if (stage/'manifest.json').exists():
            previous,raw = _read(stage/'manifest.json'); complete = previous.get('state') == 'verified_inactive'
            if raw != _encoded(dict(manifest,state='verified_inactive' if complete else 'copying')):
                raise ValueError('Reverse candidate plan changed')
        else:
            if any(p.name != '.writer-authority.lock' for p in stage.iterdir()):
                raise ValueError('Unowned reverse candidate stage')
            _replace_private(stage/'manifest.json',_encoded(manifest))
        _mkdir(stage/'payload')
        for directory in sorted(expected['directories'],key=lambda p:(p.count('/'),p)):
            _mkdir(stage/'payload'/directory)
        for name,fact in expected['files'].items():
            destination = stage/'payload'/name
            if destination.exists():
                fd=os.open(destination,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                with os.fdopen(fd,'rb') as stream:
                    _private(os.fstat(stream.fileno()))
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=fact['sha256']:
                        raise ValueError('Reverse candidate file changed')
            else:
                if complete: raise ValueError('Completed reverse candidate file missing')
                if name == 'config.json' and settings_payload is not None:
                    _write_generated(destination,settings_payload)
                else:
                    _copy((home if name in after else snapshot.payload)/name,destination,fact)
                if _after_copy: _after_copy(name)
        verify_inputs()
        if target_settings is not None and _read_settings(home/'config.json') != target_settings:
            raise ValueError('Target settings changed during capture')
        if authority.identity(stage)!=stage_identity or inventory(home,require_sessions=False)!=after:
            raise ValueError('Reverse capture changed')
        _check_stage(stage,expected)
        if _inventory(stage/'payload') != expected:
            raise ValueError('Reverse candidate content differs from plan')
        manifest['state']='verified_inactive'
        _replace_private(stage/'manifest.json',_encoded(manifest))
        result = {'format':FORMAT,'state':'verified_inactive','plan_sha256':manifest['plan_sha256'],
                'changes':planned['changes'],'file_count':len(expected['files']),
                'idempotent':complete,'activation_allowed':False,'rollback_completed':False,
                'both_target_writers_suspended':True,'other_domains_reversed':False,
                'credential_continuity_verified':False}
        if settings_receipt is not None:
            result['generic_settings_reversed'] = True
            result['changed_settings_sections'] = settings_receipt['changed_sections']
        return result


def _read_settings(path):
    from harness.settings_import import MAX_CONFIG_BYTES
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_size>MAX_CONFIG_BYTES:
        raise ValueError('Settings must be bounded owned regular unlinked files')
    return read_file(path)


def _write_generated(target,data):
    part=Path(str(target)+'.reverse-part')
    fd=os.open(part,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        _private(os.fstat(fd));view=memoryview(data)
        while view:
            size=os.write(fd,view)
            if size<=0:raise OSError('Short generated settings write')
            view=view[size:]
        os.fsync(fd)
    finally:os.close(fd)
    os.replace(part,target);_sync_directory(target.parent)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source-snapshot','target-before','harness-home','knowledge-state','knowledge-python','checkpoint-dir','operation-id','staging'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--include-generic-settings',action='store_true')
    args=parser.parse_args(argv)
    try: result=prepare_session_reverse(**vars(args))
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','activation_allowed':False,'error_code':'session_reverse_rejected'}));return 1
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
