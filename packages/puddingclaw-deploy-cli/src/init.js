import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { CliError } from "./errors.js";
import { DEFAULT_BACKEND_PORT, DEFAULT_FRONTEND_PORT, defaultConfig, saveConfig } from "./config.js";
import { integerFlag } from "./args.js";
import { findFreePort, probeHome, probeNode, probePlatform, probePort, probePython, probeUv } from "./probes.js";
import { prepareManagedPython } from "./python-runtime.js";
import { readJson } from "./store.js";
import { buildInitPlan } from "./init-schema.js";
import { embeddedRuntimeStatus } from "./runtime-commands.js";
import { installRuntimeBundle } from "./runtime-bundle.js";
import { prepareRuntimePython } from "./runtime-python.js";
import { bootstrapUv } from "./uv-runtime.js";
import { discoverCoreDatabase, discoverInitialMultimodalProvider, discoverInitialProvider, validatePreparedInfrastructure } from "./init-discovery.js";
import { readSecret, writeSecret } from "./secrets.js";

async function confirmSummary(summary) {
  const rl = createInterface({ input, output });
  try {
    output.write("\n即将写入 Harness 配置：\n");
    output.write(`  Home:     ${summary.home}\n`);
    output.write(`  Backend:  127.0.0.1:${summary.backend_port}\n`);
    output.write(`  Frontend: 127.0.0.1:${summary.frontend_port}\n`);
    output.write(`  Provider: ${summary.provider?.name || "稍后配置"}${summary.provider?.model ? ` / ${summary.provider.model}` : ""}\n`);
    const answer = String(await rl.question("\n确认初始化？[Y/n] ")).trim().toLowerCase();
    return answer === "" || answer === "y" || answer === "yes";
  } finally { rl.close(); }
}

async function confirmPythonPreparation() {
  const rl = createInterface({ input, output });
  try {
    output.write("\n未找到兼容的 Python 3.11/3.12。\n");
    const answer = String(await rl.question("现在使用 uv 在独立 Home 中准备？[Y/n] ")).trim().toLowerCase();
    return answer === "" || answer === "y" || answer === "yes";
  } finally { rl.close(); }
}

async function confirmRuntimeInstallation(releaseVersion) {
  const rl = createInterface({ input, output });
  try {
    output.write(`\n安装 Harness Runtime ${releaseVersion}。\n`);
    const answer = String(await rl.question("现在安装 Runtime？[Y/n] ")).trim().toLowerCase();
    return answer === "" || answer === "y" || answer === "yes";
  } finally { rl.close(); }
}

