import { CliError, assertArgument } from "./errors.js";
import { buildInitPlan } from "./init-schema.js";
import { defaultConfig, loadConfig, PROFILES, saveConfig } from "./config.js";

export async function inspectProfile(profile, paths) {
  assertArgument(Object.hasOwn(PROFILES, profile), `unknown profile: ${profile}`);
  const config = await loadConfig(paths.config);
  return {
    schema_version: 1,
    profile: "harness",
    label: "Agent Harness",
    current_profile: config?.profile || null,
    plan: buildInitPlan("harness"),
    initialized: Boolean(config?.initialized),
  };
}

export async function applyProfile(profile, paths) {
  assertArgument(Object.hasOwn(PROFILES, profile), `unknown profile: ${profile}`);
  const existing = await loadConfig(paths.config);
  const config = existing || defaultConfig();
  config.profile = "harness";
  await saveConfig(paths.config, config);
  return { status: "updated", profile: "harness", inspection: await inspectProfile("harness", paths) };
}

export async function profileCommand(args, paths) {
  const [action, profile] = args;
  assertArgument(action === "inspect" || action === "apply", "usage: profile inspect|apply harness");
  assertArgument(profile === "harness", `unknown profile: ${profile || ""}`);
  return action === "inspect" ? inspectProfile(profile, paths) : applyProfile(profile, paths);
}
