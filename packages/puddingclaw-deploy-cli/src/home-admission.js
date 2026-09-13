import fs from "node:fs/promises";
import fsSync from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { AsyncLocalStorage } from "node:async_hooks";
import { CliError } from "./errors.js";

const admissionContext = new AsyncLocalStorage();
const GATE = ".installation-cli-admission";
const LEASES = ".installation-cli-leases";
const FREEZE = ".installation-freeze-v1.json";
const FREEZE_PART = `${FREEZE}.part`;
const TICKET_RE = /^[0-9a-f-]{36}\.json$/i;
const OPERATION_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;

function reject(message, code = "installation_admission_rejected") {
  throw new CliError(message, { code, exitCode: 1 });
}

async function assertPrivatePath(target, { allowMissing = true } = {}) {
  const absolute = path.resolve(target);
  if (!path.isAbsolute(absolute)) reject("installation Home must be absolute", "configuration_error");
  if (absolute === path.parse(absolute).root) reject("installation Home must not be the filesystem root", "configuration_error");
  try {
    const targetInfo = await fs.lstat(absolute);
    if (targetInfo.isSymbolicLink()) reject("installation Home is symlinked");
  } catch (error) {
    if (!(error?.code === "ENOENT" && allowMissing)) throw error;
  }
  // macOS commonly exposes /tmp and /var as system symlinks. Canonicalize
  // those path components, while still rejecting a symlink Home itself.
  let probe = absolute;
  const suffix = [];
  while (true) {
    try {
      const canonical = await fs.realpath(probe);
      return path.join(canonical, ...suffix.reverse());
    } catch (error) {
      if (error?.code !== "ENOENT" || !allowMissing || probe === path.dirname(probe)) throw error;
      suffix.push(path.basename(probe));
      probe = path.dirname(probe);
    }
  }
}

async function statPrivateDirectory(target, { missingOk = false } = {}) {
  try {
    const info = await fs.lstat(target);
    if (!info.isDirectory() || info.isSymbolicLink() || info.uid !== process.getuid?.() || (info.mode & 0o77)) {
      reject("installation admission directory is not private");
    }
    return info;
  } catch (error) {
    if (missingOk && error?.code === "ENOENT") return null;
    throw error;
  }
}

async function syncDirectory(directory) {
  const handle = await fs.open(directory, fsSync.constants.O_RDONLY | (fsSync.constants.O_DIRECTORY || 0));
  try { await handle.sync(); } finally { await handle.close(); }
}

