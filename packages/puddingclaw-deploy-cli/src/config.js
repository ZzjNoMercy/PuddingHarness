import { CliError, assertArgument } from "./errors.js";
import { readJson, writeJsonAtomic } from "./store.js";

// Harness is the only deployable runtime in this package. Product-specific
// services are configured by their own products and are not part of this
// deployment document or lifecycle.
export const PROFILES = Object.freeze({ harness: Object.freeze({}) });
export const EXTENSIONS = Object.freeze([]);
export const DEFAULT_BACKEND_PORT = 8888;
export const DEFAULT_FRONTEND_PORT = 3000;

export function defaultConfig({
  profile = "harness",
  backendPort = DEFAULT_BACKEND_PORT,
  frontendPort = DEFAULT_FRONTEND_PORT,
} = {}) {
  assertArgument(profile === "harness", `unknown profile: ${profile}`);
  return {
    schema_version: 1,
    initialized: false,
    profile: "harness",
    release: { channel: "stable" },
    server: {
      host: "127.0.0.1",
      backend_port: backendPort,
      frontend_port: frontendPort,
      port_conflict: "ask",
      auto_open: false,
    },
    provider: {
      status: "unconfigured", id: "", name: "", protocol: "openai_compatible", base_url: "", model: "",
    },
    multimodal_provider: {
      status: "unconfigured", id: "", name: "", protocol: "openai_compatible", base_url: "", model: "",
      reuse_primary_credential: false,
    },
    infrastructure: {
      catalog: {
        mode: "sqlite", provider: "sqlite", source: "local_file", host: "", port: 0, database: "", probe_status: "skipped",
      },
    },
    harness: {
      sandbox_mode: "auto",
      model_call_limit: { enabled: true, run_limit: 50, exit_behavior: "end" },
      goals: { enabled: true, max_rounds: 8 },
    },
  };
}

export async function loadConfig(file) {
  const config = await readJson(file, null);
  if (!config) return null;
  const defaults = defaultConfig({
    backendPort: config.server?.backend_port || DEFAULT_BACKEND_PORT,
    frontendPort: config.server?.frontend_port || DEFAULT_FRONTEND_PORT,
  });
  // Selectively copy the Harness contract. This drops legacy product fields
  // whenever a deployment document is read instead of reactivating them.
  return {
    ...defaults,
    schema_version: config.schema_version,
    initialized: Boolean(config.initialized),
    ...(config.initialized_at ? { initialized_at: config.initialized_at } : {}),
    ...(config.runtime ? { runtime: config.runtime } : {}),
    ...(config.release ? { release: { ...defaults.release, ...config.release } } : {}),
    server: { ...defaults.server, ...config.server },
    provider: { ...defaults.provider, ...config.provider },
    multimodal_provider: { ...defaults.multimodal_provider, ...config.multimodal_provider },
    infrastructure: {
      catalog: { ...defaults.infrastructure.catalog, ...config.infrastructure?.catalog },
    },
    harness: {
      ...defaults.harness,
      ...config.harness,
      model_call_limit: { ...defaults.harness.model_call_limit, ...config.harness?.model_call_limit },
      goals: { ...defaults.harness.goals, ...config.harness?.goals },
    },
  };
}

export async function saveConfig(file, config) {
  validateConfig(config);
  await writeJsonAtomic(file, config);
}

export function validateConfig(config) {
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new CliError("config must be a JSON object", { code: "configuration_error" });
  }
  if (config.schema_version !== 1) throw new CliError("unsupported config schema_version", { code: "configuration_error" });
  for (const key of ["backend_port", "frontend_port"]) {
    const value = config.server?.[key];
    if (!Number.isInteger(value) || value < 1 || value > 65535) {
      throw new CliError(`server.${key} must be an integer between 1 and 65535`, { code: "configuration_error" });
    }
  }
  if (config.server.backend_port === config.server.frontend_port) {
    throw new CliError("server backend and frontend ports must differ", { code: "configuration_error" });
  }
  if (!["127.0.0.1", "::1", "localhost"].includes(config.server.host)) {
    throw new CliError("server.host must be a loopback address in this release", { code: "configuration_error" });
  }
  if (!["ask", "error", "auto"].includes(config.server.port_conflict)) {
    throw new CliError("server.port_conflict must be ask, error, or auto", { code: "configuration_error" });
  }
  if (typeof config.server.auto_open !== "boolean") throw new CliError("server.auto_open must be boolean", { code: "configuration_error" });
  if (!config.provider || !["unconfigured", "configured", "needs_action"].includes(config.provider.status)) {
    throw new CliError("provider.status must be unconfigured, configured, or needs_action", { code: "configuration_error" });
  }
  if (!config.multimodal_provider
      || !["unconfigured", "configured", "needs_action"].includes(config.multimodal_provider.status)) {
    throw new CliError("multimodal_provider.status must be unconfigured, configured, or needs_action", { code: "configuration_error" });
  }
  if (typeof config.multimodal_provider.reuse_primary_credential !== "boolean") {
    throw new CliError("multimodal_provider.reuse_primary_credential must be boolean", { code: "configuration_error" });
  }
  if (!config.infrastructure || !["sqlite", "postgresql"].includes(config.infrastructure.catalog?.mode)) {
    throw new CliError("infrastructure.catalog.mode must be sqlite or postgresql", { code: "configuration_error" });
  }
  if (config.infrastructure.catalog?.provider !== undefined
      && !["sqlite", "postgresql"].includes(config.infrastructure.catalog.provider)) {
    throw new CliError("infrastructure.catalog.provider must be sqlite or postgresql", { code: "configuration_error" });
  }
  if (config.infrastructure.catalog?.source !== undefined
      && !["local_file", "local", "native_apt", "docker", "external", "fallback"].includes(config.infrastructure.catalog.source)) {
    throw new CliError("infrastructure.catalog.source is invalid", { code: "configuration_error" });
  }
  if (!config.harness || !["auto", "spawn", "kernel"].includes(config.harness.sandbox_mode)) {
    throw new CliError("harness.sandbox_mode must be auto, spawn, or kernel", { code: "configuration_error" });
  }
}

export function getConfigValue(config, dottedPath) {
  assertArgument(Boolean(dottedPath), "config key is required");
  let current = config;
  for (const segment of dottedPath.split(".")) {
    if (!current || typeof current !== "object" || !Object.hasOwn(current, segment)) {
      throw new CliError(`unknown config key: ${dottedPath}`, { code: "configuration_error" });
    }
    current = current[segment];
  }
  return current;
}

export function setConfigValue(config, dottedPath, value) {
  assertArgument(Boolean(dottedPath), "config key is required");
  if (/(api[_-]?key|token|password|secret|credential)/i.test(dottedPath)) {
    throw new CliError("secrets cannot be written with config set", { code: "secret_rejected" });
  }
  const segments = dottedPath.split(".");
  let current = config;
  for (const segment of segments.slice(0, -1)) {
    if (!current?.[segment] || typeof current[segment] !== "object") {
      throw new CliError(`unknown config key: ${dottedPath}`, { code: "configuration_error" });
    }
    current = current[segment];
  }
  const leaf = segments.at(-1);
  if (!Object.hasOwn(current, leaf)) throw new CliError(`unknown config key: ${dottedPath}`, { code: "configuration_error" });
  current[leaf] = value;
  validateConfig(config);
  return config;
}