export async function runInit({ flags, paths, interactive = process.stdin.isTTY && process.stdout.isTTY }) {
  const nonInteractive = Boolean(flags.non_interactive) || !interactive;
  const profile = String(flags.profile || "harness").trim() || "harness";
  if (profile !== "harness") throw new CliError("only the harness profile is supported", { code: "argument_error" });
  const settingsPlan = buildInitPlan("harness");
  if (flags.plan) return { status: "plan", ...settingsPlan };

  const existing = await readJson(paths.config, null);
  if (existing && !flags.force) throw new CliError("Harness is already initialized; pass --force to replace non-secret config", { code: "already_initialized", exitCode: 1 });
  const homeProbe = await probeHome(paths.home, { create: true });
  if (homeProbe.status !== "available") throw new CliError(homeProbe.reason || "deploy home is unavailable", { code: "home_unavailable", exitCode: 1, details: homeProbe });

  const providerDiscovery = await discoverInitialProvider({ flags, nonInteractive });
  const multimodalProviderDiscovery = await discoverInitialMultimodalProvider({ flags, nonInteractive, primaryDiscovery: providerDiscovery });
  const databaseDiscovery = await discoverCoreDatabase({
    profile: "harness", flags, nonInteractive, home: paths.home,
    existingDatabaseUrl: existing && flags.force ? await readSecret(paths.databaseUrl) : "",
    existingCatalog: existing && flags.force ? existing.infrastructure?.catalog : null,
    promptWhenUnspecified: false,
  });

  let pythonProbe = probePython(flags.python);
  let uvProbe = probeUv(flags.uv);
  if (pythonProbe.status !== "available" && (flags.prepare_python || (!nonInteractive && await confirmPythonPreparation()))) {
    if (uvProbe.status !== "available") {
      const bootstrapped = await bootstrapUv(paths.home);
      uvProbe = { ...probeUv(bootstrapped.selected.command), selected: bootstrapped.selected };
    }
    const prepared = await prepareManagedPython(paths.home, { uvCommand: uvProbe.selected.command });
    pythonProbe = { probe: "runtime.python", status: "available", required: true, selected: prepared.selected, interpreters: [prepared.selected], remediation: [] };
  }
  if (pythonProbe.status !== "available") throw new CliError("compatible Python 3.11/3.12 was not found", { code: "python_required", exitCode: 1, details: { python: pythonProbe, uv: uvProbe } });

  const selectedPorts = await selectPorts({
    backendPort: flags.backend_port ? integerFlag(flags.backend_port, "--backend-port") : DEFAULT_BACKEND_PORT,
    frontendPort: flags.frontend_port ? integerFlag(flags.frontend_port, "--frontend-port") : DEFAULT_FRONTEND_PORT,
    automatic: flags.port === "auto",
  });
  const { backendPort, frontendPort, initialBackendProbe, initialFrontendProbe } = selectedPorts;
  const summary = {
    profile: "harness", home: paths.home, provider: providerDiscovery.provider, multimodal_provider: multimodalProviderDiscovery.provider,
    infrastructure: { catalog: databaseDiscovery.catalog }, backend_port: backendPort, frontend_port: frontendPort,
    probes: { platform: await probePlatform(), node: probeNode(), python: pythonProbe, uv: uvProbe, home: homeProbe,
      requested_backend_port: initialBackendProbe, requested_frontend_port: initialFrontendProbe,
      provider: providerDiscovery.probe, multimodal_provider: multimodalProviderDiscovery.probe, database: databaseDiscovery.probes },
    settings_plan: { selected_steps: settingsPlan.execution_order, disabled_steps: [] },
  };
  if (!nonInteractive && !flags.yes && !(await confirmSummary(summary))) throw new CliError("initialization cancelled", { code: "cancelled", exitCode: 1 });

  const config = defaultConfig({ backendPort, frontendPort });
  config.initialized = true;
  config.initialized_at = new Date().toISOString();
  config.runtime = { python: pythonProbe.selected, uv: uvProbe.selected };
  config.provider = { ...config.provider, ...providerDiscovery.provider };
  config.multimodal_provider = { ...config.multimodal_provider, ...multimodalProviderDiscovery.provider };
  config.infrastructure.catalog = databaseDiscovery.catalog;
  if (providerDiscovery.apiKey) await writeSecret(paths.providerApiKey, providerDiscovery.apiKey);
  if (multimodalProviderDiscovery.apiKey) await writeSecret(paths.multimodalProviderApiKey, multimodalProviderDiscovery.apiKey);
  if (databaseDiscovery.databaseUrl) await writeSecret(paths.databaseUrl, databaseDiscovery.databaseUrl);
  await saveConfig(paths.config, config);

  const embedded = await embeddedRuntimeStatus();
  if (flags.install_runtime && !embedded.available) throw new CliError("this npm package does not contain an embedded runtime", { code: "runtime_not_bundled", exitCode: 1 });
  const installRequested = embedded.available && (flags.install_runtime || (!nonInteractive && await confirmRuntimeInstallation(embedded.release_version)));
  let runtime = { status: embedded.available ? "available" : "not_bundled" };
  if (installRequested) {
    try {
      const installed = await installRuntimeBundle(embedded.path, paths);
      runtime = await prepareAndValidate(installed, { paths, config, databaseDiscovery });
    } catch (error) {
      if (error?.code !== "runtime_already_installed") throw error;
      runtime = await prepareAndValidate({ status: "already_installed" }, { paths, config, databaseDiscovery });
    }
  }
  return { schema_version: 1, status: "initialized", ...summary, config_path: paths.config, runtime };
}

async function prepareAndValidate(installed, { paths, databaseDiscovery }) {
  const prepared = await prepareRuntimePython(paths, { allowUvBootstrap: true });
  const validation = validatePreparedInfrastructure({ python: prepared.python, databaseUrl: databaseDiscovery.databaseUrl, createDatabaseIfMissing: databaseDiscovery.createDatabaseIfMissing, requirePgvector: false });
  const databaseProbe = validation.find((probe) => probe.probe === "database.connection");
  if (databaseProbe?.code === "runtime_dependency_missing") throw new CliError(`Runtime 缺少 PostgreSQL 驱动：${databaseProbe.reason}`, { code: "runtime_dependency_missing", exitCode: 1 });
  return { status: "prepared", installed, prepared, infrastructure_validation: validation };
}

export async function selectPorts({ backendPort, frontendPort, automatic, probe = probePort, findFree = findFreePort }) {
  const initialBackendProbe = await probe(backendPort);
  const initialFrontendProbe = await probe(frontendPort);
  let selectedBackend = backendPort;
  let selectedFrontend = frontendPort;
  if (initialBackendProbe.status !== "available") {
    if (!automatic) throw new CliError(`backend port ${backendPort} is occupied`, { code: "port_occupied", exitCode: 1, details: initialBackendProbe });
    selectedBackend = await findFree(backendPort + 1);
  }
  if (initialFrontendProbe.status !== "available" || selectedFrontend === selectedBackend) {
    if (!automatic) throw new CliError(`frontend port ${frontendPort} is occupied`, { code: "port_occupied", exitCode: 1, details: initialFrontendProbe });
    selectedFrontend = await findFree(Math.max(frontendPort + 1, selectedBackend + 1));
  }
  return { backendPort: selectedBackend, frontendPort: selectedFrontend, initialBackendProbe, initialFrontendProbe };
}
