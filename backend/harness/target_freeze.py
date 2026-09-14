"""Durable two-product cooperative freeze barrier, never CUTOVER authority.

Harness owns this checkpoint and its Home. An explicitly selected independent
Knowledge interpreter owns workspace validation and freezing. No Knowledge
source is imported. Partial completion remains frozen and is retried explicitly.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from harness.home_freeze import freeze_home, _sync_directory
from harness.migration_orchestrator import (
    _path, _read_private, _replace_private, _json, _delegate, _validate_executable,
)

FORMAT = 'puddingharness-target-freeze-barrier/v1'
KNOWLEDGE_FORMAT = 'puddingknowledge-workspace-freeze/v1'


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)+'\n').encode()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _identity(root):
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Barrier roots must be existing private owned directories')
    return {'path_sha256': _hash(str(root).encode()), 'device': info.st_dev, 'inode': info.st_ino}


def _record(root, name, expected):
    info = (root/name).lstat()
    if info.st_uid != os.getuid(): raise ValueError('Freeze record owner changed')
    raw = _read_private(root/name, 4096)
    if _hash(raw) != expected: raise ValueError('Freeze receipt no longer matches persisted record')
    if (root/(name+'.part')).exists() or (root/(name+'.part')).is_symlink():
        raise ValueError('Freeze publication is incomplete')
    return raw


def _knowledge_receipt(raw, root, operation):
    value = _json(raw)
    keys = {'format','operation_id','workspace_manifest_sha256','root_path_sha256','state','status','receipt_sha256','activation_allowed','directory_identity'}
    if set(value) != keys or value['format'] != KNOWLEDGE_FORMAT or value['state'] != 'workspace_frozen' or value['status'] != 'workspace_frozen':
        raise ValueError('Unsupported Knowledge freeze receipt')
    if value['operation_id'] != operation or value['root_path_sha256'] != _hash(str(root).encode()):
        raise ValueError('Knowledge freeze receipt target mismatch')
    identity = root.stat()
    if value['activation_allowed'] is not False or value['directory_identity'] != {'device':identity.st_dev,'inode':identity.st_ino}:
        raise ValueError('Knowledge freeze authority or directory changed')
    manifest = _read_private(root/'workspace.json')
    if value['workspace_manifest_sha256'] != _hash(manifest):
        raise ValueError('Knowledge workspace manifest changed')
    commitment = {key: item for key, item in value.items() if key not in {'status','receipt_sha256'}}
    if _hash(_encoded(commitment)) != value['receipt_sha256']:
        raise ValueError('Knowledge freeze receipt digest mismatch')
    _record(root, '.workspace-freeze-v1.json', value['receipt_sha256'])
    return value


def _executable_identity(python):
    resolved = python.resolve(strict=True)
    with resolved.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > 128*1024*1024:
            raise ValueError('Knowledge executable must be bounded and regular')
        digest = hashlib.file_digest(stream,'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    current = python.stat()
    signature = lambda info: (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_uid,info.st_mode)
    if signature(before) != signature(after) or signature(after) != signature(current):
        raise ValueError('Knowledge executable changed during inspection')
    return {'resolved':str(resolved),'device':after.st_dev,'inode':after.st_ino,
            'owner':after.st_uid,'mode':after.st_mode,'sha256':digest}


def freeze_targets(harness_home, knowledge_state, knowledge_python, checkpoint_dir, operation_id,
                   *, timeout_seconds=120, _after_checkpoint=None):
    if not isinstance(operation_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}',operation_id):
        raise ValueError('Invalid operation ID')
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3600:
        raise ValueError('Invalid timeout')
    home, knowledge, stage = map(_path, (harness_home,knowledge_state,checkpoint_dir))
    roots = (home,knowledge,stage)
    if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(roots) for b in roots[i+1:]):
        raise ValueError('Barrier roots must be disjoint')
    identities = {'harness':_identity(home),'knowledge':_identity(knowledge)}
    python = _validate_executable(knowledge_python)
    if not stage.exists():
        stage.mkdir(mode=0o700)
        _sync_directory(stage.parent)
    stage_identity = _identity(stage)
    lock = stage/'.target-freeze.lock'
    fd = os.open(lock,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError('Invalid barrier lock')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        current = lock.lstat()
        if (info.st_dev,info.st_ino) != (current.st_dev,current.st_ino): raise ValueError('Barrier lock changed')
        for entry in stage.iterdir():
            if entry.name in {lock.name,'plan.json','checkpoint.json'}: continue
            if re.fullmatch(r'\.(?:plan|checkpoint)\.json\.tmp-[0-9a-f]{16}',entry.name):
                _read_private(entry); continue  # interrupted atomic replacement; never interpreted as committed
            raise ValueError('Unknown barrier checkpoint entry')
        plan = {'format':FORMAT,'operation_id':operation_id,'targets':identities,'checkpoint_identity':stage_identity,
                'knowledge_python':str(python),'knowledge_executable':_executable_identity(python),'knowledge_protocol':KNOWLEDGE_FORMAT}
        def verify_control():
            current = lock.lstat()
            if _identity(stage) != stage_identity or (current.st_dev,current.st_ino) != (info.st_dev,info.st_ino):
                raise ValueError('Barrier checkpoint directory or lock changed')
            if _executable_identity(python) != plan['knowledge_executable']:
                raise ValueError('Knowledge executable changed')
        plan_bytes = _encoded(plan)
        verify_control()
        plan_path = stage/'plan.json'; checkpoint = stage/'checkpoint.json'
        if plan_path.exists() or plan_path.is_symlink():
            if _read_private(plan_path) != plan_bytes: raise ValueError('Barrier plan changed')
        else:
            if checkpoint.exists() or checkpoint.is_symlink(): raise ValueError('Checkpoint has no plan')
            _replace_private(plan_path,plan_bytes)
        receipts = {}
        base = {'format':FORMAT,'operation_id':operation_id,'plan_sha256':_hash(plan_bytes),
                'activation_allowed':False,'installation_cutover_performed':False,'rollback_completed':False,
                'external_writers_fenced':False}
        old = None
        if checkpoint.exists() or checkpoint.is_symlink():
            old = _json(_read_private(checkpoint))
            if set(old) != set(base)|{'state','receipts'} or any(old[k] != v for k,v in base.items()):
                raise ValueError('Invalid barrier checkpoint')
            expected = {'harness'} if old['state']=='harness_frozen' else {'harness','knowledge'} if old['state']=='both_targets_frozen' else None
            if expected is None or not isinstance(old['receipts'],dict) or set(old['receipts']) != expected:
                raise ValueError('Invalid barrier checkpoint state')
            receipts = old['receipts']
            _record(home,'.installation-freeze-v1.json',receipts['harness']['receipt_sha256'])
            if 'knowledge' in receipts:
                _knowledge_receipt(_encoded(receipts['knowledge']),knowledge,operation_id)
        harness_receipt = freeze_home(home,operation_id)
        if 'harness' in receipts and receipts['harness'] != harness_receipt:
            raise ValueError('Harness receipt changed')
        receipts = dict(receipts,harness=harness_receipt)
        if old is None:
            _replace_private(checkpoint,_encoded(dict(base,state='harness_frozen',receipts=receipts)))
            if _after_checkpoint: _after_checkpoint('harness_frozen')
        verify_control()
        raw = _delegate([str(python),'-m','knowledge_platform.local.workspace_freeze',
                         '--state-dir',str(knowledge),'--operation-id',operation_id],stage,fd,timeout_seconds)
        verify_control()
        knowledge_receipt = _knowledge_receipt(raw,knowledge,operation_id)
        if 'knowledge' in receipts and receipts['knowledge'] != knowledge_receipt:
            raise ValueError('Knowledge receipt changed')
        receipts['knowledge'] = knowledge_receipt
        if identities != {'harness':_identity(home),'knowledge':_identity(knowledge)}:
            raise ValueError('Target root changed')
        _record(home,'.installation-freeze-v1.json',harness_receipt['receipt_sha256'])
        verify_control()
        result = dict(base,state='both_targets_frozen',receipts=receipts)
        _replace_private(checkpoint,_encoded(result))
        if _after_checkpoint: _after_checkpoint('both_targets_frozen')
        return result
    finally:
        os.close(fd)  # delegated child retains admission after parent SIGKILL


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('harness-home','knowledge-state','knowledge-python','checkpoint-dir','operation-id'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    try: result=freeze_targets(**vars(args))
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','activation_allowed':False,'error_code':'target_freeze_rejected'}))
        return 1
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
