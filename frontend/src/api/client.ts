// BookMind frontend API client — thin fetch wrappers for the /api/* surface.
// Migrated from the vanilla ESM client.js to TypeScript. All calls are
// same-origin; the session cookie is sent automatically. The learner is derived
// from the cookie server-side — the browser never sends a user_id.

import type {
  AnswerResult,
  ApiMessage,
  LearningSourceView,
  Conversation,
  ConversationActivity,
  ConversationSummary,
  ConsolidationFilter,
  ConsolidationMode,
  ConsolidationQueue,
  ConceptLearningRecord,
  JobView,
  LearningSummary,
  KnowledgeGraph,
  MisconceptionView,
  Project,
  SendMessageResult,
  EventType,
  User,
} from "../types/blocks";

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public canRetry?: boolean,
    public action?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

const BASE = "";
const REQUEST_TIMEOUT_MS = 55_000;

async function json<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let r: Response;
  try {
    r = await fetch(BASE + path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      credentials: "same-origin",
      ...opts,
      signal: controller.signal,
    });
  } catch (error) {
    if ((error as Error).name === "AbortError") {
      throw new ApiError(
        "REQUEST_TIMEOUT",
        "这次处理等待太久，页面已经恢复，可以直接重试。",
        true,
      );
    }
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
  if (r.status === 401) {
    // No session — bootstrap transparently, then retry once.
    await bootstrap();
    return json<T>(path, opts);
  }
  const body = (await r.json().catch(() => ({}))) as {
    error?: { code?: string; message?: string; can_retry?: boolean; action?: string };
  };
  if (!r.ok) {
    const err = body.error || { message: `HTTP ${r.status}` };
    throw new ApiError(
      err.code || "HTTP_ERROR",
      err.message || `HTTP ${r.status}`,
      err.can_retry,
      err.action,
    );
  }
  return body as T;
}

export function bootstrap(): Promise<User> {
  return json("/api/session/bootstrap", { method: "POST" });
}

export function getMe(): Promise<User> {
  return json("/api/me");
}

export function listProjects(): Promise<Project[]> {
  return json("/api/projects");
}

export interface CreateLearningSpaceInput {
  name: string;
  goal?: string;
  learning_scope?: string;
  deadline?: string;
  current_plan?: string;
}

