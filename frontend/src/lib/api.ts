/**
 * Target Harness API client.
 *
 * This overlay keeps the Claw runtime contract: sessions, agents, tasks,
 * workspaces, MCP, permissions, files, and generic evaluation/review APIs.
 * Product-specific endpoint families live in the Platform Console and are
 * deliberately absent from this client.
 */

const API_BASE = "/api";

const DIRECT_BACKEND_API_BASE =
  process.env.NEXT_PUBLIC_BACKEND_API_BASE ||
  process.env.NEXT_PUBLIC_BACKEND_URL ||
  "http://localhost:8888/api";

function apiErrorMessage(text: string, fallback: string): string {
  if (!text) return fallback;
  try {
    const payload = JSON.parse(text) as { detail?: unknown; message?: unknown };
    const detail = payload.detail ?? payload.message;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const messages = detail
        .map((item) => {
          if (!item || typeof item !== "object") return "";
          const issue = item as { loc?: unknown; msg?: unknown };
          const location = Array.isArray(issue.loc) ? issue.loc.slice(1).join(".") : "";
          const message = typeof issue.msg === "string" ? issue.msg : "";
          return [location, message].filter(Boolean).join(": ");
        })
        .filter(Boolean);
      if (messages.length) return messages.join("；");
    }
    if (detail && typeof detail === "object" && "message" in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === "string" && message.trim()) return message;
    }
  } catch {
    // keep raw text below
  }
  return text;
}

async function fetchWithTimeout(input: RequestInfo | URL, init: RequestInit = {}, timeoutMs = 5000): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, {
      ...init,
      signal: init.signal ?? controller.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("状态刷新超时，后台任务仍会继续处理。");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export type SkillPlanStatus = "prepared" | "committed" | "cancelled" | "expired";

export interface SkillPlan {
  plan_id: string;
  plan_sha256: string;
  action: "install" | "update";
  skill_name: string;
  source: string;
  ref?: string;
  subpath?: string;
  created_at: number;
  expires_at: number;
  status: SkillPlanStatus;
  phase: "awaiting_confirmation" | "installed" | "cancelled" | "expired";
  requires_confirmation: boolean;
  installed: boolean;
  ui_commit_supported: boolean;
  diff?: {
    added?: string[];
    changed?: string[];
    removed?: string[];
    summary?: string;
  };
  staged_metadata?: Record<string, unknown>;
  installed_path?: string;
  installed_sha256?: string;
}

interface SkillPlanResponse {
  session_id: string;
  plan: SkillPlan;
  idempotent?: boolean;
  permission_recorded?: boolean;
}

export async function getSkillPlan(sessionId: string, planId: string): Promise<SkillPlan> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/skill-plans/${encodeURIComponent(planId)}`,
    { cache: "no-store" },
  );
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `加载 Skill 计划失败：${response.status}`));
  return (JSON.parse(text) as SkillPlanResponse).plan;
}

export async function commitSkillPlan(
  sessionId: string,
  planId: string,
  planSha256: string,
): Promise<SkillPlan> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/skill-plans/${encodeURIComponent(planId)}/commit`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_sha256: planSha256 }),
    },
  );
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `提交 Skill 计划失败：${response.status}`));
  return (JSON.parse(text) as SkillPlanResponse).plan;
}

