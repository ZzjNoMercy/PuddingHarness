import { PROFILES } from "./config.js";
import { CliError } from "./errors.js";

const STEPS = Object.freeze([
  ["provider.agent", "Agent 模型与 Provider"],
  ["provider.multimodal", "图片分析模型"],
  ["database.shared", "核心数据库（SQLite 本地默认 / PostgreSQL 可选）"],
  ["harness.context", "上下文工程"],
  ["harness.prompt_cache", "Prompt 缓存"],
  ["harness.completion", "Goal 与验收"],
  ["harness.terminal", "终端执行"],
  ["harness.runtime", "运行保护"],
  ["harness.subagents", "SubAgent"],
].map(([id, label]) => Object.freeze({ id, label, extension: null })));

const DEPENDENCIES = Object.freeze({
  "harness.context": ["provider.agent"],
  "harness.completion": ["provider.agent"],
  "harness.subagents": ["provider.agent", "provider.multimodal"],
});

export function buildInitPlan(profile = "harness") {
  if (!Object.hasOwn(PROFILES, profile)) throw new CliError(`unknown profile: ${profile}`, { code: "argument_error" });
  const steps = STEPS.map((step) => ({ ...step, depends_on: DEPENDENCIES[step.id] || [], status: "selected" }));
  return {
    schema_version: 1,
    profile: "harness",
    execution_order: steps.map((step) => step.id),
    branches: { database: ["sqlite_local_default", "postgresql_if_explicit", "sqlite_fallback_on_unreachable"] },
    steps,
  };
}
