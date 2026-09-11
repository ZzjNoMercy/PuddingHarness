"""Read-only admission of the versioned raw Claw Home snapshot contract.

No Claw modules are imported. This proves private artifact integrity, not the
source installation version, absence of old writers, or permission to activate.
"""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat

FORMAT = 'puddingclaw-source-home-snapshot/v1'
LOCK_NAME = '.installation-gate-v1.lock'
MAX_JSON = 32 * 1024**2
MAX_ENTRIES = 50000
MAX_FILE = 2 * 1024**3
MAX_TOTAL = 16 * 1024**3


def _sha(data): return hashlib.sha256(data).hexdigest()
def _encoded(value): return (json.dumps(value,sort_keys=True,separators=(',',':'))+'\n').encode()


def _unique(pairs):
    result = {}
    for key,value in pairs:
        if key in result: raise ValueError('Duplicate snapshot JSON key')
        result[key] = value
    return result


def _path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('Snapshot path must be absolute and unlinked')
    return path.resolve()


def _private(info, *, directory=False):
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError('Snapshot objects must be private and owned')
    if not directory and info.st_nlink != 1: raise ValueError('Linked snapshot file rejected')


def _read(path):
    fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info = os.fstat(fd); _private(info)
        if info.st_size > MAX_JSON: raise ValueError('Snapshot JSON limit exceeded')
        chunks=[]; size=0
        while chunk := os.read(fd,min(65536,MAX_JSON+1-size)):
            chunks.append(chunk);size+=len(chunk)
            if size>MAX_JSON: raise ValueError('Snapshot JSON limit exceeded')
        raw=b''.join(chunks)
        value=json.loads(raw,object_pairs_hook=_unique)
        if not isinstance(value,dict) or _encoded(value)!=raw: raise ValueError('Snapshot JSON must be canonical')
        return value,raw
    finally: os.close(fd)