export async function cancelSkillPlan(
  sessionId: string,
  planId: string,
  planSha256: string,
): Promise<SkillPlan> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/skill-plans/${encodeURIComponent(planId)}/cancel`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_sha256: planSha256 }),
    },
  );
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `取消 Skill 计划失败：${response.status}`));
  return (JSON.parse(text) as SkillPlanResponse).plan;
}

export interface SSEEvent {
  event: string;
  data: Record<string, unknown>;
  id?: string;
}

export type GoalStatus =
  | "active"
  | "paused"
  | "blocked"
  | "completed"
  | "cancelled"
  | "budget_exceeded";

export type RunStatus =
  | "preparing"
  | "running"
  | "waiting_hitl"
  | "evaluating"
  | "completed"
  | "cancelled"
  | "failed"
  | "blocked"
  | "budget_exceeded"
  | "verification_failed";

export interface VerificationCriterion {
  id: string;
  statement: string;
  source: string;
  verifier: string;
  required: boolean;
  evidence_scope: "run_only" | "goal_inheritable" | "artifact_bound" | "freshness_bound";
}

export interface RunVerificationContract {
  contract_id: string;
  version: string;
  task_type: string;
  criteria: VerificationCriterion[];
  rubric: string;
  verification_packs?: string[];
  activation_reasons?: Record<string, string[]>;
  browser_e2e_required?: boolean;
  base_contract_id?: string | null;
  created_at: number;
}

export interface RunTaskProfile {
  primary_intent: string;
  intents: string[];
  initial_packs: string[];
  available_context_refs: string[];
  reasons: string[];
}

export interface VerificationActivation {
  activation_id: string;
  run_id: string;
  query_id: string;
  tool_call_id: string;
  tool_name: string;
  pack: string;
  source: string;
  status: string;
  evidence_refs: Array<Record<string, unknown>>;
  created_at: number;
}

export interface CriterionEvaluation {
  criterion_id: string;
  name: string;
  passed: boolean | null;
  verifier: string;
  evidence: Array<Record<string, unknown>>;
  gap?: string | null;
  failure_kind?: "task_gap" | "infrastructure_error" | null;
}

export type VerificationStatus =
  | "not_required"
  | "pending"
  | "evaluating"
  | "satisfied"
  | "needs_revision"
  | "failed"
  | "max_iterations_reached"
  | "verification_incomplete"
  | "grader_error"
  | "infrastructure_error"
  | "budget_exceeded";

export interface RubricEvaluationReport {
  report_id: string;
  run_id: string;
  status: string;
  contract_id?: string | null;
  contract_version?: string | null;
  evaluations: CriterionEvaluation[];
  gaps: string[];
  explanation: string;
  iteration_count: number;
  verification_scope?: "run" | "goal_aggregate";
  supporting_run_ids?: string[];
  goal_revision?: number | null;
  accepted_for_goal_revision?: boolean | null;
  created_at: number;
}

export type RunReviewPolicy = "off" | "shadow" | "blocking_one_shot";

export type RunReviewStatus =
  | "not_requested"
  | "pending"
  | "running"
  | "satisfied"
  | "needs_revision"
  | "failed"
  | "grader_error"
  | "infrastructure_error"
  | "stale";

export interface RunReviewCriterionResult {
  criterion_id: string;
  name: string;
  passed: boolean | null;
  evidence_refs?: Array<Record<string, unknown>>;
  evidence?: Array<Record<string, unknown>>;
  gap?: string | null;
  failure_kind?: string | null;
}

export interface RunReviewVerificationRecord {
  verification_id: string;
  snapshot_id: string;
  method: "deterministic" | "environment" | "semantic_rubric";
  status: RunReviewStatus | "not_evaluated";
  criteria: RunReviewCriterionResult[];
  latency_ms?: number | null;
  verifier_model?: string | null;
  error_kind?: string | null;
  stale_reason?: string | null;
}

export interface RunReviewReport {
  report_id: string;
  run_id: string;
  snapshot_id: string;
  policy: Exclude<RunReviewPolicy, "off">;
  manual?: boolean;
  status: RunReviewStatus;
  verification_record_ids?: string[];
  operation_id?: string;
  attempt_no?: number;
  summary?: string;
  published_before_review?: boolean;
  created_at?: number;
  completed_at?: number | null;
  error_kind?: string | null;
  /** Public criterion results attached by the API; not part of the compact stored report. */
  verification_records?: RunReviewVerificationRecord[];
}

export interface RunReviewStatusResponse {
  status: RunReviewStatus;
  run_id: string;
  operation_id?: string;
  snapshot_id?: string;
  policy?: Exclude<RunReviewPolicy, "off">;
  manual?: boolean;
  report?: RunReviewReport;
}

export interface RunReviewVerificationOperation {
  operation_id: string;
  snapshot_id: string;
  method: "deterministic" | "environment" | "semantic_rubric";
  status: "pending" | "running" | "completed";
  attempt_no?: number;
}

export interface HarnessRun {
  run_id: string;
  query_id: string;
  session_id: string;
  objective: string;
  run_kind?: "goal_execution" | "goal_inspection" | "standalone";
  goal_id?: string | null;
  context_goal_id?: string | null;
  context_goal_revision?: number | null;
  goal_revision?: number | null;
  goal_turn_intent?: "inspect_goal" | "continue_goal" | "revise_goal" | "control_goal" | "standalone_task" | "clarify" | null;
  verification_enabled?: boolean;
  run_review_policy?: RunReviewPolicy;
  evaluation_snapshot_id?: string | null;
  run_review_report_id?: string | null;
  run_review_report?: RunReviewReport | null;
  task_profile?: RunTaskProfile;
  status: RunStatus;
  outcome?: string | null;
  declared_verification_contract?: RunVerificationContract | null;
  verification_contract?: RunVerificationContract | null;
  verification_activations?: VerificationActivation[];
  verification_report?: RubricEvaluationReport | null;
  delegation_contracts?: Array<Record<string, unknown>>;
  delegation_results?: Array<Record<string, unknown>>;
  delegation_events?: Array<{
    type?: string;
    status?: string;
    objective?: string;
    tool?: string;
    subagent_run_id?: string;
    timestamp?: number;
  }>;
  model_call_count: number;
  budget_exhaustion_reason?: string | null;
  error?: string | null;
  created_at: number;
  updated_at: number;
  completed_at?: number | null;
}

export interface HarnessGoal {
  goal_id: string;
  session_id: string;
  objective: string;
  objective_revision?: number;
  revisions?: Array<{
    revision: number;
    objective: string;
    contract_id?: string | null;
    created_at: number;
  }>;
  pending_revision?: boolean;
  status: GoalStatus;
  requested_status?: GoalStatus | null;
  current_run_id?: string | null;
  run_ids: string[];
  completion_policy?: "standard" | "rubric";
  latest_completion_request_id?: string | null;
  gaps: string[];
  control_notices?: string[];
  latest_verification_report_id?: string | null;
  latest_goal_decision?: {
    decision_id: string;
    goal_id: string;
    objective_revision: number;
    status: VerificationStatus;
    accepted?: boolean;
    supporting_run_ids: string[];
    criterion_provenance?: Array<Record<string, unknown>>;
    evidence_ref_count: number;
    gaps: string[];
    accepted_run_id?: string | null;
    report_id?: string | null;
    created_at: number;
  } | null;
  round: number;
  max_rounds: number;
  model_call_count: number;
  budget_exhaustion_reason?: string | null;
  created_at: number;
  updated_at: number;
  completed_at?: number | null;
}

export interface SessionHarnessState {
  session_id: string;
  runs: Record<string, HarnessRun>;
  run_order: string[];
  latest_run_id?: string | null;
  goals: Record<string, HarnessGoal>;
  goal_order: string[];
  active_goal_id?: string | null;
  run_review_reports?: Record<string, RunReviewReport>;
  verification_records?: Record<string, RunReviewVerificationRecord>;
  verification_operations?: Record<string, RunReviewVerificationOperation>;
}

export interface ToolContextJobStatus {
  id?: string;
  status:
    | "idle"
    | "pending"
    | "running"
    | "completed"
    | "completed_with_errors"
    | "failed"
    | "expired";
  completed_count?: number;
  failed_count?: number;
  error?: string;
  revision?: number;
}

export async function getToolContextJobStatus(sessionId: string): Promise<ToolContextJobStatus> {
  const response = await fetchWithTimeout(
    `${API_BASE}/agent/tool-context/status/${encodeURIComponent(sessionId)}`,
    { cache: "no-store" },
  );
  if (!response.ok) throw new Error(`Failed to get Tool Context status: ${response.status}`);
  return response.json();
}

export interface AgentAttachment {
  type: "image" | "pdf" | "spreadsheet" | "markdown" | "text" | "document" | "file";
  id?: string;
  name?: string;
  mime_type?: string;
  path?: string;
  size?: number;
  source?: "upload" | "paste" | "generated";
  sha256?: string;
  derived_from?: string;
  created_by_run_id?: string;
  created_by_query_id?: string;
  created_by_tool_call_id?: string;
  created_by_goal_id?: string;
  created_by_goal_revision?: number;
  download_url?: string;
  preview_url?: string;
  preview_mime_type?: string;
  width?: number;
  height?: number;
 created_at?: number;
}
export async function uploadAgentAttachments(
  files: File[],
  sessionId: string,
  source: "upload" | "paste" = "upload"
): Promise<AgentAttachment[]> {
  const form = new FormData();
  form.append("session_id", sessionId);
  form.append("source", source);
  files.forEach((file) => form.append("files", file, file.name));
  const response = await fetch(`${API_BASE}/attachments`, {
    method: "POST",
    body: form,
  });
  if (!response.ok) {
    throw new Error(`Attachment upload failed: ${response.status}`);
  }
  const payload = await response.json();
  return Array.isArray(payload.attachments) ? payload.attachments : [];
}

export interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled" | "error";
  position?: number;
  parent_id?: string | null;
  created_at?: number;
  updated_at?: number;
  metadata?: Record<string, unknown>;
}

export interface TraceSpan {
  id: string;
  parent_id: string | null;
  type:
    | "root"
    | "llm"
    | "model_input"
    | "tool"
    | "reasoning"
    | "todo"
    | "custom"
    | "rag"
    | "graph"
    | "middleware"
    | "memory"
    | "skill"
    | "subagent"
    | "permission";
  name: string;
  started_at: number;
  completed_at: number | null;
  status: "running" | "completed" | "error";
  input: unknown;
  output: unknown;
  metadata?: Record<string, unknown>;
  children?: TraceSpan[];
}

export interface TraceRuntimeMiddlewareEntry {
  name: string;
  order?: number;
  stack_order?: number;
  execution_order?: number;
  source?: string;
  hooks?: string[];
  note?: string;
}

export interface TraceRuntimeInventory {
  middleware?: {
    stack?: TraceRuntimeMiddlewareEntry[];
    hooks?: Record<string, TraceRuntimeMiddlewareEntry[]>;
    order_rule?: Record<string, string>;
  };
  filesystem?: {
    mounts?: Array<{
      virtual_path: string;
      root_dir?: string;
      exists?: boolean;
      role?: string;
    }>;
  };
  tools?: Array<{
    name: string;
    source?: string;
    description?: string;
  }>;
  skills?: Array<{
    name: string;
    description?: string;
    location?: string;
    system_prompt_source?: string;
    in_system_prompt?: boolean;
    href?: string;
  }>;
  subagents?: Array<{
    name: string;
    enabled?: boolean;
    model?: string;
    description?: string;
    route_trigger?: string;
    tools_mode?: string;
    skills_mode?: string;
    href?: string;
  }>;
  package_versions?: Record<string, string>;
}

export interface TraceMiddlewareEffect {
  id: string;
  category: string;
  title: string;
  hook?: string | null;
  middleware?: string[];
  before?: unknown;
  after?: unknown;
  diff?: Record<string, unknown>;
  evidence?: string[];
  metadata?: Record<string, unknown>;
  created_at?: number;
}

export interface TraceMiddlewareInvocation {
  id: string;
  hook: string;
  middleware?: string[];
  category?: string | null;
  title: string;
  invocation_index: number;
  sequence: number;
  status: "changed" | "read" | "noop" | "error" | string;
  evidence?: string[];
  before?: unknown;
  after?: unknown;
  diff?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  flow_ref?: Record<string, unknown>;
  created_at?: number;
}

export interface TraceHookBoundarySnapshot {
  id: string;
  hook: string;
  phase: "before" | "after" | string;
  title: string;
  snapshot?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  evidence?: string[];
  created_at?: number;
  sequence?: number;
}

export interface AgentTrace {
  trace_id: string;
  query_id?: string;
  session_id: string;
  started_at: number;
  completed_at: number | null;
  status: "running" | "completed" | "error";
  runtime_inventory?: TraceRuntimeInventory;
  middleware_effects?: TraceMiddlewareEffect[];
  middleware_invocations?: TraceMiddlewareInvocation[];
  hook_boundary_snapshots?: TraceHookBoundarySnapshot[];
  spans: TraceSpan[];
}

export interface GraphNode {
  id: string;
  type?: string;
  data?: unknown;
}

export interface GraphEdge {
  source: string;
  target: string;
}

export interface GraphStructure {
  nodes: GraphNode[];
  edges: GraphEdge[];
  mermaid?: string;
  mermaid_png_data_url?: string;
}

export interface PermissionGrant {
  id: string;
  type: string;
  scope: "once" | "session" | "project" | string;
  target_kind: "exact_file" | "all_external_files" | string;
  target: string;
  capabilities: string[];
  source?: string;
  created_at?: number;
  revoked_at?: number;
  consumed_at?: number;
  binding_schema_version?: number;
  semantic_key?: string;
  stable_bindings?: Record<string, unknown>;
  runtime_observations?: Record<string, unknown>;
  superseded_at?: number;
  superseded_by?: string;
  supersede_reason?: string;
  metadata?: {
    tool_name?: string;
    command?: string;
    reason?: string;
    risk?: string;
    policy_source?: string;
    policy_explanation?: string;
    control_descriptor?: Record<string, string>;
    session_scope_label?: string;
    session_target?: string;
    run_id?: string;
    change_preview?: Record<string, string>;
  };
}

export interface SessionPermissionState {
  grants: PermissionGrant[];
  history: PermissionGrant[];
}

export interface PermissionRequest {
  id: string;
  type: string;
  session_id: string;
  query_id?: string;
  tool_call_id?: string;
  path?: string;
  paths?: string[];
  authority_plane?: "shell" | string;
  grant_specs?: Array<{
    target: string;
    access: "read" | "write" | string;
    delete?: boolean;
    capabilities?: string[];
  }>;
  target_kind?: string;
  capabilities?: string[];
  operation?: string;
  tool_name?: string;
  command?: string;
  reason?: string;
  risk?: string;
  policy_source?: string;
  policy_explanation?: string;
  control_descriptor?: Record<string, string>;
  fingerprint?: string;
  semantic_key?: string;
  session_target_kind?: string;
  session_target?: string;
  session_scope_label?: string;
  options?: string[];
  change_preview?: Record<string, string>;
  status?: string;
}

export interface KernelFallbackRequest {
  id: string;
  request_id?: string;
  version: number;
  type: "kernel_fallback" | string;
  session_id: string;
  run_id: string;
  query_id?: string;
  project_id?: string | null;
  configured_mode: "kernel";
  fallback_runner: "spawn";
  platform?: string;
  availability_class: "stable" | "transient";
  reason_code: string;
  reason: string;
  probe_fingerprint: string;
  options?: Array<"switch_project_to_spawn" | "fallback_once" | "reject" | string>;
  status?: string;
}

export async function resolveKernelFallbackRequest(
  sessionId: string,
  requestId: string,
  requestVersion: number,
  action: "switch_project_to_spawn" | "fallback_once" | "reject",
): Promise<void> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/kernel-fallback-requests/${encodeURIComponent(requestId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_version: requestVersion, action }),
    },
  );
  const text = await response.text();
  if (!response.ok) throw new Error(apiErrorMessage(text, `处理 Kernel 回退请求失败：${response.status}`));
}

export interface UserInputOption {
  id: string;
  label: string;
  description?: string;
  recommended?: boolean;
}

export interface UserInputQuestion {
  id: string;
  prompt: string;
  type: "single_select" | "multi_select" | "text";
  options?: UserInputOption[];
  required?: boolean;
  allow_other?: boolean;
  min_selections?: number;
  max_selections?: number | null;
  max_length?: number;
}

export interface UserInputRequest {
  id: string;
  version: number;
  type: "user_input" | string;
  session_id: string;
  query_id: string;
  run_id: string;
  goal_id?: string | null;
  goal_revision?: number | null;
  tool_call_id?: string;
  status: "pending" | "resolved" | "cancelled" | string;
  title: string;
  reason: string;
  questions: UserInputQuestion[];
  allow_agent_decide?: boolean;
  decision?: {
    action?: "submit" | "cancel" | "agent_decide" | string;
    answers?: UserInputAnswer[];
  };
}

export interface UserInputAnswer {
  question_id: string;
  option_ids: string[];
  text: string;
}

export async function resolveUserInputRequest(
  sessionId: string,
  requestId: string,
  payload: {
    request_version: number;
    action: "submit" | "cancel" | "agent_decide";
    answers?: UserInputAnswer[];
  },
): Promise<{ request_id: string; decision: Record<string, unknown>; resumed: boolean }> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/user-input-requests/${encodeURIComponent(requestId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, "Failed to resolve user input request"));
  }
  return response.json();
}

export interface SkillSecretRequest {
  id: string;
  version: number;
  type: "skill_secret" | string;
  session_id: string;
  query_id: string;
  run_id: string;
  status: "pending" | "resolved" | "cancelled" | string;
  skill_id: string;
  skill_version: string;
  env_name: string;
  reason: string;
  mode: "enter" | "reuse";
  decision?: { action?: "configured" | "cancel" | string; env_name?: string };
}

export async function resolveSkillSecretRequest(
  sessionId: string,
  requestId: string,
  payload: {
    request_version: number;
    action: "configure" | "reuse" | "cancel";
    secret_value?: string;
  },
): Promise<{ request_id: string; decision: Record<string, unknown>; resumed: boolean }> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/skill-secret-requests/${encodeURIComponent(requestId)}/resolve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(apiErrorMessage(text, "Failed to configure Skill Secret"));
  }
  return response.json();
}
export async function* streamAgent(
  message: string,
  sessionId: string,
  projectId?: string | null,
  signal?: AbortSignal,
  userId?: string,
  attachments?: AgentAttachment[],
  goalMode = false,
  goalId?: string | null,
  contextGoalId?: string | null,
  goalControlAction?: "start" | null,
  skillHints?: string[],
  llmModelId?: string | null,
  thinkingLevel?: "low" | "high" | "max" | null,
  credentialName?: string | null,
  runReviewPolicy?: RunReviewPolicy | null,
): AsyncGenerator<SSEEvent> {
  const response = await fetch(`${API_BASE}/agent`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      user_id: userId || "default_user",
      project_id: projectId || null,
      attachments: attachments || [],
      skill_hints: skillHints ?? null,
      llm_model_id: llmModelId || null,
      thinking_level: thinkingLevel || null,
      credential_name: credentialName || null,
      run_review_policy: runReviewPolicy || null,
      goal_mode: goalMode,
      goal_id: goalMode ? goalId || null : null,
      context_goal_id: contextGoalId || null,
      goal_control_action: goalControlAction || null,
      stream: true
    }),
    signal,
  });

  if (!response.ok) {
    throw new Error(`Agent API error: ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n/g, "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";

    const parsedFrames = frames
      .map((frame) => parseSSEFrame(frame))
      .filter((event): event is SSEEvent => event !== null);

    for (const parsed of parsedFrames) {
      yield parsed;
    }
  }
}
export async function requestRunReview(
  sessionId: string,
  runId: string,
): Promise<RunReviewStatusResponse> {
  const response = await fetch(
    `${API_BASE}/agent/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/review`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    },
  );
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `无法开始回答验收：${response.status}`));
  }
  return (text ? JSON.parse(text) : { status: "pending", run_id: runId }) as RunReviewStatusResponse;
}
export async function getRunReviewStatus(
  sessionId: string,
  runId: string,
): Promise<RunReviewStatusResponse> {
  const response = await fetch(
    `${API_BASE}/agent/sessions/${encodeURIComponent(sessionId)}/runs/${encodeURIComponent(runId)}/review`,
    { cache: "no-store" },
  );
  const text = await response.text();
  if (!response.ok) {
    throw new Error(apiErrorMessage(text, `无法读取回答验收状态：${response.status}`));
  }
  return (text ? JSON.parse(text) : { status: "not_requested", run_id: runId }) as RunReviewStatusResponse;
}
export async function* streamSessionEvents(
  sessionId: string,
  signal?: AbortSignal,
  after = 0,
): AsyncGenerator<SSEEvent> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/events?after=${Math.max(0, after)}`,
    { cache: "no-store", signal },
  );
  if (response.status === 204) return;
  if (!response.ok) throw new Error(`Session event API error: ${response.status}`);
  const reader = response.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n/g, "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const parsed = parseSSEFrame(frame);
      if (parsed) yield parsed;
    }
  }
  buffer += decoder.decode();
  const tail = parseSSEFrame(buffer.trim());
  if (tail) yield tail;
}
export async function getSessionHarnessState(
  sessionId: string,
): Promise<SessionHarnessState> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/harness`,
    { cache: "no-store" },
  );
  if (!response.ok) {
    throw new Error(`Failed to get Harness state: ${response.status}`);
  }
  return response.json();
}
async function transitionGoal(
  sessionId: string,
  goalId: string,
  action: "pause" | "resume" | "cancel",
): Promise<HarnessGoal> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/goals/${encodeURIComponent(goalId)}/${action}`,
    { method: "POST" },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new Error(apiErrorMessage(text, `Failed to ${action} Goal: ${response.status}`));
  }
  return response.json();
}
export const pauseGoal = (sessionId: string, goalId: string) =>
  transitionGoal(sessionId, goalId, "pause");
export const resumeGoal = (sessionId: string, goalId: string) =>
  transitionGoal(sessionId, goalId, "resume");
export const cancelGoal = (sessionId: string, goalId: string) =>
  transitionGoal(sessionId, goalId, "cancel");
export async function extendGoalBudget(
  sessionId: string,
  goalId: string,
  additionalRounds: number,
): Promise<HarnessGoal> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/goals/${encodeURIComponent(goalId)}/extend-budget`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ additional_rounds: additionalRounds }),
    },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new Error(apiErrorMessage(text, `Failed to extend Goal budget: ${response.status}`));
  }
  return response.json();
}
export async function updateGoalObjective(
  sessionId: string,
  goalId: string,
  objective: string,
  expectedRevision: number,
): Promise<HarnessGoal> {
  const response = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/goals/${encodeURIComponent(goalId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        objective,
        expected_revision: expectedRevision,
      }),
    },
  );
  if (!response.ok) {
    const text = await response.text();
    throw new Error(apiErrorMessage(text, `Failed to update Goal: ${response.status}`));
  }
  return response.json();
}
function parseSSEFrame(frame: string): SSEEvent | null {
  let event = "message";
  let id: string | undefined;
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
    } else if (line.startsWith("id:")) {
      id = line.slice(3).trim() || undefined;
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  try {
    const data = JSON.parse(dataLines.join("n"));
    return { event, data, ...(id ? { id } : {}) };
  } catch {
    return null;
  }
}
export async function readFile(path: string): Promise<string> {
  const resp = await fetch(`${API_BASE}/files?path=${encodeURIComponent(path)}`);
  if (!resp.ok) throw new Error(`Failed to read file: ${resp.status}`);
  const data = await resp.json();
  return data.content;
}
export async function saveFile(path: string, content: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, content }),
  });
  if (!resp.ok) throw new Error(`Failed to save file: ${resp.status}`);
}
export async function listSessions(): Promise<
  Array<{
    id: string;
    title: string;
    updated_at: number;
    runtime_mode?: "agent" | "chat";
    project_id?: string | null;
    project_path?: string | null;
    workspace_type?: string;
    workspace_path?: string;
    session_source?: string;
    llm_model_id?: string | null;
    thinking_level?: "low" | "high" | "max" | null;
    credential_name?: string | null;
    run_review_policy?: RunReviewPolicy | null;
    approval_mode?: ApprovalMode;
    policy_epoch?: number;
    policy_version?: string;
  }>
