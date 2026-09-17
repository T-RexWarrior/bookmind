// Content block + task/judgment schemas — mirror the backend exactly so the
// frontend renders the server-owned payload and nothing more.
//
// SECURITY: the task card data only ever carries the prompt + kind + ids the
// browser needs. It MUST NOT include rubric, expected_answer, target_concept_ids,
// discriminated_bug_ids, or any internal field (enforced server-side by
// tests/test_api_m4_closure.py — "test_task_card_never_leaks_internals").

export type BlockType =
  | "text"
  | "citation"
  | "context"
  | "question_signal"
  | "task"
  | "state_change"
  | "error"
  | "status";

export interface ContentBlock {
  type: BlockType;
  text?: string;
  // citation
  chunk_id?: string;
  quote?: string;
  page?: string;
  book_id?: string;
  source_id?: string;
  label?: string;
  // task / state_change
  data?: Record<string, unknown>;
}

export type TaskKind = "quiz" | "probe" | "changed_task" | "judgment" | "task_options" | "task_complete" | "fallback";

/** The browser-safe task card payload (the assistant-ui data part). */
export interface TaskCardData {
  kind: TaskKind;
  task_id: string;
  prompt_text: string;
  is_probe?: boolean;
  is_changed_task?: boolean;
  remediation_stage?: number;
  focus?: string;
  source_scope?: { source_id: string; title: string; locator: string }[];
  generation_reason?: string;
  generation_mode?: "llm" | "offline_fallback" | "curated" | "template";
  generation_notice?: string;
}

export interface CriterionResult {
  criterion_id: string;
  satisfied: boolean;
  note?: string;
}

export interface JudgmentPayload {
  result?: "PASS" | "PARTIAL" | "FAIL";
  judgment_status?: "DECIDED" | "NEEDS_REVIEW";
  reason?: string;
  criterion_results?: CriterionResult[];
}

/** The judgment card payload surfaced after submitting an answer. */
export interface JudgmentCardData {
  kind: "judgment";
  task_id: string;
  judgment: JudgmentPayload;
  needs_review?: boolean;
  written?: boolean;
  next_action_code?: string;
  source_scope?: { source_id: string; title: string; page: number; locator: string }[];
}

export interface Transition {
  concept_id?: string;
  bug_id?: string;
  old_state?: string;
  new_state?: string;
}

export interface StateChangeData {
  mastery_transitions?: Transition[];
  misconception_transitions?: Transition[];
}

// --- SSE events (mirror domain/enums.py EventType) ---------------------------

export type EventType =
  | "run_started"
  | "mode_selected"
  | "action_selected"
  | "agent_started"
  | "agent_delta"
  | "tool_started"
  | "tool_completed"
  | "agent_completed"
  | "citation_attached"
  | "fallback_used"
  | "evidence_created"
  | "state_updated"
  | "review_scheduled"
  | "run_completed"
  | "run_failed"
  | "run_cancelled"
  | "retrieval_completed"
  | "source_locations_ready"
  | "answer_delta"
  | "answer_completed"
  | "answer_unavailable";

export interface SseEvent {
  run_id: string;
  sequence: number;
  timestamp?: string;
  // event-type-specific payload fields (union; consumers switch on type)
  [key: string]: unknown;
}

// --- API response shapes -----------------------------------------------------

export interface User {
  user_id: string;
  display_name: string;
}

export interface Project {
  project_id: string;
  name: string;
  goal?: string;
  learning_scope?: string;
  deadline?: string;
  current_plan?: string;
  last_source_id?: string;
  last_source_page?: number;
  updated_at?: string;
  last_activity_at?: string;
  source_count?: number;
  default_mode?: string;
}

export type ConversationActivity = "LEARN" | "REVIEW" | "ASSESSMENT";

export interface ConversationSummary {
  conversation_id: string;
  title: string;
  activity_type: ConversationActivity;
  created_at: string;
  updated_at: string;
}

export interface Conversation extends ConversationSummary {
  project_id: string;
  messages: ApiMessage[];
  practice_state?: {
    phase: "IDLE" | "ANSWERING" | "FOLLOWUP";
    task_id: string;
  };
}

/** One uniform exit card for every terminal task path. */
export interface TaskCompletionCardData {
  kind: "task_complete";
  task_id: string;
  completion_status: "ANSWERED" | "SKIPPED" | "EXPLAINED";
  source_scope?: { source_id: string; title: string; page: number; locator: string }[];
}

export interface ApiMessage {
  message_id: string;
  role: "user" | "assistant";
  content_blocks: ContentBlock[];
  run_id?: string;
  created_at: string;
}

