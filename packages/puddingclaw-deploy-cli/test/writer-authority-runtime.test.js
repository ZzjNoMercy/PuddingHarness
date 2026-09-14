import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {assertRuntimeAuthorityContract} from '../src/runtime-bundle.js';
test('enrolled Home rejects older embedded runtime before installation or startup',async()=>{
 const home=await mkdtemp(path.join(os.tmpdir(),'authority-runtime-'));
 try{
  await assertRuntimeAuthorityContract({contracts:{}},home);
  await writeFile(path.join(home,'.installation-authority-v1.json'),'{}');
  await assert.rejects(assertRuntimeAuthorityContract({contracts:{}},home),error=>error.code==='runtime_authority_unsupported');
  await assertRuntimeAuthorityContract({contracts:{writer_authority:1}},home);
 }finally{await rm(home,{recursive:true,force:true});}
});