> {
  const resp = await fetch(`${API_BASE}/sessions`);
  if (!resp.ok) throw new Error(`Failed to list sessions: ${resp.status}`);
  const data = await resp.json();
  return data.sessions;
}
export interface SessionSearchResult {
  id: string;
  title: string;
  updated_at: number;
  runtime_mode?: "agent" | "chat";
  project_id?: string | null;
  project_path?: string | null;
  snippet: string;
  matched_in: "title" | "content";
}
export async function searchSessions(
  query: string,
  signal?: AbortSignal,
): Promise<SessionSearchResult[]> {
  const params = new URLSearchParams({
    q: query.trim(),
    limit: "50",
  });
  const resp = await fetch(`${API_BASE}/sessions/search?${params.toString()}`, {
    cache: "no-store",
    signal,
  });
  if (!resp.ok) throw new Error(`Failed to search sessions: ${resp.status}`);
  const data = await resp.json() as { results?: SessionSearchResult[] };
  return Array.isArray(data.results) ? data.results : [];
}
export interface ProjectMeta {
  project_id: string;
  name: string;
  path: string;
  created_at: number;
  updated_at: number;
  pinned?: boolean;
  execution_mode?: "spawn" | "kernel" | null;
  trust_state: "pending" | "trusted" | "denied";
  identity_digest?: string;
}
export async function listProjects(): Promise<ProjectMeta[]> {
  const resp = await fetch(`${API_BASE}/projects`);
  if (!resp.ok) throw new Error(`Failed to list projects: ${resp.status}`);
  const data = await resp.json();
  return data.projects;
}
export async function registerProject(path: string, name?: string): Promise<ProjectMeta> {
  const resp = await fetch(`${API_BASE}/projects/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // Selecting or pasting a local directory in PuddingClaw is the user's
    // explicit authorization of that exact workspace identity.
    body: JSON.stringify({ path, name, authorize: true }),
  });
  if (!resp.ok) throw new Error(`Failed to register project: ${resp.status}`);
  return resp.json();
}
export async function openProject(projectId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/open`, {
    method: "POST",
  });
  if (!resp.ok) throw new Error(`Failed to open project: ${resp.status}`);
}
export async function openLocalFile(path: string, sessionId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/local-files/open`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path, session_id: sessionId }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(apiErrorMessage(text, `Failed to open file: ${resp.status}`));
  }
}
export async function updateProject(
  projectId: string,
  update: { name?: string; pinned?: boolean; execution_mode?: "spawn" | "kernel" }
): Promise<ProjectMeta> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(update),
  });
  if (!resp.ok) throw new Error(`Failed to update project: ${resp.status}`);
  return resp.json();
}
export async function setProjectTrust(
  projectId: string,
  state: "pending" | "trusted" | "denied",
): Promise<ProjectMeta> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/trust`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state }),
  });
  if (!resp.ok) {
    const responseText = await resp.text();
    throw new Error(apiErrorMessage(responseText, `Failed to update project trust: ${resp.status}`));
  }
  return resp.json();
}
export async function removeProject(projectId: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw new Error(`Failed to remove project: ${resp.status}`);
}
export async function listSessionPermissions(sessionId: string): Promise<SessionPermissionState> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions`);
  if (!resp.ok) throw new Error(`Failed to list permissions: ${resp.status}`);
  const data = await resp.json();
  return {
    grants: Array.isArray(data.grants) ? data.grants : [],
    history: Array.isArray(data.history) ? data.history : [],
  };
}
export async function grantExternalFilePermission(
  sessionId: string,
  targetKind: "exact_file" | "exact_directory" | "all_external_files",
  path?: string,
  permissionRequestId?: string,
  scope?: "run" | "session",
): Promise<PermissionGrant> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/external-files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      target_kind: targetKind,
      path,
      permission_request_id: permissionRequestId,
      scope,
    }),
  });
  if (!resp.ok) throw new Error(`Failed to grant external file permission: ${resp.status}`);
  const data = await resp.json();
  return data.grant;
}
export async function grantToolActionPermission(
  sessionId: string,
  permissionRequestId: string,
  scope: "once" | "session" | "project",
): Promise<PermissionGrant> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/tool-actions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        permission_request_id: permissionRequestId,
        scope,
      }),
    },
  );
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(apiErrorMessage(text, `Failed to grant Tool permission: ${resp.status}`));
  }
  const data = await resp.json();
  return data.grant;
}
export async function grantShellDirectoryPermission(
  sessionId: string,
  permissionRequestId: string,
  scope: "run" | "session",
): Promise<PermissionGrant[]> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/shell-directories`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        permission_request_id: permissionRequestId,
        scope,
      }),
    },
  );
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(apiErrorMessage(text, `Failed to grant shell directory permission: ${resp.status}`));
  }
  const data = await resp.json();
  return Array.isArray(data.grants) ? data.grants : [];
}
export async function revokePermissionGrant(sessionId: string, grantId: string): Promise<void> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/${encodeURIComponent(grantId)}/revoke`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to revoke permission: ${resp.status}`);
}
export async function denyPermissionRequest(
  sessionId: string,
  permissionRequestId: string,
  message?: string
): Promise<void> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/deny`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ permission_request_id: permissionRequestId, message }),
  });
  if (!resp.ok) throw new Error(`Failed to deny permission: ${resp.status}`);
}
export type ApprovalMode = "strict" | "smart";

export interface CreateSessionOptions {
  llm_model_id?: string | null;
  thinking_level?: "low" | "high" | "max" | null;
  credential_name?: string | null;
  run_review_policy?: RunReviewPolicy | null;
  approval_mode?: ApprovalMode;
  runtime_mode?: "agent";
  project_id?: string | null;
}

export interface PermissionModeState {
  session_id: string;
  approval_mode: ApprovalMode;
  policy_epoch: number;
  policy_version: string;
}

export async function createSession(options: CreateSessionOptions = {}): Promise<{
  id: string;
  title: string;
  created_at?: number;
  updated_at?: number;
  runtime_mode?: "agent";
  project_id?: string | null;
  llm_model_id?: string | null;
  thinking_level?: "low" | "high" | "max" | null;
  credential_name?: string | null;
  run_review_policy?: RunReviewPolicy | null;
  approval_mode: ApprovalMode;
  policy_epoch: number;
  policy_version: string;
}> {
  const resp = await fetch(`${API_BASE}/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(options),
  });
  if (!resp.ok) throw new Error(`Failed to create session: ${resp.status}`);
  return resp.json();
}

