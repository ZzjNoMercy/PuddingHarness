#!/usr/bin/env node

import { createRequire } from "node:module";
import { parseArgs } from "./args.js";
import { CliError } from "./errors.js";
import { resolveHome, homePaths } from "./home.js";
import { runInit } from "./init.js";
import { configCommand, doctorCommand, formatDoctor, statusCommand } from "./commands.js";
import { logsCommand, requireRuntimeForStart, runtimeCommand } from "./runtime-commands.js";
import { openRuntime, startRuntime, stopRuntime } from "./supervisor.js";
import { writeError, writeHuman, writeJson } from "./output.js";
import { workerCommand, workerDoctorCommand } from "./worker-commands.js";
import { databaseCommand } from "./database-commands.js";
import { profileCommand } from "./profile-commands.js";

const { version: VERSION } = createRequire(import.meta.url)("../package.json");

const COMMAND_FLAGS = Object.freeze({
  init: [
    "api_key", "backend_port", "base_url", "database_create_if_missing", "database_mode",
    "database_name", "database_port", "database_url", "database_username",
    "force", "frontend_port", "install_runtime", "model",
    "multimodal_api_key", "multimodal_base_url", "multimodal_model", "multimodal_provider",
    "multimodal_provider_id", "multimodal_provider_name", "non_interactive", "plan", "port",
    "prepare_python", "profile", "provider", "provider_id", "provider_name", "python", "uv", "yes",
  ],
  database: [
    "confirm_empty_switch", "database_create_if_missing", "database_mode", "database_name",
    "database_port", "database_url", "database_username", "drain_timeout", "non_interactive",
    "skip_drain", "target_path", "url",
  ],
  profile: [],
  agent: ["export", "input_json", "jsonl", "session"],
  start: ["port"],
  stop: ["force"],
  restart: ["force", "port"],
  config: [],
  runtime: [],
  logs: [],
  open: [],
  status: [],
  doctor: [],
  version: [],
});

function assertCommandFlags(command, flags) {
  const accepted = new Set(["help", "json", ...(COMMAND_FLAGS[command] || [])]);
  const unknown = Object.keys(flags).find((name) => !accepted.has(name));
  if (unknown) {
    throw new CliError(`unknown option: --${unknown.replaceAll("_", "-")}`, {
      code: "argument_error",
    });
  }
}

function usage() {
  return [
    "Pudding Harness CLI",
    "",
    "Usage:",
    "  puddingharness init [--profile harness] [--port auto] [--python /path] [--uv /path] [--prepare-python] [--install-runtime]",
    "  puddingharness init --profile harness --plan --json",
    "  puddingharness config show|get|set ...",
    "  puddingharness profile inspect|apply harness [--json]",
    "  puddingharness database show|configure",
    "  puddingharness database migrate sqlite-to-postgres --url <pg-url> [--skip-drain] [--drain-timeout <s>]",
    "  puddingharness database migrate postgres-to-sqlite [--target-path <path>] [--skip-drain] [--drain-timeout <s>]",
    "  puddingharness agent run <message> [--session <id>] [--export <dir>] [--json|--jsonl]",
    "  puddingharness agent respond <run_id> --input-json - [--json|--jsonl]",
    "  puddingharness agent cancel <run_id> [--json]",
    "  puddingharness agent models list [--json]",
    "  puddingharness agent capabilities [--json]",
    "  puddingharness runtime install <bundle-directory|bundled>",
    "  puddingharness runtime prepare",
    "  puddingharness runtime inspect",
    "  puddingharness runtime prune",
    "  puddingharness logs [--json]",
    "  puddingharness start [--port auto] [--json]",
    "  puddingharness stop [--force] [--json]",
    "  puddingharness restart [--force] [--port auto] [--json]",
    "  puddingharness open [--json]",
    "  puddingharness doctor [--json]",
    "  puddingharness status [--json]",
    "  puddingharness version [--json]",
  ].join("\n");
}

