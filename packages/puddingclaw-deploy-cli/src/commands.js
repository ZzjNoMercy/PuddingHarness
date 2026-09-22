import { CliError, assertArgument } from "./errors.js";
import { getConfigValue, loadConfig, saveConfig, setConfigValue } from "./config.js";
import { parseConfigValue } from "./args.js";
import { probeHome, probeNode, probePlatform, probePort, probePython, probeUv } from "./probes.js";
import { readJson } from "./store.js";
import { mark } from "./output.js";
import { probeManagedRuntimeState, publicRuntimeState } from "./supervisor.js";

async function requireConfig(paths) {
  const config = await loadConfig(paths.config);
  if (!config) throw new CliError("Harness is not initialized; run puddingharness init", { code: "not_initialized", exitCode: 1 });
  return config;
}

export async function configCommand(args, paths) {
  const [action, key, rawValue] = args;
  const config = await requireConfig(paths);
  if (action === "show") return config;
  if (action === "get") return { key, value: getConfigValue(config, key) };
  if (action === "set") {
    assertArgument(key && rawValue !== undefined, "usage: config set <key> <value>");
    setConfigValue(config, key, parseConfigValue(rawValue));
    await saveConfig(paths.config, config);
    return { status: "updated", key, value: getConfigValue(config, key) };
  }
  throw new CliError("usage: config show | config get <key> | config set <key> <value>", { code: "argument_error" });
}

export async function statusCommand(paths) {
  const config = await loadConfig(paths.config);
  const runtime = await readJson(paths.runtimeState, null);
  const instance = await probeManagedRuntimeState(paths, runtime);
  return {
    schema_version: 1,
    initialized: Boolean(config?.initialized),
    home: paths.home,
    profile: config?.profile || null,
    instance,
    runtime: publicRuntimeState(runtime),
  };
}

export async function doctorCommand(paths) {
  const config = await loadConfig(paths.config);
  const runtime = await readJson(paths.runtimeState, null);
  const instance = await probeManagedRuntimeState(paths, runtime);
  const probes = [await probePlatform(), probeNode(), probePython(config?.runtime?.python?.command || ""), probeUv(), await probeHome(paths.home, { create: false })];
  if (config?.server) {
    for (const [name, port] of [["backend", config.server.backend_port], ["frontend", config.server.frontend_port]]) {
      const portProbe = await probePort(port, config.server.host);
      let runtimePort = null;
      try { runtimePort = Number(new URL(runtime?.[`${name}_url`]).port); } catch {}
      probes.push(portProbe.status === "occupied" && instance.status === "running" && runtimePort === port
        ? { ...portProbe, status: "managed", required: false, instance_id: runtime.instance_id } : portProbe);
    }
  }
  probes.push(instance);
  const blocking = probes.filter((probe) => probe.required && ["failed", "needs_action", "occupied"].includes(probe.status));
  const initialized = Boolean(config?.initialized);
  return { schema_version: 1, status: !initialized || blocking.length ? "needs_action" : "ok", initialized, home: paths.home, probes };
}

export function formatDoctor(result) {
  const deployment = result.deployment || result;
  const lines = ["Pudding Harness Doctor", `Home: ${deployment.home}`, "", "Deployment"];
  for (const probe of deployment.probes) {
    const detail = probe.version || probe.path || probe.selected?.version || probe.status;
    lines.push(`${mark(probe.status)} ${probe.probe.padEnd(24)} ${detail}`);
  }
  if (result.deployment) {
    lines.push("", "Agent API");
    const ready = result.configured === true && result.reachable === true;
    lines.push(`${ready ? "✓" : "!"} ${"connection".padEnd(24)} ${ready ? "local · reachable" : (result.error || "not ready")}`);
  }
  return lines.join("\n");
}