export async function updateSessionLlmSelection(
  sessionId: string,
  llmModelId: string,
  thinkingLevel: "low" | "high" | "max" | null,
  credentialName: string | null = null,
): Promise<{
  id: string;
  llm_model_id?: string | null;
  thinking_level?: "low" | "high" | "max" | null;
  credential_name?: string | null;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/llm-selection`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        llm_model_id: llmModelId,
        thinking_level: thinkingLevel,
        credential_name: credentialName,
      }),
    },
  );
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to update conversation model: ${resp.status}`);
  }
  return resp.json();
}

export async function updateSessionRunReviewPolicy(
  sessionId: string,
  runReviewPolicy: RunReviewPolicy | null,
): Promise<{
  id: string;
  run_review_policy?: RunReviewPolicy | null;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/run-review-policy`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run_review_policy: runReviewPolicy }),
    },
  );
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `Failed to update Run review policy: ${resp.status}`);
  }
  return resp.json();
}

export async function getSessionApprovalMode(
  sessionId: string,
): Promise<PermissionModeState> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/mode`,
    { cache: "no-store" },
  );
  if (!resp.ok) throw new Error(`Failed to get approval mode: ${resp.status}`);
  return resp.json();
}

export async function updateSessionApprovalMode(
  sessionId: string,
  approvalMode: ApprovalMode,
  expectedEpoch?: number,
): Promise<PermissionModeState> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/permissions/mode`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        approval_mode: approvalMode,
        expected_epoch: expectedEpoch,
      }),
    },
  );
  if (!resp.ok) {
    const payload = await resp.json().catch(() => null) as { detail?: unknown } | null;
    const detail = typeof payload?.detail === "string" ? payload.detail : "";
    const localized = resp.status === 409
      ? detail.toLowerCase().includes("active run")
        ? "当前 Run 仍在进行，完成后才能切换授权模式。"
        : "授权模式已在其他位置更新，请刷新后重试。"
      : resp.status === 404
        ? "当前会话已不存在，请新建会话后重试。"
        : "授权模式更新失败，请稍后重试。";
    throw new Error(localized);
  }
  return resp.json();
}

export async function renameSession(id: string, title: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  if (!resp.ok) throw new Error(`Failed to rename session: ${resp.status}`);
}

export async function deleteSession(id: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/sessions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
  if (!resp.ok) throw new Error(`Failed to delete session: ${resp.status}`);
}

export async function getRawMessages(
  sessionId: string
): Promise<{
  session_id: string;
  title: string;
  messages: Array<{ role: string; content: string }>;
  todos?: TodoItem[];
  todos_authority?: { kind: "legacy" | "none" | "goal" | "run"; goal_id?: string; goal_revision?: number; run_id?: string };
  todo_ledger_revision?: number;
  graph?: GraphStructure | null;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/messages`
  );
  if (!resp.ok) throw new Error(`Failed to get raw messages: ${resp.status}`);
  return resp.json();
}

