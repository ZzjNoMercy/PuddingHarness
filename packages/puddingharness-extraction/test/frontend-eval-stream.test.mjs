import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const ts = createRequire(path.join(root, 'frontend/package.json'))('typescript');
const file = path.join(root, 'packages/puddingharness-extraction/overlays/frontend/src/hooks/useEvalStream.ts');
const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText;
const completeText = ['触发质量','路由清晰度','上下文效率','复用与确定性','验证强度']
  .map(name => `[DIM:${name}:4:checked]`).join('\n') + '\n[VERDICT:基础扎实:20:reviewed]';

// Execute the actual hook callbacks with deterministic state/transport adapters.
// This checks async control flow, not React rendering or a real model provider.
async function scenario(events, {saveError = false} = {}) {
  const state = [], saved = [], calls = [];
  let cursor = 0, started = false, finish;
  const finished = new Promise(resolve => { finish = resolve; });
  const react = {
    useCallback: fn => fn, useEffect: () => {}, useRef: current => ({current}),
    useState(initial) {
      const index = cursor++; state[index] = initial;
      return [initial, value => {
        state[index] = typeof value === 'function' ? value(state[index]) : value;
        if (index === 0 && state[index] === 'evaluating') started = true;
        if (index === 0 && started && state[index] !== 'evaluating') finish();
      }];
    },
  };
  let sessionCreated = false;
  const api = {
    async createSession(options) {
      assert.equal(options.runtime_mode, 'agent');
      assert.equal(options.run_review_policy, 'off');
      sessionCreated = true;
      return {id:'session-eval-1'};
    },
    async *streamAgent(...args) {assert.ok(sessionCreated); calls.push(args); yield* events;},
  };
  const evalApi = {async saveEvalResult(...args) {
    if (saveError) throw new Error('save failed');
    saved.push(args);
  }};
  const exports = {};
  vm.runInNewContext(compiled, {
    exports, require: name => {
      if (name === 'react') return react;
      if (name === '@/lib/api') return api;
      if (name === '@/lib/evalApi') return evalApi;
      throw new Error('Unexpected import: ' + name);
    },
    AbortController, console, setInterval: () => 1, clearInterval: () => {},
  }, {filename: file});
  exports.useEvalStream().startEval('中文 / skill', '/skills/example/SKILL.md');
  let timeout;
  try {await Promise.race([finished, new Promise((_, reject) => {
    timeout = setTimeout(() => reject(new Error('Evaluation never reached a terminal UI state')), 3000);
  })]);} finally {clearTimeout(timeout);}
  assert.equal(calls.length, 1);
  assert.equal(calls[0][2], null);
  assert.ok(calls[0][3] instanceof AbortSignal);
  assert.equal(calls[0][1], 'session-eval-1');
  return {state, saved, calls};
}
const done = (content, outcome = 'completed') => ({event:'done',data:{content,run_outcome:outcome}});
const good = await scenario([done(completeText)]);
assert.equal(good.state[0], 'completed');
assert.equal(good.saved.length, 1);
assert.equal(good.saved[0][1].total_score, 20);
const streamed = await scenario([{event:'token',data:{content:'partial output'}}, done(completeText)]);
assert.equal(streamed.saved.length, 1);
for (const events of [
  [done(completeText, 'budget_exceeded')],
  [{event:'token',data:{content:completeText}}, done('')],
  [{event:'token',data:{content:completeText}}, done('Final answer without scores')],
  [done('[VERDICT:基础扎实:20:missing scores]')],
  [done(completeText.replace(':20:', ':21:'))],
  [{event:'token',data:{content:completeText}}],
  [{event:'permission_required',data:{id:'approval'}}],
  [{event:'error',data:{message:'provider unavailable'}}],
]) {
  const failed = await scenario(events);
  assert.equal(failed.state[0], 'idle');
  assert.ok(failed.state[8]);
  assert.equal(failed.saved.length, 0);
}
const saveFailure = await scenario([done(completeText)], {saveError:true});
assert.equal(saveFailure.state[0], 'idle');
assert.match(saveFailure.state[8], /save failed/);
console.log('Evaluation Agent transport and terminal-state checks: 11 passed');