def _hex(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def _relative(value):
    if not isinstance(value,str) or not value or '\x00' in value: raise ValueError('Invalid snapshot relative path')
    path=PurePosixPath(value)
    if path.is_absolute() or path.as_posix()!=value or any(p in ('.','..') for p in path.parts):
        raise ValueError('Invalid snapshot relative path')
    return value


def _validate_inventory(value):
    if not isinstance(value,dict) or set(value)!={'files','directories','total_bytes'}: raise ValueError('Invalid snapshot inventory')
    files,dirs=value['files'],value['directories']
    if not isinstance(files,dict) or not isinstance(dirs,list) or len(files)+len(dirs)>MAX_ENTRIES:
        raise ValueError('Snapshot entry limit exceeded')
    if any(not isinstance(p,str) for p in dirs) or len(set(dirs))!=len(dirs): raise ValueError('Invalid snapshot directories')
    directory_set=set(dirs)
    paths=set(files)|directory_set
    if len(paths)!=len(files)+len(dirs): raise ValueError('Snapshot path collision')
    for p in paths:
        _relative(p)
        for parent in PurePosixPath(p).parents:
            if parent.as_posix()!='.' and parent.as_posix() not in directory_set: raise ValueError('Snapshot parent missing')
    total=0
    for fact in files.values():
        if not isinstance(fact,dict) or set(fact)!={'sha256','size'} or not _hex(fact['sha256']) or type(fact['size']) is not int or not 0<=fact['size']<=MAX_FILE:
            raise ValueError('Invalid snapshot file commitment')
        total+=fact['size']
    if type(value['total_bytes']) is not int or total!=value['total_bytes'] or total>MAX_TOTAL:
        raise ValueError('Snapshot total limit exceeded')


def _inventory(root):
    files={};dirs=[];total=0;count=0
    def walk(fd,prefix=''):
        nonlocal total,count
        for entry in sorted(os.scandir(fd),key=lambda item:item.name):
            count+=1
            if count>MAX_ENTRIES: raise ValueError('Snapshot entry limit exceeded')
            relative=prefix+entry.name
            info=entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child=os.open(entry.name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    _private(os.fstat(child),directory=True);dirs.append(relative);walk(child,relative+'/')
                finally: os.close(child)
            else:
                child=os.open(entry.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                try:
                    before=os.fstat(child);_private(before)
                    if before.st_size>MAX_FILE: raise ValueError('Snapshot file limit exceeded')
                    digest=hashlib.sha256();size=0
                    while data:=os.read(child,1024**2):
                        size+=len(data);total+=len(data)
                        if size>MAX_FILE or total>MAX_TOTAL: raise ValueError('Snapshot byte limit exceeded')
                        digest.update(data)
                    after=os.fstat(child)
                    if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                        raise ValueError('Snapshot changed during verification')
                    files[relative]={'sha256':digest.hexdigest(),'size':size}
                finally: os.close(child)
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try: _private(os.fstat(fd),directory=True);walk(fd)
    finally: os.close(fd)
    return {'files':files,'directories':dirs,'total_bytes':total}


class VerifiedSourceSnapshot:
    def __init__(self,path):
        self.root=_path(path);self.payload=self.root/'payload';self.fd=None;self.commitment=None

    def __enter__(self):
        _private(self.root.stat(),directory=True)
        self.fd=os.open(self.root/LOCK_NAME,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            info=os.fstat(self.fd);_private(info)
            fcntl.flock(self.fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
            self._lock_identity=(info.st_dev,info.st_ino)
            self.verify()
            return self
        except BaseException:
            os.close(self.fd);self.fd=None;raise

    def verify(self):
        if self.fd is None: raise ValueError('Snapshot admission not held')
        info=(self.root/LOCK_NAME).stat(follow_symlinks=False);_private(info)
        if (info.st_dev,info.st_ino)!=self._lock_identity: raise ValueError('Snapshot lock changed')
        if {p.name for p in self.root.iterdir()}!={LOCK_NAME,'plan.json','manifest.json','payload'}:
            raise ValueError('Snapshot is incomplete or has unexpected entries')
        plan,plan_raw=_read(self.root/'plan.json');manifest,manifest_raw=_read(self.root/'manifest.json')
        if set(plan)!={'format','source_identity','source_directory_identity','output_identity','inventory'} or plan['format']!=FORMAT:
            raise ValueError('Unsupported snapshot plan')
        identity=plan['source_directory_identity']
        if not _hex(plan['source_identity']) or not isinstance(identity,dict) or set(identity)!={'device','inode'} or any(type(v)is not int or v<0 for v in identity.values()):
            raise ValueError('Invalid source directory identity')
        if plan['output_identity']!=_sha(str(self.root).encode()): raise ValueError('Snapshot was relocated')
        _validate_inventory(plan['inventory'])
        expected={'format':FORMAT,'plan_digest':_sha(plan_raw),'state':'verified_raw_home_snapshot','inventory':plan['inventory'],
            'cooperative_admission_held':True,'writer_fence_verified':False,'catalog_normalization_required':True,
            'activation_allowed':False,'installation_prepared':False,'credential_rebind_required':True,
            'requires_domain_migration':True,'source_home_kind':'legacy_puddingclaw_home',
            'catalog':{'relative_path':'db/catalog.sqlite3','role':'legacy_claw_core_catalog','direct_import_allowed':False,'wal_policy':'captured_as_sibling_files'}}
        # Compare canonical bytes so booleans cannot be replaced by integers.
        if manifest_raw!=_encoded(expected): raise ValueError('Snapshot manifest contract mismatch')
        commitment={'format':FORMAT,'plan_sha256':_sha(plan_raw),'manifest_sha256':_sha(manifest_raw)}
        if self.commitment is not None and commitment!=self.commitment: raise ValueError('Snapshot commitment changed')
        if _inventory(self.payload)!=plan['inventory']: raise ValueError('Snapshot payload integrity mismatch')
        self.commitment=commitment
        return commitment

    def __exit__(self,*unused):
        if self.fd is not None: os.close(self.fd);self.fd=None
