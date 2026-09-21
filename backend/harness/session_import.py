"""Resumable import of offline session-domain files into inactive Harness staging."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

ROOTS=('sessions','data/attachments','data/large-tool-results','data/harness-scratch','data/harness-rewind')
MAX_FILE=256*1024*1024
MAX_TOTAL=2*1024**3
MAX_FILES=10000
FORMAT='puddingharness-session-import/v1'


def digest(data): return 'sha256:'+hashlib.sha256(data).hexdigest()


def encoded(value): return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()


def checked(path):
    path=Path(path).expanduser().absolute()
    if '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Unsafe migration path')
    return path


def read_file(path):
    checked(path)
    info=path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size>MAX_FILE or info.st_nlink!=1:
        raise ValueError('Unsupported or oversized migration file')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream: data=stream.read(MAX_FILE+1)
    if len(data)>MAX_FILE: raise ValueError('Migration file exceeds budget')
    return data


def inventory(source, *, require_sessions=True):
    files={};total=0
    for name in ROOTS:
        root=checked(source/name)
        if not root.exists():continue
        if not root.is_dir():raise ValueError('Session-domain root must be a directory')
        for parent,dirs,names in os.walk(root,followlinks=False):
            for child in dirs: checked(Path(parent)/child)
            for child in names:
                path=checked(Path(parent)/child)
                relative=path.relative_to(source).as_posix()
                if child.endswith('.lock'):
                    if read_file(path):raise ValueError('Unexpected session lock contents')
                    continue
                if child.endswith('.migration-part'):raise ValueError('Reserved migration filename')
                data=read_file(path);total+=len(data)
                if total>MAX_TOTAL or len(files)>=MAX_FILES:raise ValueError('Session migration budget exceeded')
                files[relative]={'digest':digest(data),'size':len(data)}
    if require_sessions and not any(name.startswith('sessions/') for name in files):raise ValueError('No session files found')
    return dict(sorted(files.items()))


def atomic_write(path,data):
    checked(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    part=checked(Path(str(path)+'.migration-part'))
    if part.exists():read_file(part)
    fd=os.open(part,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    os.replace(part,path)
    descriptor=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def _check_stage(stage,files):
    allowed={'manifest.json','.import.lock','manifest.json.migration-part'}
    allowed.update('payload/'+name for name in files)
    allowed.update('payload/'+name+'.migration-part' for name in files)
    allowed_dirs = {str(parent) for name in allowed for parent in Path(name).parents if str(parent) != '.'}
    for parent,dirs,names in os.walk(stage,followlinks=False):
        for name in dirs:
            directory = checked(Path(parent)/name)
            if directory.relative_to(stage).as_posix() not in allowed_dirs:
                raise ValueError('Unowned staging directory')
        for name in names:
            path=checked(Path(parent)/name)
            if path.relative_to(stage).as_posix() not in allowed:raise ValueError('Unowned staging content')
            read_file(path)


def prepare_session_import(source_snapshot,staging,*,_after_copy=None):
    source=checked(source_snapshot);stage=checked(staging)
    if not source.is_dir() or stage==source or stage.is_relative_to(source) or source.is_relative_to(stage):
        raise ValueError('Migration roots must be distinct and disjoint')
    files=inventory(source)
    plan={'format':FORMAT,'source_identity':digest(str(source).encode()),'files':files}
    plan_digest=digest(encoded(plan))
    if not stage.exists():stage.mkdir(mode=0o700)
    if not stage.is_dir() or stage.stat().st_mode & 0o077:raise ValueError('Staging must be a private directory')
    lock=checked(stage/'.import.lock')
    fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        manifest_path=stage/'manifest.json'
        _check_stage(stage,files)
        if manifest_path.exists():
            manifest=json.loads(read_file(manifest_path))
            expected={'format':FORMAT,'plan_digest':plan_digest,'plan':plan,'state':manifest.get('state'),
                'activation_allowed':False,'writer_fence_verified':False,'settings_migrated':False}
            if manifest!=expected or any(type(manifest.get(k)) is not bool for k in ('activation_allowed','writer_fence_verified','settings_migrated')) or manifest.get('state') not in {'copying','verified_inactive'}:
                raise ValueError('Migration source or plan changed')
        else:
            if any(p.name not in {'.import.lock'} for p in stage.iterdir()):raise ValueError('Unowned staging directory')
            manifest={'format':FORMAT,'plan_digest':plan_digest,'plan':plan,'state':'copying',
                'activation_allowed':False,'writer_fence_verified':False,'settings_migrated':False}
            atomic_write(manifest_path,encoded(manifest))
        already_complete=manifest['state']=='verified_inactive'
        for relative,fact in files.items():
            destination=checked(stage/'payload'/relative)
            if destination.exists():
                if digest(read_file(destination))!=fact['digest']:raise ValueError('Staged file integrity mismatch')
                continue
            if already_complete:raise ValueError('Verified staging file is missing')
            data=read_file(source/relative)
            if digest(data)!=fact['digest']:raise ValueError('Source changed during import')
            atomic_write(destination,data)
            if _after_copy:_after_copy(relative)
        if inventory(source)!=files:raise ValueError('Source changed during import')
        _check_stage(stage,files)
        for relative,fact in files.items():
            if digest(read_file(stage/'payload'/relative))!=fact['digest']:raise ValueError('Staged file integrity mismatch')
        manifest['state']='verified_inactive'
        atomic_write(manifest_path,encoded(manifest))
        return {'format':FORMAT,'state':'verified_inactive','plan_digest':plan_digest,
            'file_count':len(files),'byte_count':sum(f['size'] for f in files.values()),
            'idempotent':already_complete,'activation_allowed':False,'writer_fence_verified':False,
            'settings_migrated':False}
    finally:os.close(fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-snapshot',type=Path,required=True)
    parser.add_argument('--staging',type=Path,required=True)
    args=parser.parse_args()
    try:result=prepare_session_import(args.source_snapshot,args.staging)
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','error_code':'session_import_rejected','activation_allowed':False}))
        return 1
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
