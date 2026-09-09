import os from "node:os";
import path from "node:path";
import { CliError } from "./errors.js";

export const HOME_NAME = ".puddingharness";

export function resolveHome(env = process.env) {
  const configured = String(env.PUDDINGHARNESS_HOME || "").trim();
  const candidate = configured || path.join(os.homedir(), HOME_NAME);
  if (!path.isAbsolute(candidate)) {
    throw new CliError("PUDDINGHARNESS_HOME must be an absolute path", {
      code: "configuration_error",
    });
  }
  return path.resolve(candidate);
}

export function homePaths(home) {
  return {
    home,
    config: path.join(home, "deploy.json"),
    productConfig: path.join(home, "config.json"),
    runtimeState: path.join(home, "runtime.json"),
    initState: path.join(home, "init-state.json"),
    providerApiKey: path.join(home, "secrets", "initial-provider-api-key"),
    multimodalProviderApiKey: path.join(home, "secrets", "initial-multimodal-provider-api-key"),
    databaseUrl: path.join(home, "secrets", "database-url"),
    logs: path.join(home, "logs"),
    runtime: path.join(home, "runtime"),
    toolchains: path.join(home, "toolchains"),
  };
}
