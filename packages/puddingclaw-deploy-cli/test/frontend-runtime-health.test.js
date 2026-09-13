import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {fileURLToPath} from 'node:url';

for (const instance of ['ph-fixture', '']) {
 test(`standalone HTTP identity ${instance ? 'is bound' : 'requires an instance'}`,{timeout:5000},async t=>{
  const helper=fileURLToPath(new URL('../scripts/frontend-runtime-health.cjs',import.meta.url));
  const code=`require(${JSON.stringify(helper)});const http=require('node:http');const server=http.createServer((req,res)=>res.end('page'));server.listen(0,'127.0.0.1',()=>console.log(server.address().port));`;
  const child=spawn(process.execPath,['-e',code],{env:{...process.env,PUDDINGHARNESS_INSTANCE_ID:instance},stdio:['ignore','pipe','pipe']});
  t.after(()=>child.kill('SIGKILL'));
  const [chunk]=await once(child.stdout,'data');
  const url=`http://127.0.0.1:${Number(chunk.toString().trim())}`;
  const response=await fetch(url+'/.puddingharness/health');
  assert.equal(response.status,instance ? 200 : 503);
  assert.deepEqual(await response.json(),{name:'PuddingHarness',role:'frontend',instance_id:instance || null});
  assert.equal(await (await fetch(url+'/')).text(),'page');
 });
}
