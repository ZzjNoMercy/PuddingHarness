import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {cleanSourceRevision,verifyCommittedStage} from '../scripts/source-provenance.mjs';

test('staged bytes must match the exact clean commit, including restored-source races',async()=>{
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'harness-source-proof-'));
 const stage=await fs.mkdtemp(path.join(os.tmpdir(),'harness-stage-proof-'));
 const git=(...args)=>execFileSync('git',['-C',root,...args],{stdio:'pipe'});
 git('init');git('config','user.email','fixture@example.invalid');git('config','user.name','Fixture');
 await fs.mkdir(path.join(root,'backend'));await fs.writeFile(path.join(root,'backend/app.py'),'original');
 git('add','backend/app.py');git('commit','-m','fixture');
 const revision=await cleanSourceRevision(root);
 const rows=[{path:'app.py',sha256:crypto.createHash('sha256').update('original').digest('hex')}];
 await fs.writeFile(path.join(stage,'app.py'),'original');
 await verifyCommittedStage(root,'backend',stage,rows,revision);
 await fs.writeFile(path.join(root,'backend/app.py'),'dirty');
 await assert.rejects(cleanSourceRevision(root),/clean committed/);
 git('restore','backend/app.py');
 await fs.writeFile(path.join(stage,'app.py'),'dirty');
 rows[0].sha256=crypto.createHash('sha256').update('dirty').digest('hex');
 await assert.rejects(verifyCommittedStage(root,'backend',stage,rows,revision),/does not match/);
});