async function ensureHome(home) {
  const canonical = await assertPrivatePath(home);
  let existing;
  try {
    existing = await fs.lstat(canonical);
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  if (!existing) {
    await fs.mkdir(canonical, { recursive: true, mode: 0o700 });
    existing = await fs.lstat(canonical);
  }
  if (!existing.isDirectory() || existing.isSymbolicLink() || existing.uid !== process.getuid?.()) {
    reject("installation Home must be an owned real directory");
  }
  if (existing.mode & 0o77) reject("installation Home must be private");
  return canonical;
}

async function assertNoFreezeMarker(home) {
  for (const name of [FREEZE, FREEZE_PART]) {
    try {
      await fs.lstat(path.join(home, name));
      reject("installation is frozen; CLI writes are denied", "installation_frozen");
    } catch (error) {
      if (error instanceof CliError) throw error;
      if (error?.code !== "ENOENT") throw error;
    }
  }
}

async function acquireGate(home) {
  const gate = path.join(home, GATE);
  try {
    await fs.mkdir(gate, { mode: 0o700 });
  } catch (error) {
    if (error?.code === "EEXIST") reject("installation admission gate is busy or stale", "installation_admission_busy");
    throw error;
  }
  try {
    await statPrivateDirectory(gate);
  } catch (error) {
    await fs.rmdir(gate).catch(() => {});
    throw error;
  }
  return gate;
}

async function releaseGate(home, gate) {
  await fs.rmdir(gate);
  await syncDirectory(home);
}

async function createTicket(home, operation) {
  const leases = path.join(home, LEASES);
  const existing = await statPrivateDirectory(leases, { missingOk: true });
  if (!existing) {
    await fs.mkdir(leases, { mode: 0o700 });
    await fs.chmod(leases, 0o700);
  }
  await statPrivateDirectory(leases);
  const ticket = path.join(leases, `${randomUUID()}.json`);
  const payload = JSON.stringify({ format: "puddingharness-installation-cli-ticket/v1", operation, pid: process.pid }) + "\n";
  const handle = await fs.open(
    ticket,
    fsSync.constants.O_WRONLY | fsSync.constants.O_CREAT | fsSync.constants.O_EXCL | (fsSync.constants.O_NOFOLLOW || 0),
    0o600,
  );
  try {
    await handle.writeFile(payload, "utf8");
    await handle.sync();
  } finally { await handle.close(); }
  await fs.chmod(ticket, 0o600);
  await syncDirectory(leases);
  await syncDirectory(home);
  return ticket;
}

async function removeTicket(home, ticket) {
  const leases = path.join(home, LEASES);
  const resolved = path.resolve(ticket);
  if (!resolved.startsWith(`${path.resolve(leases)}${path.sep}`) || !TICKET_RE.test(path.basename(resolved))) {
    reject("installation admission ticket is invalid");
  }
  await fs.unlink(resolved);
  await syncDirectory(leases);
  await syncDirectory(home);
}

async function waitForChildren(context) {
  while (context.pending > 0) {
    await new Promise((resolve) => context.waiters.push(resolve));
  }
}

async function runNested(context, callback) {
  context.pending += 1;
  try {
    return await callback();
  } catch (error) {
    context.failed = true;
    context.failure = context.failure || error;
    throw error;
  } finally {
    context.pending -= 1;
    if (context.pending === 0) {
      for (const resolve of context.waiters.splice(0)) resolve();
    }
  }
}

// Explicit implementation that keeps ticket cleanup in the same async context.
export async function admitHomeWrite(homeValue, operation, callback) {
  if (process.platform === "win32") {
    reject("CLI write admission requires POSIX; Windows writes are unavailable", "installation_admission_unsupported");
  }
  const active = admissionContext.getStore();
  const requestedHome = path.resolve(homeValue);
  if (active && !active.revoked && active.originalHome === requestedHome) {
    return runNested(active, callback);
  }
  const home = await assertPrivatePath(homeValue);
  if (active && !active.revoked && active.home === home) return runNested(active, callback);
  if (typeof operation !== "string" || !OPERATION_RE.test(operation)) reject("invalid installation operation", "argument_error");
  // Check a pre-existing freeze marker before any gate/Home mutation. This
  // preserves an existing frozen Home's mode even when it is legacy 0755.
  for (const name of [FREEZE, FREEZE_PART]) {
    try {
      await fs.lstat(path.join(home, name));
      reject("installation is frozen; CLI writes are denied", "installation_frozen");
    } catch (error) {
      if (error instanceof CliError) throw error;
      if (error?.code !== "ENOENT") throw error;
    }
  }
  await ensureHome(home);
  const gate = await acquireGate(home);
  let ticket;
  try {
    await assertNoFreezeMarker(home);
    ticket = await createTicket(home, operation);
  } finally {
    await releaseGate(home, gate);
  }
  const context = { home, originalHome: requestedHome, ticket, pending: 0, waiters: [], revoked: false, failed: false };
  let result;
  let failure;
  let failed = false;
  try {
    result = await admissionContext.run(context, callback);
  } catch (error) {
    failed = true;
    failure = error;
  }
  context.revoked = true;
  await waitForChildren(context);
  if (!failed && !context.failed) {
    const cleanupGate = await acquireGate(home);
    try {
      await assertNoFreezeMarker(home);
      await removeTicket(home, ticket);
    } finally {
      await releaseGate(home, cleanupGate);
    }
  }
  if (failed) throw failure;
  if (context.failed) throw context.failure;
  return result;
}

export const withHomeAdmission = admitHomeWrite;

export const admissionNames = Object.freeze({ GATE, LEASES, FREEZE, FREEZE_PART });
