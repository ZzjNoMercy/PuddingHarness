"""Persistent authority for an enrolled existing Harness Home.

Enrollment records its existing session_harness writer; it does not activate a
migrated installation. Suspension first persistently freezes all participating
Home writers. Reassignment commits an assigned revision bound to a verified
installation migration manifest, and audited thaw retires the freeze marker
into the authority record only for an installation assigned to this Harness.
Cross-product CUTOVER orchestration lives in ``harness.cutover_orchestrator``.
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
from contextlib import contextmanager

BINDING = '.installation-authority-v1.json'
FORMAT = 'puddingharness-writer-authority/v1'
MAX_BYTES = 65536
SELF = 'puddingharness'
WRITERS = ('puddingclaw', 'puddingharness')
POINTER = 'active-installation.json'
POINTER_FORMAT = 'puddingharness-active-installation/v1'


def encoded(value):
    return (json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)+'\n').encode()


def digest(value):return hashlib.sha256(encoded(value)).hexdigest()


def _unique(items):
    result={}
    for key,value in items:
        if key in result:raise ValueError('Duplicate authority JSON key')
        result[key]=value
    return result


def read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077 or info.st_size>MAX_BYTES:
            raise ValueError('Authority file must be private owned regular and bounded')
        raw=os.read(fd,MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('Authority file exceeds budget')
        current=path.lstat()
        if (current.st_dev,current.st_ino,current.st_size)!=(info.st_dev,info.st_ino,len(raw)):
            raise ValueError('Authority file changed')
        value=json.loads(raw,object_pairs_hook=_unique)
        if not isinstance(value,dict) or encoded(value)!=raw:raise ValueError('Authority JSON must be canonical')
        return value
    finally:os.close(fd)


def identity(root):
    info=root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('Authority roots must be private owned directories')
    return {'path':str(root),'device':info.st_dev,'inode':info.st_ino}


def _path(value):
    root=Path(value).expanduser()
    if not root.is_absolute() or '..' in root.parts or any(p.is_symlink() for p in (root,*root.parents)):
        raise ValueError('Authority paths must be absolute and unlinked')
    for parent in root.parents:
        info=parent.stat()
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise ValueError('Authority ancestor is writable without sticky protection')
    return root


@contextmanager
def lock(root,*,exclusive):
    path=root/'.writer-authority.lock'
    fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK|(os.O_CREAT if exclusive else 0),0o600)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1 or info.st_mode&0o077:
            raise ValueError('Invalid authority lock')
        fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
        current=path.lstat()
        if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise ValueError('Authority lock changed')
        yield fd
    finally:os.close(fd)


def load_binding(home):
    if (home/(BINDING+'.part')).exists() or (home/(BINDING+'.part')).is_symlink():
        raise ValueError('Authority enrollment is incomplete')
    path=home/BINDING
    if not path.exists() and not path.is_symlink():return None
    value=read(path)
    if set(value)!={'format','home','authority','enrollment_id'} or value['format']!=FORMAT or value['home']!=identity(home):
        raise ValueError('Authority Home binding changed')
    root=_path(value['authority']['path'])
    if value['authority']!=identity(root):raise ValueError('Authority directory changed')
    return value


def journal(binding):
    value=read(Path(binding['authority']['path'])/'journal.json')
    if set(value)!={'format','binding_sha256','events'} or value['format']!=FORMAT or value['binding_sha256']!=digest(binding):
        raise ValueError('Authority journal binding mismatch')
    events=value['events']
    if not isinstance(events,list) or not events:raise ValueError('Invalid authority history')
    base={'revision','previous','operation_id','state','writer','freeze_receipt_sha256','sha256'}
    previous=None
    for number,event in enumerate(events):
        expected=base|({'active_installation_revision','migration_manifest_sha256','rollback_evidence_sha256'} if number>0 and number%2==0 else set())
        if not isinstance(event,dict) or set(event)!=expected or type(event['revision']) is not int or event['revision']!=number or event['previous']!=previous:
            raise ValueError('Invalid authority revision chain')
        payload={key:item for key,item in event.items() if key!='sha256'}
        if event['sha256']!=digest(payload):raise ValueError('Authority revision digest mismatch')
        _operation(event['operation_id'])
        if number==0:
            if event['state']!='existing_writer' or event['writer']!='session_harness' or event['freeze_receipt_sha256'] is not None or event['operation_id']!=binding['enrollment_id']:
                raise ValueError('Invalid existing writer enrollment')
        elif number%2:
            if event['state']!='suspended' or event['writer'] is not None or not _hex64(event['freeze_receipt_sha256']):
                raise ValueError('Invalid suspended authority')
        else:
            if event['state']!='assigned' or event['writer'] not in WRITERS:raise ValueError('Invalid assigned authority')
            if not _hex64(event['freeze_receipt_sha256']) or event['freeze_receipt_sha256']!=events[number-1]['freeze_receipt_sha256']:
                raise ValueError('Invalid assigned freeze commitment')
            if not isinstance(event['active_installation_revision'],str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',event['active_installation_revision']):
                raise ValueError('Invalid active installation revision commitment')
            if not _hex64(event['migration_manifest_sha256']):raise ValueError('Invalid migration manifest commitment')
            if event['writer']=='puddingclaw':
                if not _hex64(event['rollback_evidence_sha256']):raise ValueError('Invalid rollback evidence commitment')
            elif event['rollback_evidence_sha256'] is not None:raise ValueError('Invalid rollback evidence commitment')
        previous=event['sha256']
    return value


def _hex64(value):
    return isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) is not None


def _operation(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,159}',value):raise ValueError('Invalid authority operation')


def _event(number,previous,operation,state,writer,receipt,**extra):
    value={'revision':number,'previous':previous,'operation_id':operation,'state':state,'writer':writer,'freeze_receipt_sha256':receipt,**extra}
    return dict(value,sha256=digest(value))


def acquire_writer(home):
    binding=load_binding(home)
    if binding is None:return None
    root=Path(binding['authority']['path'])
    with lock(root,exclusive=False) as fd:
        current=journal(binding)['events'][-1]
        if current['state']=='assigned':
            if current['writer']!=SELF:raise ValueError('Installation writer authority is assigned to another product')
            _active_pointer(home,current)
        elif current['state']!='existing_writer':raise ValueError('Installation has no active Harness writer authority')
        if load_binding(home)!=binding:raise ValueError('Authority binding changed')
        return os.dup(fd)


def _active_pointer(home, event):
    """Require the runtime pointer to agree with the assigned authority head."""
    value=read(home/POINTER)
    expected={'format','operation_id','cutover_manifest_sha256','prepared_manifest_sha256',
              'source_home_identity','source_freeze_receipt_sha256',
              'active_installation_revision','harness_assigned_event_sha256',
              'knowledge_assigned_event_sha256','active_writers'}
    writers={'session_harness':'puddingharness','knowledge_catalog':'puddingknowledge',
             'connector_jobs':'puddingknowledge'}
    if (set(value)!=expected or value['format']!=POINTER_FORMAT
            or value['operation_id']!=event['operation_id']
            or value['prepared_manifest_sha256']!=event['migration_manifest_sha256']
            or value['active_installation_revision']!=event['active_installation_revision']
            or value['harness_assigned_event_sha256']!=event['sha256']
            or value['active_writers']!=writers
            or not _hex64(value['cutover_manifest_sha256'])
            or not _hex64(value['source_home_identity'])
            or not re.fullmatch(r'sha256:[0-9a-f]{64}',value['source_freeze_receipt_sha256'])
            or not _hex64(value['knowledge_assigned_event_sha256'])):
        raise ValueError('Active installation pointer does not match writer authority')
    return value


def enroll(home,authority,enrollment_id):
    from harness.installation_guard import InstallationGuard
    from harness.home_freeze import _cli_freeze_gate,_sync_directory
    from harness.migration_orchestrator import _replace_private
    _operation(enrollment_id);home=_path(home);root=_path(authority)
    if home==root or home.is_relative_to(root) or root.is_relative_to(home):raise ValueError('Authority and Home must be disjoint')
    identity(home)
    with InstallationGuard(home,exclusive=True,allow_frozen=True) as guard,_cli_freeze_gate(home):
        guard._check_not_frozen()
        if not root.exists():root.mkdir(mode=0o700);_sync_directory(root.parent)
        binding={'format':FORMAT,'home':identity(home),'authority':identity(root),'enrollment_id':enrollment_id}
        with lock(root,exclusive=True):
            allowed={'.writer-authority.lock','journal.json'}
            artifact=r'(?:thaw-receipt|freeze-marker)-rev[0-9]+\.json'
            temporary=r'\.(?:journal\.json|(?:thaw-receipt|freeze-marker)-rev[0-9]+\.json)\.tmp-[0-9a-f]{16}'
            for entry in root.iterdir():
                if entry.name not in allowed and not re.fullmatch(artifact,entry.name) and not re.fullmatch(temporary,entry.name):raise ValueError('Unknown authority entry')
            existing=home/BINDING;part=home/(BINDING+'.part')
            for path in (existing,part):
                if path.exists() or path.is_symlink():
                    if read(path)!=binding:raise ValueError('Authority enrollment changed')
            first={'format':FORMAT,'binding_sha256':digest(binding),'events':[_event(0,None,enrollment_id,'existing_writer','session_harness',None)]}
            if (root/'journal.json').exists() or (root/'journal.json').is_symlink():
                if journal(binding)!=first:raise ValueError('Authority enrollment already suspended or changed')
            else:_replace_private(root/'journal.json',encoded(first))
            if not existing.exists():
                if not part.exists():
                    fd=os.open(part,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    try:
                        data=encoded(binding);offset=0
                        while offset<len(data):offset+=os.write(fd,data[offset:])
                        os.fsync(fd)
                    finally:os.close(fd)
                _sync_directory(home)
                os.replace(part,existing);_sync_directory(home)
            elif part.exists():raise ValueError('Unexpected authority publication part')
            _sync_directory(root);_sync_directory(home)
            guard.verify()
            return first


def suspend(home,operation_id):
    from harness.home_freeze import freeze_home
    from harness.migration_orchestrator import _replace_private
    _operation(operation_id);home=_path(home);binding=load_binding(home)
    if binding is None:raise ValueError('Harness Home is not enrolled')
    before=journal(binding)
    if before['events'][-1]['state']=='suspended':
        if before['events'][-1]['operation_id']!=operation_id:
            raise ValueError('Authority suspension operation changed')
        from harness.target_freeze import _record
        _record(home,'.installation-freeze-v1.json',before['events'][-1]['freeze_receipt_sha256'])
    receipt=freeze_home(home,operation_id)
    with lock(Path(binding['authority']['path']),exclusive=True):
        if load_binding(home)!=binding:raise ValueError('Authority binding changed')
        current=journal(binding)
        head=current['events'][-1]
        if head['state']=='suspended':
            event=_event(head['revision'],current['events'][-2]['sha256'],operation_id,'suspended',None,receipt['receipt_sha256'])
            if head!=event:raise ValueError('Authority suspension changed')
        else:
            event=_event(len(current['events']),head['sha256'],operation_id,'suspended',None,receipt['receipt_sha256'])
            current=dict(current,events=[*current['events'],event])
            _replace_private(Path(binding['authority']['path'])/'journal.json',encoded(current))
        return current


def assign(home,manifest,operation_id,writer,*,rollback_evidence=None):
    from harness.installation_manifest import validate_manifest
    from harness.migration_orchestrator import _json,_read_private,_replace_private
    from harness.target_freeze import _record
    _operation(operation_id);home=_path(home)
    if writer not in WRITERS:raise ValueError('Invalid assigned writer')
    raw=_read_private(manifest)
    document=_json(raw);validate_manifest(document)
    commitment=hashlib.sha256(raw).hexdigest()
    evidence=None
    if writer==SELF:
        if document['state']!='PREPARED':raise ValueError('Assignment to this Harness requires a PREPARED installation manifest')
        if rollback_evidence is not None:raise ValueError('Forward assignment carries no rollback evidence')
    else:
        if document['state']!='ROLLED_BACK':raise ValueError('Rollback assignment requires a ROLLED_BACK installation manifest')
        if rollback_evidence is None:raise ValueError('Rollback assignment requires rollback evidence')
        evidence=hashlib.sha256(_read_private(rollback_evidence)).hexdigest()
        if document.get('rollback_evidence_digest')!='sha256:'+evidence:raise ValueError('Rollback evidence does not match the installation manifest')
    binding=load_binding(home)
    if binding is None:raise ValueError('Harness Home is not enrolled')
    with lock(Path(binding['authority']['path']),exclusive=True):
        if load_binding(home)!=binding:raise ValueError('Authority binding changed')
        current=journal(binding)
        head=current['events'][-1]
        extra={'active_installation_revision':'sha256:'+commitment,'migration_manifest_sha256':commitment,'rollback_evidence_sha256':evidence}
        if head['state']=='assigned':
            event=_event(head['revision'],current['events'][-2]['sha256'],operation_id,'assigned',writer,head['freeze_receipt_sha256'],**extra)
            if head!=event:raise ValueError('Authority assignment changed')
            return current
        if head['state']!='suspended':raise ValueError('Installation writer authority is not suspended')
        if head['operation_id']!=operation_id:raise ValueError('Authority assignment operation changed')
        _record(home,'.installation-freeze-v1.json',head['freeze_receipt_sha256'])
        event=_event(len(current['events']),head['sha256'],operation_id,'assigned',writer,head['freeze_receipt_sha256'],**extra)
        current=dict(current,events=[*current['events'],event])
        _replace_private(Path(binding['authority']['path'])/'journal.json',encoded(current))
        return current


def thaw(home,manifest,operation_id,*,_after_receipt=None):
    from harness.home_freeze import _cli_freeze_gate,_sync_directory
    from harness.installation_guard import InstallationGuard
    from harness.installation_manifest import validate_manifest
    from harness.migration_orchestrator import _json,_read_private,_replace_private
    from harness.target_freeze import _record
    _operation(operation_id);home=_path(home)
    raw=_read_private(manifest)
    document=_json(raw);validate_manifest(document)
    commitment=hashlib.sha256(raw).hexdigest()
    binding=load_binding(home)
    if binding is None:raise ValueError('Harness Home is not enrolled')
    root=Path(binding['authority']['path'])
    with InstallationGuard(home,exclusive=True,allow_frozen=True) as guard,_cli_freeze_gate(home):
        with lock(root,exclusive=True):
            if load_binding(home)!=binding:raise ValueError('Authority binding changed')
            current=journal(binding);head=current['events'][-1]
            if head['state']!='assigned' or head['writer']!=SELF:raise ValueError('Installation writer authority is not assigned to this Harness')
            if head['operation_id']!=operation_id:raise ValueError('Authority thaw operation changed')
            if head['migration_manifest_sha256']!=commitment or head['active_installation_revision']!='sha256:'+commitment:
                raise ValueError('Authority thaw manifest changed')
            receipt_path=root/f"thaw-receipt-rev{head['revision']}.json"
            retired_path=root/f"freeze-marker-rev{head['revision']}.json"
            marker_path=home/'.installation-freeze-v1.json'
            if (home/'.installation-freeze-v1.json.part').exists() or (home/'.installation-freeze-v1.json.part').is_symlink():
                raise ValueError('Freeze publication is incomplete')
            receipt={'format':FORMAT,'state':'thawed','operation_id':operation_id,'writer':SELF,'revision':head['revision'],
                     'revision_sha256':head['sha256'],'freeze_receipt_sha256':head['freeze_receipt_sha256'],
                     'migration_manifest_sha256':head['migration_manifest_sha256'],'active_installation_revision':head['active_installation_revision']}
            retired=retired_path.exists() or retired_path.is_symlink()
            if retired:
                if marker_path.exists() or marker_path.is_symlink():raise ValueError('Conflicting authority thaw state')
                if hashlib.sha256(_read_private(retired_path,4096)).hexdigest()!=head['freeze_receipt_sha256']:raise ValueError('Retired freeze marker changed')
            else:
                _record(home,'.installation-freeze-v1.json',head['freeze_receipt_sha256'])
            if receipt_path.exists() or receipt_path.is_symlink():
                if read(receipt_path)!=receipt:raise ValueError('Authority thaw receipt changed')
            else:
                if retired:raise ValueError('Authority thaw receipt missing for retired freeze marker')
                _replace_private(receipt_path,encoded(receipt))
            if _after_receipt:_after_receipt()
            if not retired:
                os.rename(marker_path,retired_path)
                _sync_directory(home);_sync_directory(root)
            guard.verify()
            return {'state':'thawed','thaw_receipt_sha256':hashlib.sha256(encoded(receipt)).hexdigest(),'journal':current,'activation_allowed':True}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['enroll','suspend','assign','thaw','status']);parser.add_argument('--home',required=True);parser.add_argument('--authority');parser.add_argument('--operation-id');parser.add_argument('--manifest');parser.add_argument('--writer',choices=WRITERS);parser.add_argument('--rollback-evidence')
    args=parser.parse_args(argv)
    try:
        if args.action=='enroll':report={'journal':enroll(args.home,args.authority,args.operation_id)}
        elif args.action=='suspend':report={'journal':suspend(args.home,args.operation_id)}
        elif args.action=='assign':report={'journal':assign(args.home,args.manifest,args.operation_id,args.writer,rollback_evidence=args.rollback_evidence),'activation_allowed':False}
        elif args.action=='thaw':report=thaw(args.home,args.manifest,args.operation_id)
        else:
            binding=load_binding(_path(args.home))
            if binding is None:raise ValueError('Home is not enrolled')
            current=journal(binding);head=current['events'][-1]
            report={'journal':current,'head':{'revision':head['revision'],'state':head['state'],'writer':head['writer']}}
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','activation_allowed':False}));return 1
    print(json.dumps({'format':FORMAT,'status':'ok',**report,'installation_cutover_performed':False},sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