export async function getSessionTraces(
  sessionId: string
): Promise<{
  session_id: string;
  trace?: AgentTrace | null;
  traces: Record<string, AgentTrace>;
  latest_query_id?: string | null;
  latest_trace_id?: string | null;
  todos?: TodoItem[];
  todos_authority?: { kind: "legacy" | "none" | "goal" | "run"; goal_id?: string; goal_revision?: number; run_id?: string };
  todo_ledger_revision?: number;
  graph?: GraphStructure | null;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/traces`
  );
  if (!resp.ok) throw new Error(`Failed to get session traces: ${resp.status}`);
  return resp.json();
}

/** Historical read-only compatibility: legacy transcript fields remain opaque. */
export async function getSessionHistory(
  sessionId: string
): Promise<{
  session_id: string;
  todos?: TodoItem[];
  todos_authority?: { kind: "legacy" | "none" | "goal" | "run"; goal_id?: string; goal_revision?: number; run_id?: string };
  todo_ledger_revision?: number;
  graph?: GraphStructure | null;
  headless_pending_input?: {
    status?: string;
    run_id?: string | null;
    query_id?: string | null;
    requests?: PermissionRequest[];
    updated_at?: number;
  };
  messages: Array<{
    role: string;
    content: string;
    created_at?: number;
    query_id?: string;
    attachments?: AgentAttachment[];
    output_attachments?: AgentAttachment[];
    reasoning_content?: string;
    tool_calls?: Array<{ tool: string; input?: string; output?: string }>;
  }>;
}> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/history`
  );
  if (!resp.ok) throw new Error(`Failed to get session history: ${resp.status}`);
  return resp.json();
}

