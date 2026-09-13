import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import { probeRuntimeIdentity } from "../src/runtime-health.js";

async function server(handler) {
  const instance = http.createServer(handler);
  await new Promise((resolve) => instance.listen(0, "127.0.0.1", resolve));
  const address = instance.address();
  return { instance, url: `http://127.0.0.1:${address.port}` };
}

test("accepts only the exact managed backend identity", async () => {
  const { instance, url } = await server((request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ name: "PuddingHarness", role: "backend", instance_id: "instance-1" }));
  });
  try { assert.equal(await probeRuntimeIdentity(url, "instance-1", "backend"), true); }
  finally { instance.close(); }
});

for (const [name, handler] of [
  ["decoy old instance", (_request, response) => { response.writeHead(200); response.end("<html>old PuddingClaw</html>"); }],
  ["wrong role", (_request, response) => { response.writeHead(200); response.end(JSON.stringify({ name: "PuddingHarness", role: "frontend", instance_id: "instance-1" })); }],
  ["wrong instance", (_request, response) => { response.writeHead(200); response.end(JSON.stringify({ name: "PuddingHarness", role: "backend", instance_id: "old" })); }],
  ["redirect", (_request, response) => { response.writeHead(302, { location: "/real" }); response.end(); }],
  ["oversized", (_request, response) => { response.writeHead(200); response.end(JSON.stringify({ name: "PuddingHarness", role: "backend", instance_id: "instance-1", padding: "x".repeat(5000) })); }],
]) {
  test(`rejects ${name} response`, async () => {
    const { instance, url } = await server(handler);
    try { assert.equal(await probeRuntimeIdentity(url, "instance-1", "backend"), false); }
    finally { instance.close(); }
  });
}

test("uses frontend identity path and rejects extra fields", async () => {
  let requestedPath = "";
  const { instance, url } = await server((request, response) => {
    requestedPath = request.url;
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ name: "PuddingHarness", role: "frontend", instance_id: "instance-1", extra: true }));
  });
  try {
    assert.equal(await probeRuntimeIdentity(url, "instance-1", "frontend"), false);
    assert.equal(requestedPath, "/.puddingharness/health");
  } finally { instance.close(); }
});

for (const role of ["backend", "frontend"]) {
  test(`accepts actual ${role} health schema`, async () => {
    const { instance, url } = await server((_request, response) => {
      response.end(JSON.stringify({ name: "PuddingHarness", role, instance_id: "instance-1",
        ...(role === "backend" ? { version: "0.1.0", status: "running" } : {}) }));
    });
    try { assert.equal(await probeRuntimeIdentity(url, "instance-1", role), true); }
    finally { instance.closeAllConnections(); instance.close(); }
  });
}

test("rejects an oversized unfinished stream before EOF", async () => {
  const { instance, url } = await server((_request, response) => { response.write("x".repeat(5000)); });
  try {
    const started = performance.now();
    assert.equal(await probeRuntimeIdentity(url, "instance-1", "backend"), false);
    assert.ok(performance.now() - started < 1200);
  } finally { instance.closeAllConnections(); instance.close(); }
});

test("times out an unfinished small response", async () => {
  const { instance, url } = await server((_request, response) => { response.write("{"); });
  try { assert.equal(await probeRuntimeIdentity(url, "instance-1", "backend"), false); }
  finally { instance.closeAllConnections(); instance.close(); }
});
