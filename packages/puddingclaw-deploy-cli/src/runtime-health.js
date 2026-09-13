const MAX_HEALTH_BYTES = 4096;
const HEALTH_TIMEOUT_MS = 1500;

function endpoint(url, role) {
  const base = String(url).replace(/\/$/, "");
  return role === "frontend" ? `${base}/.puddingharness/health` : `${base}/`;
}

/** Probe the managed service identity without accepting redirects or partial JSON. */
export async function probeRuntimeIdentity(url, instanceId, role, { fetchImpl = globalThis.fetch } = {}) {
  if (!url || !instanceId || !["backend", "frontend"].includes(role)) return false;
  try {
    const response = await fetchImpl(endpoint(url, role), {
      redirect: "manual",
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
    if (response.status !== 200 || response.redirected) return false;
    if (!response.body) return false;
    const reader = response.body.getReader();
    const chunks = [];
    let length = 0;
    try {
      while (true) {
        const { done, value: chunk } = await reader.read();
        if (done) break;
        length += chunk.byteLength;
        if (length > MAX_HEALTH_BYTES) return false;
        chunks.push(chunk);
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const value = JSON.parse(new TextDecoder().decode(bytes));
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const allowed = role === "backend"
      ? ["instance_id", "name", "role", "version", "status"]
      : ["instance_id", "name", "role"];
    if (Object.keys(value).some((key) => !allowed.includes(key))) return false;
    if ("version" in value && (typeof value.version !== "string" || !value.version)) return false;
    if ("status" in value && value.status !== "running") return false;
    return value.name === "PuddingHarness" && value.role === role && value.instance_id === instanceId;
  } catch {
    return false;
  }
}

export const runtimeHealthLimits = Object.freeze({ maxBytes: MAX_HEALTH_BYTES, timeoutMs: HEALTH_TIMEOUT_MS });