export function createProject(input: CreateLearningSpaceInput): Promise<Project> {
  return json("/api/projects", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getProject(pid: string): Promise<Project> {
  return json(`/api/projects/${pid}`);
}

export function updateProject(
  pid: string,
  body: {
    name?: string;
    goal?: string;
    learning_scope?: string;
    deadline?: string;
    current_plan?: string;
    last_source_id?: string;
    last_source_page?: number;
    default_mode?: string;
  },
): Promise<{ project_id: string; updated: boolean }> {
  return json(`/api/projects/${pid}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export function deleteProject(pid: string): Promise<{ project_id: string; archived: boolean }> {
  return json(`/api/projects/${pid}`, { method: "DELETE" });
}

export function reviewPlan(
  pid: string,
  lastActiveAt?: string,
): Promise<{ recommendation: string; candidates: unknown[]; rationale: string }> {
  const qs = lastActiveAt ? `?last_active_at=${encodeURIComponent(lastActiveAt)}` : "";
  return json(`/api/projects/${pid}/review-plan${qs}`);
}

export function importSampleSource(pid: string): Promise<{
  source_id: string;
  book_id: string;
  job_id: string;
  reused?: boolean;
  sample_kind: "real_pdf";
}> {
  return json(`/api/projects/${pid}/sources/sample`, { method: "POST" });
}

export function learningSummary(pid: string): Promise<LearningSummary> {
  return json(`/api/projects/${pid}/learning-summary`);
}

export function conceptLearningRecord(pid: string, conceptId: string): Promise<ConceptLearningRecord> {
  return json(`/api/projects/${pid}/concepts/${conceptId}/record`);
}

export function knowledgeGraph(pid: string): Promise<KnowledgeGraph> {
  return json(`/api/projects/${pid}/knowledge-graph`);
}

export function listMisconceptions(pid: string): Promise<MisconceptionView[]> {
  return json(`/api/projects/${pid}/misconceptions`);
}

export function listConversations(pid: string, activity: ConversationActivity): Promise<ConversationSummary[]> {
  return json(`/api/projects/${pid}/conversations?activity_type=${encodeURIComponent(activity)}`);
}

export function createConversation(pid: string, activity: ConversationActivity): Promise<ConversationSummary> {
  return json(`/api/projects/${pid}/conversations?activity_type=${encodeURIComponent(activity)}`, { method: "POST" });
}

export function renameConversation(
  cid: string,
  title: string,
): Promise<{ conversation_id: string; title: string }> {
  return json(`/api/conversations/${cid}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

export function deleteConversation(cid: string): Promise<{ conversation_id: string; deleted: boolean }> {
  return json(`/api/conversations/${cid}`, { method: "DELETE" });
}

export function getConversation(cid: string): Promise<Conversation> {
  return json(`/api/conversations/${cid}`);
}

export function sendMessage(
  cid: string,
  content: string,
  idempotencyKey = "",
  sourceContext: {
    source_id?: string;
    source_page?: number;
    source_scope?: "CURRENT_PAGE" | "CURRENT_SOURCE" | "ALL_SOURCES";
    selection_text?: string;
    record_question_signal?: boolean;
  } = {},
): Promise<SendMessageResult> {
  return json(`/api/conversations/${cid}/messages`, {
    method: "POST",
    body: JSON.stringify({ content, idempotency_key: idempotencyKey, ...sourceContext }),
  });
}

export function cancelRun(runId: string): Promise<{ status: string }> {
  return json(`/api/runs/${runId}/cancel`, { method: "POST" });
}

export function consolidationCandidates(
  projectId: string,
  mode: ConsolidationMode,
  filter: ConsolidationFilter = "RECOMMENDED",
  sourceId = "",
): Promise<ConsolidationQueue> {
  const params = new URLSearchParams({ mode, filter });
  if (sourceId) params.set("source_id", sourceId);
  return json(`/api/projects/${projectId}/consolidation-candidates?${params.toString()}`);
}

export function createTask(
  conversationId: string,
  body: {
    mode: ConsolidationMode;
    selection?: ConsolidationFilter;
    concept_id?: string;
    from_task_id?: string;
    idempotency_key?: string;
  },
): Promise<{ task: Record<string, unknown>; message_id: string | null; message?: ApiMessage; existing: boolean }> {
  return json(`/api/conversations/${conversationId}/tasks`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function finishConsolidation(
  conversationId: string,
): Promise<{ message_id: string; total: number; counts: Record<string, number>; text: string }> {
  return json(`/api/conversations/${conversationId}/consolidation-summary`, { method: "POST" });
}

export function explainTask(
  conversationId: string,
  taskId: string,
): Promise<{ message_id: string; grounded: boolean }> {
  return json(`/api/conversations/${conversationId}/tasks/${taskId}/explanation`, { method: "POST" });
}

// --- M4: tasks / answers / hints --------------------------------------------
// The browser interacts with a server-owned task by task_id only. An answer
// submits ONLY answer_text + idempotency_key — never PASS/FAIL, Concept ID,
// rubric, hint count, or misconception score.

export function getTask(
  taskId: string,
): Promise<Record<string, unknown>> {
  return json(`/api/tasks/${taskId}`);
}

export function submitAnswer(
  taskId: string,
  answerText: string,
  idempotencyKey = "",
): Promise<AnswerResult> {
  return json(`/api/tasks/${taskId}/answer`, {
    method: "POST",
    body: JSON.stringify({ answer_text: answerText, idempotency_key: idempotencyKey }),
  });
}

export function requestHint(
  taskId: string,
): Promise<{ hint_notice: string; hint_text: string }> {
  return json(`/api/tasks/${taskId}/hint`, { method: "POST" });
}

export function skipTask(taskId: string): Promise<{ status: string }> {
  return json(`/api/tasks/${taskId}/skip`, { method: "POST" });
}

// --- Learning-source upload + processing + viewer ---------------------------

export async function uploadSource(
  projectId: string,
  file: File,
  title = "",
): Promise<{ source_id: string; book_id: string; job_id: string; reused?: boolean }> {
  const form = new FormData();
  form.append("file", file);
  if (title) form.append("title", title);
  const r = await fetch(BASE + `/api/projects/${projectId}/sources`, {
    method: "POST",
    body: form,
    credentials: "same-origin",
  });
  const body = (await r.json().catch(() => ({}))) as {
    error?: { code?: string; message?: string; can_retry?: boolean; action?: string };
  };
  if (!r.ok) {
    const err = body.error || { message: `HTTP ${r.status}` };
    throw new ApiError(
      err.code || "HTTP_ERROR",
      err.message || `HTTP ${r.status}`,
      err.can_retry,
      err.action,
    );
  }
  return body as { source_id: string; book_id: string; job_id: string; reused?: boolean };
}

export function listSources(projectId: string): Promise<LearningSourceView[]> {
  return json(`/api/projects/${projectId}/sources`);
}

export function getJob(jobId: string): Promise<JobView> {
  return json(`/api/jobs/${jobId}`);
}

export function retryJob(jobId: string): Promise<{ job_id: string; state: string }> {
  return json(`/api/jobs/${jobId}/retry`, { method: "POST" });
}

export function cancelJob(jobId: string): Promise<{ job_id: string; state: string }> {
  return json(`/api/jobs/${jobId}/cancel`, { method: "POST" });
}

export function sourceFileUrl(sourceId: string, page?: number | string): string {
  const base = `/api/sources/${sourceId}/file`;
  return page ? `${base}#page=${page}` : base;
}

export function getSourceOutline(sourceId: string): Promise<{
  source_id: string;
  items: LearningSourceView["outline"];
  parser_version: string;
}> {
  return json(`/api/sources/${sourceId}/outline`);
}

export function updateSourceOutline(
  sourceId: string,
  items: LearningSourceView["outline"],
): Promise<{ source_id: string; items: LearningSourceView["outline"]; message: string }> {
  return json(`/api/sources/${sourceId}/outline`, {
    method: "PATCH",
    body: JSON.stringify({ items }),
  });
}

export function reparseSource(sourceId: string): Promise<{ source_id: string; job_id: string; state: string }> {
  return json(`/api/sources/${sourceId}/reparse`, { method: "POST" });
}

export function getSourceQuality(sourceId: string): Promise<{
  source_id: string;
  summary: Record<string, number>;
  warnings: string[];
  pages_done: number;
  pages_total: number;
  parser_mode: string;
  pages: { page: number; printed_page?: string; parser: string; quality_score?: number; quality_label: string; warning?: string }[];
}> {
  return json(`/api/sources/${sourceId}/quality`);
}

export function getSourceTextLayer(sourceId: string, page: number): Promise<{
  source_id: string;
  page: number;
  ready: boolean;
  width?: number;
  height?: number;
  parser?: string;
  printed_page?: string;
  blocks: { block_id: string; text: string; bbox: [number, number, number, number]; type: string; confidence?: number }[];
}> {
  return json(`/api/sources/${sourceId}/pages/${page}/text-layer`);
}

export function searchSource(sourceId: string, query: string): Promise<{
  source_id: string;
  query: string;
  results: { page: number; printed_page?: string; section_path: string[]; snippet: string; block_id: string }[];
}> {
  return json(`/api/sources/${sourceId}/search?q=${encodeURIComponent(query)}`);
}

const TERMINAL = ["SUCCEEDED", "FAILED", "RETRYABLE_FAILED", "CANCELLED"] as const;

/** Poll a job until it reaches a terminal state; calls onProgress(job) each poll. */
export async function pollJob(
  jobId: string,
  { onProgress, timeoutMs = 1800000 }: { onProgress?: (job: JobView) => void; timeoutMs?: number } = {},
): Promise<JobView> {
  const start = Date.now();
  let latest: JobView | null = null;
  return new Promise((resolve) => {
    const tick = async () => {
      try {
        const job = await getJob(jobId);
        latest = job;
        onProgress?.(job);
        if (TERMINAL.includes(job.state as (typeof TERMINAL)[number])) {
          resolve(job);
          return;
        }
      } catch (e) {
        onProgress?.({ ...({} as JobView), state: "FAILED", error: (e as Error).message });
        resolve({ ...({} as JobView), state: "FAILED", error: (e as Error).message });
        return;
      }
      if (Date.now() - start > timeoutMs) {
        // Stop watching without turning a healthy server-side job into a fake
        // failure. The source list will reconnect to this job on the next visit.
        resolve(latest || { ...({} as JobView), state: "PENDING", error: "" });
        return;
      }
      setTimeout(tick, 600);
    };
    tick();
  });
}

const ALL_EVENTS: EventType[] = [
  "run_started", "mode_selected", "action_selected", "agent_started",
  "agent_delta", "tool_started", "tool_completed", "agent_completed",
  "citation_attached", "fallback_used", "evidence_created", "state_updated",
  "review_scheduled", "run_completed", "run_failed", "run_cancelled",
  "retrieval_completed", "source_locations_ready", "answer_delta",
  "answer_completed", "answer_unavailable",
];

export interface RunSubscription {
  close: () => void;
}

/** Subscribe to a run's SSE event stream. */
export function subscribeRun(
  runId: string,
  { onEvent, onDone }: { onEvent?: (type: EventType, data: Record<string, unknown>) => void; onDone?: () => void } = {},
): RunSubscription {
  const es = new EventSource(`/api/runs/${runId}/events`);
  const handler = (e: MessageEvent) => {
    const data = e.data ? JSON.parse(e.data) : {};
    onEvent?.(e.type as EventType, data);
    if (e.type === "run_completed" || e.type === "run_failed" || e.type === "run_cancelled") {
      es.close();
      onDone?.();
    }
  };
  for (const t of ALL_EVENTS) es.addEventListener(t, handler as EventListener);
  es.onerror = () => {
    es.close();
    onDone?.();
  };
  return { close: () => es.close() };
}