export interface LearningSummary {
  total_concepts: number;
  questioned_count?: number;
  groups: Record<string, number>;
  concepts: {
    concept_id: string;
    name: string;
    level: string;
    group: string;
    question_count?: number;
    last_question_at?: string | null;
    attempt_count?: number;
    latest_attempt_result?: "PASS" | "PARTIAL" | "FAIL" | null;
    manual_learned?: boolean;
    profile_summary?: string;
    next_practice_goal?: string;
    book_id?: string | null;
    chapter?: string | null;
    section?: string | null;
  }[];
}

export type ConsolidationMode = "PRACTICE";
export type ConsolidationFilter = "RECOMMENDED" | "QUESTIONED" | "WEAK" | "DUE" | "UNVERIFIED" | "ALL" | "RANDOM";

export interface ConsolidationCandidate {
  concept_id: string;
  name: string;
  reason_code: "QUESTIONED" | "WEAK" | "DUE" | "UNVERIFIED" | "VERIFIED";
  reason_label: string;
  current_state: string;
  current_level: string;
  source_id: string;
  source_title: string;
  locator: string;
  question_count: number;
  last_question_at?: string | null;
  priority: number;
}

export interface ConsolidationQueue {
  mode: ConsolidationMode;
  filter: ConsolidationFilter;
  counts: { questioned: number; weak: number; due: number; unverified: number };
  candidates: ConsolidationCandidate[];
}

export interface QuestionSignalData {
  signal_id: string;
  message: string;
  concepts: { concept_id: string; name: string; question_count: number }[];
  unclassified?: boolean;
}

export interface ConceptLearningRecord {
  concept_id: string;
  name: string;
  description: string;
  chapter: string;
  section: string;
  status: { group: string; current_level: string; highest_level: string; exposure: string; manual_learned?: boolean };
  question_count: number;
  attempt_count: number;
  learner_profile?: {
    summary?: string;
    observed_understanding?: string[];
    needs_attention?: string[];
    next_practice_goal?: string;
    confidence?: number;
    evidence_basis?: string[];
    updated_at?: string;
  } | null;
  source_refs: { source_id: string; source_title: string; page: number; chunk_id?: string; label: string }[];
  timeline: {
    evidence_id: string;
    type: string;
    result?: string | null;
    independent: boolean;
    hint_level: number;
    occurred_at: string;
    question?: string;
  }[];
}

export interface KnowledgeGraph {
  project_id: string;
  book_ids: string[];
  source_ids?: string[];
  nodes: {
    concept_id: string;
    book_id: string;
    name: string;
    description: string;
    chapter: string;
    section: string;
    importance: number;
    difficulty: string;
    source: string;
    source_refs: { chunk_id?: string; physical_page?: number }[];
  }[];
  edges: { source: string; target: string; relation: string; rationale: string }[];
  stats: { concepts: number; relations: number; chapters: number };
}

export interface MisconceptionView {
  item_id: string;
  status: string;
  status_label: string;
  evidence_band: string;
  evidence_score: number;
  changed_task_pass_count: number;
}

export interface LearningSourceView {
  source_id: string;
  book_id: string;
  title: string;
  source_type: string;
  original_filename: string;
  page_count: number;
  section_count: number;
  concept_count: number;
  outline: { title: string; page: number; page_end?: number; path: string[]; confidence?: number }[];
  job_id?: string | null;
  state: string;
  stage: string;
  progress: number;
  pages_done?: number;
  pages_total?: number;
  parser_mode?: string;
  quality_summary?: Record<string, number>;
  warnings?: string[];
  checkpoint_stage?: string;
}

/** Compatibility alias while persisted engine identifiers still use book_id. */
export type BookView = LearningSourceView;

export interface JobView {
  job_id: string;
  book_id: string;
  state: string;
  stage: string;
  progress: number;
  error?: string;
  user_stage: string;
  user_label: string;
  attempt: number;
  pages_done: number;
  pages_total: number;
  parser_mode: string;
  quality_summary: Record<string, number>;
  warnings: string[];
  checkpoint_stage: string;
}

export interface SendMessageResult {
  message_id: string;
  run_id: string;
  assistant_message_id: string;
  status: string;
}

export interface AnswerResult {
  written: boolean;
  replay?: boolean;
  evidence_id?: string;
  judgment: JudgmentPayload;
  needs_review?: boolean;
  state_delta?: {
    mastery_transitions?: Transition[];
    misconception_transitions?: Transition[];
  };
  next_action?: { reason?: string };
}
