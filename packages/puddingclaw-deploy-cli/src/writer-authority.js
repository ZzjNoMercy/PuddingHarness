// Reader for the versioned installed Python writer-authority protocol.
// Called while holding the existing Home CLI gate, before creating a write
// ticket. Authority updates must freeze Home writers and tickets first.
// Revision 2 and beyond alternate assigned/suspended; admission requires the
// journal head to be existing_writer or assigned to this product.
import fs from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { CliError } from './errors.js';
const BINDING='.installation-authority-v1.json';
const FORMAT='puddingharness-writer-authority/v1';
const SELF='puddingharness';
const POINTER='active-installation.json';
const POINTER_FORMAT='puddingharness-active-installation/v1';
const OPERATION_RE=/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
const HEX64_RE=/^[0-9a-f]{64}$/;
const DIGEST_RE=/^sha256:[0-9a-f]{64}$/;
const BASE_KEYS=['revision','previous','operation_id','state','writer','freeze_receipt_sha256','sha256'];
const ASSIGNED_KEYS=[...BASE_KEYS,'active_installation_revision','migration_manifest_sha256','rollback_evidence_sha256'];
const fail=()=>{throw new CliError('installation writer authority is unavailable or changed',{code:'installation_authority_rejected',exitCode:1});};
function canonical(value){
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(value!==null && typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
const encoded=v=>canonical(v)+'\n';
const digest=v=>createHash('sha256').update(encoded(v)).digest('hex');
const same=(a,b)=>canonical(a)===canonical(b);
const keys=(v,list)=>v && !Array.isArray(v) && typeof v==='object' && same(Object.keys(v).sort(),list.slice().sort());
async function present(file){try{await fs.lstat(file);return true;}catch(error){if(error.code==='ENOENT')return false;throw error;}}
async function read(file){
  const handle=await fs.open(file,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
  try{
    const info=await handle.stat();
    if(!info.isFile() || info.uid!==process.getuid() || info.nlink!==1 || (info.mode&0o77) || info.size>65536)fail();
    const buffer=Buffer.alloc(65537);const {bytesRead}=await handle.read(buffer,0,buffer.length,0);
    const current=await fs.lstat(file);
    if(bytesRead!==info.size || current.dev!==info.dev || current.ino!==info.ino || current.size!==info.size)fail();
    const raw=buffer.subarray(0,bytesRead).toString('utf8');const value=JSON.parse(raw);
    if(encoded(value)!==raw)fail();
    return value;
  }finally{await handle.close();}
}
async function identity(root){
  if(typeof root!=='string' || !path.isAbsolute(root) || await fs.realpath(root)!==root)fail();
  for(let parent=path.dirname(root);;parent=path.dirname(parent)){
    const ancestor=await fs.stat(parent);
    if((ancestor.mode&0o22) && !(ancestor.mode&0o1000))fail();
    if(parent===path.dirname(parent))break;
  }
  const info=await fs.lstat(root);
  if(!info.isDirectory() || info.isSymbolicLink() || info.uid!==process.getuid() || (info.mode&0o77) || !Number.isSafeInteger(info.ino))fail();
  return {path:root,device:info.dev,inode:info.ino};
}
export async function assertWriterAuthority(home){
  try{
    if(await present(path.join(home,BINDING+'.part')))fail();
    const file=path.join(home,BINDING);if(!await present(file))return null;
    const binding=await read(file);
    if(!keys(binding,['format','home','authority','enrollment_id']) || binding.format!==FORMAT || !same(binding.home,await identity(home)))fail();
    if(typeof binding.enrollment_id!=='string' || !OPERATION_RE.test(binding.enrollment_id))fail();
    if(!same(binding.authority,await identity(binding.authority?.path)))fail();
    const document=await read(path.join(binding.authority.path,'journal.json'));
    if(!keys(document,['format','binding_sha256','events']) || document.format!==FORMAT || document.binding_sha256!==digest(binding) || !Array.isArray(document.events) || document.events.length<1)fail();
    let previous=null;
    for(let number=0;number<document.events.length;number++){
      const event=document.events[number];
      const assigned=number>0 && number%2===0;
      if(!keys(event,assigned?ASSIGNED_KEYS:BASE_KEYS))fail();
      if(typeof event.revision!=='number' || !Number.isInteger(event.revision) || event.revision!==number || event.previous!==previous)fail();
      const {sha256,...payload}=event;
      if(sha256!==digest(payload))fail();
      if(typeof event.operation_id!=='string' || !OPERATION_RE.test(event.operation_id))fail();
      if(number===0){
        if(event.state!=='existing_writer' || event.writer!=='session_harness' || event.freeze_receipt_sha256!==null || event.operation_id!==binding.enrollment_id)fail();
      }else if(number%2===1){
        if(event.state!=='suspended' || event.writer!==null || !HEX64_RE.test(event.freeze_receipt_sha256||''))fail();
      }else{
        if(event.state!=='assigned' || !['puddingclaw','puddingharness'].includes(event.writer))fail();
        if(!HEX64_RE.test(event.freeze_receipt_sha256||'') || event.freeze_receipt_sha256!==document.events[number-1].freeze_receipt_sha256)fail();
        if(!DIGEST_RE.test(event.active_installation_revision||'') || !HEX64_RE.test(event.migration_manifest_sha256||''))fail();
        if(event.writer==='puddingclaw'){if(!HEX64_RE.test(event.rollback_evidence_sha256||''))fail();}
        else if(event.rollback_evidence_sha256!==null)fail();
      }
      previous=event.sha256;
    }
    const head=document.events[document.events.length-1];
    if(head.state==='suspended' || (head.state==='assigned' && head.writer!==SELF))fail();
    if(head.state==='assigned'){
      const pointer=await read(path.join(home,POINTER));
      const pointerKeys=['format','operation_id','cutover_manifest_sha256','prepared_manifest_sha256',
        'source_home_identity','source_freeze_receipt_sha256','active_installation_revision',
        'harness_assigned_event_sha256','knowledge_assigned_event_sha256','active_writers'];
      const writers={session_harness:'puddingharness',knowledge_catalog:'puddingknowledge',connector_jobs:'puddingknowledge'};
      if(!keys(pointer,pointerKeys) || pointer.format!==POINTER_FORMAT
        || pointer.operation_id!==head.operation_id
        || pointer.prepared_manifest_sha256!==head.migration_manifest_sha256
        || pointer.active_installation_revision!==head.active_installation_revision
        || pointer.harness_assigned_event_sha256!==head.sha256
        || !same(pointer.active_writers,writers)
        || !HEX64_RE.test(pointer.cutover_manifest_sha256||'')
        || !HEX64_RE.test(pointer.source_home_identity||'')
        || !DIGEST_RE.test(pointer.source_freeze_receipt_sha256||'')
        || !HEX64_RE.test(pointer.knowledge_assigned_event_sha256||''))fail();
    }
    return {binding_sha256:document.binding_sha256,revision:head.revision,revision_sha256:head.sha256};
  }catch(error){if(error instanceof CliError)throw error;fail();}
}