export interface CurrentTodosSnapshot {
  session_id: string;
  todos: TodoItem[];
  authority: { kind: "legacy" | "none" | "goal" | "run"; goal_id?: string; goal_revision?: number; run_id?: string };
  ledger_revision: number;
}

export async function getCurrentSessionTodos(sessionId: string): Promise<CurrentTodosSnapshot> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/todos/current`,
    { cache: "no-store" },
  );
  if (!resp.ok) throw new Error(`Failed to get current Todos: ${resp.status}`);
  return resp.json();
}

export async function listSkills(): Promise<
  Array<{ name: string; path: string; description: string }>
> {
  const resp = await fetch(`${API_BASE}/skills`);
  if (!resp.ok) throw new Error(`Failed to list skills: ${resp.status}`);
  const data = await resp.json();
  return data.skills;
}

export async function listMcpServers(): Promise<
  Array<{ key: string; name: string; url: string; transport: string }>
> {
  const resp = await fetch(`${API_BASE}/mcp/servers`);
  if (!resp.ok) throw new Error(`Failed to list MCP servers: ${resp.status}`);
  const data = await resp.json();
  return data.servers;
}

export interface McpServerConfig {
  name?: string;
  transport: "stdio" | "sse" | "streamable-http";
  url?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  headers?: Record<string, string>;
  timeout?: number;
}

export interface McpConfig {
  enabled: string[];
  servers: Record<string, McpServerConfig>;
}

export interface McpConfigPayload {
  path: string;
  config: McpConfig;
  credential_vault?: { readable: boolean; error: string };
}

export async function getMcpConfig(): Promise<McpConfigPayload> {
  const resp = await fetch(`${API_BASE}/mcp/config`, { cache: "no-store" });
  if (!resp.ok) throw new Error(`Failed to load MCP config: ${resp.status}`);
  return resp.json() as Promise<McpConfigPayload>;
}

export async function updateMcpConfig(config: McpConfig): Promise<McpConfigPayload> {
  const resp = await fetch(`${API_BASE}/mcp/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config }),
  });
  const data = await resp.json().catch(() => null) as { detail?: string } | null;
  if (!resp.ok) throw new Error(data?.detail || `Failed to save MCP config: ${resp.status}`);
  mcpStatusProbeInFlight = null;
  return data as McpConfigPayload;
}

