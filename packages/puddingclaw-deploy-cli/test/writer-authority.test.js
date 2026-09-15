import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, chmod, lstat, realpath, readdir, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { admitHomeWrite } from '../src/home-admission.js';

const FORMAT = 'puddingharness-writer-authority/v1';
const BINDING = '.installation-authority-v1.json';
const LEASES = '.installation-cli-leases';
const hex = (letter) => letter.repeat(64);

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value !== null && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map((k) => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  }
  return JSON.stringify(value);
}
const encoded = (v) => canonical(v) + '\n';
const digest = (v) => createHash('sha256').update(encoded(v)).digest('hex');

function event(number, previous, fields) {
  const value = { revision: number, previous, operation_id: 'op-1', ...fields };
  return { ...value, sha256: digest(value) };
}
const existingWriter = () => event(0, null, { operation_id: 'enroll-1', state: 'existing_writer', writer: 'session_harness', freeze_receipt_sha256: null });
const suspended = (number, previous, receipt) => event(number, previous, { state: 'suspended', writer: null, freeze_receipt_sha256: receipt });
const assigned = (number, previous, receipt, { writer = 'puddingharness', rollback = null } = {}) =>
  event(number, previous, {
    state: 'assigned', writer, freeze_receipt_sha256: receipt,
    active_installation_revision: 'sha256:' + hex('c'), migration_manifest_sha256: hex('c'),
    rollback_evidence_sha256: rollback,
  });

async function enrolled(events) {
  const root = await realpath(await mkdtemp(path.join(os.tmpdir(), 'writer-authority-')));
  const home = path.join(root, 'home');
  const authority = path.join(root, 'authority');
  await mkdir(home); await mkdir(authority);
  await chmod(home, 0o700); await chmod(authority, 0o700);
  const identify = async (target) => {
    const info = await lstat(target);
    return { path: target, device: info.dev, inode: info.ino };
  };
  const binding = { format: FORMAT, home: await identify(home), authority: await identify(authority), enrollment_id: 'enroll-1' };
  const journal = { format: FORMAT, binding_sha256: digest(binding), events };
  await writeFile(path.join(home, BINDING), encoded(binding)); await chmod(path.join(home, BINDING), 0o600);
  await writeFile(path.join(authority, 'journal.json'), encoded(journal)); await chmod(path.join(authority, 'journal.json'), 0o600);
  return { root, home, journal, cleanup: () => rm(root, { recursive: true, force: true }) };
}

async function admittedTicket(home) {
  let ticket;
  await admitHomeWrite(home, 'fixture', async () => {
    const leases = await readdir(path.join(home, LEASES));
    assert.equal(leases.length, 1);
    ticket = JSON.parse(await readFile(path.join(home, LEASES, leases[0]), 'utf8'));
  });
  return ticket;
}

test('existing writer head is admitted and the ticket binds revision 0', async () => {
  const rev0 = existingWriter();
  const { root, home, journal, cleanup } = await enrolled([rev0]);
  try {
    const ticket = await admittedTicket(home);
    assert.deepEqual(ticket.writer_authority, { binding_sha256: journal.binding_sha256, revision: 0, revision_sha256: rev0.sha256 });
  } finally { await cleanup(); }
});

test('suspended head without a marker is denied by the journal', async () => {
  const rev0 = existingWriter();
  const rev1 = suspended(1, rev0.sha256, hex('a'));
  const { home, cleanup } = await enrolled([rev0, rev1]);
  try {
    await assert.rejects(
      admitHomeWrite(home, 'fixture', async () => { throw new Error('callback ran'); }),
      (error) => error.code === 'installation_authority_rejected');
  } finally { await cleanup(); }
});

test('assigned-to-self head is admitted and the ticket binds revision 2', async () => {
  const rev0 = existingWriter();
  const rev1 = suspended(1, rev0.sha256, hex('a'));
  const rev2 = assigned(2, rev1.sha256, rev1.freeze_receipt_sha256);
  const { home, journal, cleanup } = await enrolled([rev0, rev1, rev2]);
  try {
    const ticket = await admittedTicket(home);
    assert.deepEqual(ticket.writer_authority, { binding_sha256: journal.binding_sha256, revision: 2, revision_sha256: rev2.sha256 });
  } finally { await cleanup(); }
});

test('rollback assigned-away head denies writes even without a freeze marker', async () => {
  const rev0 = existingWriter();
  const rev1 = suspended(1, rev0.sha256, hex('a'));
  const rev2 = assigned(2, rev1.sha256, rev1.freeze_receipt_sha256, { writer: 'puddingclaw', rollback: hex('d') });
  const { home, cleanup } = await enrolled([rev0, rev1, rev2]);
  try {
    await assert.rejects(
      admitHomeWrite(home, 'fixture', async () => { throw new Error('callback ran'); }),
      (error) => error.code === 'installation_authority_rejected');
  } finally { await cleanup(); }
});

test('re-suspended revision 3 denies writes after a self assignment', async () => {
  const rev0 = existingWriter();
  const rev1 = suspended(1, rev0.sha256, hex('a'));
  const rev2 = assigned(2, rev1.sha256, rev1.freeze_receipt_sha256);
  const rev3 = suspended(3, rev2.sha256, hex('b'));
  const { home, cleanup } = await enrolled([rev0, rev1, rev2, rev3]);
  try {
    await assert.rejects(
      admitHomeWrite(home, 'fixture', async () => { throw new Error('callback ran'); }),
      (error) => error.code === 'installation_authority_rejected');
  } finally { await cleanup(); }
});

const violations = {
  alternation: (rev0, rev1) => suspended(2, rev1.sha256, rev1.freeze_receipt_sha256),
  skip_revision: (rev0, rev1) => assigned(3, rev1.sha256, rev1.freeze_receipt_sha256),
  wrong_previous: (rev0, rev1) => assigned(2, hex('f'), rev1.freeze_receipt_sha256),
  missing_binding: (rev0, rev1) => {
    const crafted = assigned(2, rev1.sha256, rev1.freeze_receipt_sha256);
    delete crafted.migration_manifest_sha256;
    const { sha256, ...payload } = crafted;
    return { ...payload, sha256: digest(payload) };
  },
  bad_writer: (rev0, rev1) => assigned(2, rev1.sha256, rev1.freeze_receipt_sha256, { writer: 'puddingknowledge' }),
  rollback_without_evidence: (rev0, rev1) => assigned(2, rev1.sha256, rev1.freeze_receipt_sha256, { writer: 'puddingclaw' }),
  forward_with_evidence: (rev0, rev1) => assigned(2, rev1.sha256, rev1.freeze_receipt_sha256, { rollback: hex('d') }),
  receipt_mismatch: (rev0, rev1) => assigned(2, rev1.sha256, hex('e')),
};

for (const [name, build] of Object.entries(violations)) {
  test(`invalid assigned chain is rejected: ${name}`, async () => {
    const rev0 = existingWriter();
    const rev1 = suspended(1, rev0.sha256, hex('a'));
    const { home, cleanup } = await enrolled([rev0, rev1, build(rev0, rev1)]);
    try {
      await assert.rejects(
        admitHomeWrite(home, 'fixture', async () => { throw new Error('callback ran'); }),
        (error) => error.code === 'installation_authority_rejected');
    } finally { await cleanup(); }
  });
}