async function main({ positionals, flags }) {
  const [command, ...rest] = positionals;
  const paths = homePaths(resolveHome());
  if (!command || command === "help" || flags.help) return { value: usage(), humanOnly: true, code: 0 };
  assertCommandFlags(command, flags);
  if (command === "version") {
    return {
      value: {
        schema_version: "1",
        cli: "puddingharness",
        cli_version: VERSION,
        agent_id: "puddingharness",
        protocol_version: "1",
      },
      code: 0,
    };
  }
  if (command === "agent") {
    const [agentCommand, ...agentArgs] = rest;
    if (!agentCommand || agentCommand === "help") {
      return { value: usage(), humanOnly: true, code: 0 };
    }
    if (["run", "respond", "cancel", "models", "capabilities"].includes(agentCommand)) {
      return workerCommand(agentCommand, agentArgs, flags, paths);
    }
    throw new CliError(`unknown agent command: ${agentCommand}`, { code: "argument_error" });
  }
  if (command === "init") return { value: await runInit({ flags, paths }), code: 0 };
  if (command === "config") return { value: await configCommand(rest, paths), code: 0 };
  if (command === "profile") return { value: await profileCommand(rest, paths), code: 0 };
  if (command === "database") return { value: await databaseCommand(rest, flags, paths), code: 0 };
  if (command === "runtime") return { value: await runtimeCommand(rest, paths), code: 0 };
  if (command === "logs") return { value: await logsCommand(paths), code: 0 };
  if (command === "start") {
    await requireRuntimeForStart(paths);
    return { value: await startRuntime(paths, { automaticPorts: flags.port === "auto" }), code: 0 };
  }
  if (command === "stop") return { value: await stopRuntime(paths, { force: Boolean(flags.force) }), code: 0 };
  if (command === "restart") {
    const previous = await stopRuntime(paths, { force: Boolean(flags.force) });
    await requireRuntimeForStart(paths);
    const started = await startRuntime(paths, { automaticPorts: flags.port === "auto" });
    return { value: { status: "restarted", previous, runtime: started.runtime }, code: 0 };
  }
  if (command === "open") return { value: await openRuntime(paths), code: 0 };
  if (command === "status") return { value: await statusCommand(paths), code: 0 };
  if (command === "doctor") {
    const deployment = await doctorCommand(paths);
    const worker = await workerDoctorCommand(paths);
    const workerReady = worker.value.configured === true
      && worker.value.reachable === true;
    const value = {
      ...worker.value,
      schema_version: "1",
      cli_version: VERSION,
      agent_id: "puddingharness",
      protocol_version: "1",
      status: workerReady ? "ok" : "needs_action",
      deployment,
    };
    return {
      value,
      human: formatDoctor(value),
      code: workerReady ? 0 : (worker.code || (deployment.status === "ok" ? 2 : 1)),
    };
  }
  throw new CliError(`unknown command: ${command}`, { code: "argument_error" });
}

try {
  const parsed = parseArgs(process.argv.slice(2));
  const result = await main(parsed);
  process.exitCode = result.code;
  if (result.suppressOutput) {
    // Streaming commands already emitted their protocol events.
  } else if (parsed.flags.json || result.forceJson) writeJson(result.value);
  else if (result.humanOnly) writeHuman(result.value);
  else if (result.human) writeHuman(result.human);
  else writeHuman(JSON.stringify(result.value, null, 2));
} catch (error) {
  const cliError = error instanceof CliError
    ? error
    : new CliError(error?.message || String(error), { code: "internal_error" });
  process.exitCode = cliError.exitCode;
  if (process.argv.includes("--json")) {
    const outcomeCodes = new Set(["session_expired", "interaction_expired", "interaction_conflict", "run_expired"]);
    writeJson({
      schema_version: "1",
      status: "error",
      ...(outcomeCodes.has(cliError.code) ? { outcome: cliError.code } : {}),
      error_code: cliError.code,
      ...(Number(cliError.status) > 0 ? { http_status: Number(cliError.status) } : {}),
      error: cliError.message,
      ...(cliError.details && !Number(cliError.status) ? { details: cliError.details } : {}),
    });
  } else {
    writeError(cliError.message);
  }
}