export interface McpServerStatus {
  key: string;
  name: string;
  url: string;
  transport: string;
  enabled: boolean;
  auto_enabled: boolean;
  managed_by?: "mcp" | string;
  ready: boolean;
  loaded: boolean;
  status: "ready" | "loaded" | "not_ready" | "error";
  reason: string;
  tools: string[];
  tool_count: number;
}

export interface McpServersStatus {
  servers: Array<{ key: string; name: string; url: string; transport: string }>;
  catalog: McpServerStatus[];
}

let mcpStatusProbeInFlight: Promise<McpServersStatus> | null = null;
export async function getMcpServersStatus(probe = true): Promise<McpServersStatus> {
  if (probe && mcpStatusProbeInFlight) return mcpStatusProbeInFlight;
  const request = (async () => {
    const resp = await fetch(`${API_BASE}/mcp/servers?probe=${probe ? "true" : "false"}`, {
      cache: "no-store",
    });
    if (!resp.ok) throw new Error(`Failed to inspect MCP servers: ${resp.status}`);
    return resp.json() as Promise<McpServersStatus>;
  })();
  if (!probe) return request;
  mcpStatusProbeInFlight = request;
  try {
    return await request;
  } finally {
    if (mcpStatusProbeInFlight === request) mcpStatusProbeInFlight = null;
  }
}

export async function loadSkill(skillName: string): Promise<void> {
  const resp = await fetch(`${API_BASE}/skills/load`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skill_name: skillName }),
  });
  if (!resp.ok) throw new Error(`Failed to load skill: ${resp.status}`);
}

export async function generateTitle(
  sessionId: string
): Promise<{ title: string }> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/generate-title`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to generate title: ${resp.status}`);
  return resp.json();
}

export async function getSessionTokenCount(
  sessionId: string,
  runtimeMode?: "agent" | "chat",
): Promise<{
  system_tokens: number;
  message_tokens: number;
  total_tokens: number;
  compaction_trigger: number;
  percentage: number;
  measured: boolean;
}> {
  const params = new URLSearchParams();
  if (runtimeMode) params.set("runtime_mode", runtimeMode);
  const query = params.toString();
  const resp = await fetch(
    `${API_BASE}/tokens/session/${encodeURIComponent(sessionId)}${query ? `?${query}` : ""}`
  );
  if (!resp.ok) throw new Error(`Failed to get token count: ${resp.status}`);
  return resp.json();
}

export async function getFileTokenCounts(
  paths: string[]
): Promise<{ files: Array<{ path: string; tokens: number }> }> {
  const resp = await fetch(`${API_BASE}/tokens/files`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
  });
  if (!resp.ok) throw new Error(`Failed to get file token counts: ${resp.status}`);
  return resp.json();
}

export interface AgentCompactResult {
  status: "completed";
  session_id: string;
  operation_id: string;
  trigger: "manual";
  tokens_before: number;
  tokens_after: number;
  tokens_reduced: number;
  reduction_percentage: number;
  summarized_message_count: number;
  kept_recent_message_count: number;
  source_query_id: string;
  source_run_id: string;
  projection_version: number;
  summary_model: string;
}

interface AgentCompactOperation extends Omit<Partial<AgentCompactResult>, "status" | "session_id" | "operation_id"> {
  status: "running" | "completed" | "failed" | "expired";
  session_id: string;
  operation_id: string;
  started_at?: number;
  completed_at?: number;
  error?: string;
}

export async function compactAgentSession(
  sessionId: string,
  focus = "",
): Promise<AgentCompactResult> {
  const resp = await fetch(
    `${API_BASE}/agent/sessions/${encodeURIComponent(sessionId)}/compact`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ focus }),
    },
  );
  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(apiErrorMessage(text, `Agent 上下文压缩失败：${resp.status}`));
  }
  const operation = JSON.parse(text) as AgentCompactOperation;
  if (!operation.operation_id) {
    throw new Error("Agent 上下文压缩未返回 operation_id。");
  }

  const deadline = Date.now() + 16 * 60 * 1000;
  let consecutivePollFailures = 0;
  while (Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, 750));
    let terminalFailure: Error | null = null;
    try {
      const statusResp = await fetch(
        `${API_BASE}/agent/sessions/${encodeURIComponent(sessionId)}/compact/${encodeURIComponent(operation.operation_id)}`,
        { cache: "no-store" },
      );
      const statusText = await statusResp.text();
      if (!statusResp.ok) {
        throw new Error(apiErrorMessage(statusText, `读取压缩状态失败：${statusResp.status}`));
      }
      consecutivePollFailures = 0;
      const status = JSON.parse(statusText) as AgentCompactOperation;
      if (status.status === "completed") return status as AgentCompactResult;
      if (status.status === "failed" || status.status === "expired") {
        terminalFailure = new Error(
          status.error || `Agent 上下文压缩已${status.status === "expired" ? "超时" : "失败"}。`,
        );
      }
    } catch (error) {
      consecutivePollFailures += 1;
      if (consecutivePollFailures >= 5) throw error;
    }
    if (terminalFailure) throw terminalFailure;
  }
  throw new Error("等待 Agent 上下文压缩结果超时；可稍后重试或刷新 Session 状态。");
}

export async function clearSession(
  sessionId: string
): Promise<{ status: string; session_id: string }> {
  const resp = await fetch(
    `${API_BASE}/sessions/${encodeURIComponent(sessionId)}/clear`,
    { method: "POST" }
  );
  if (!resp.ok) throw new Error(`Failed to clear session: ${resp.status}`);
  return resp.json();
}
 
