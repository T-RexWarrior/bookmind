// App-wide state (React context + useReducer, no Redux). The server is the
// authority for projects/conversations/learning state; this store holds only
// what the UI needs to render between fetches plus transient flags.

import { createContext, useContext, useReducer, type ReactNode, type Dispatch } from "react";
import type {
  ApiMessage,
  LearningSourceView,
  ConversationSummary,
  ConversationActivity,
  JobView,
  LearningSummary,
  MisconceptionView,
  Project,
  TaskCardData,
  User,
} from "../types/blocks";

export type UIMode = "LEARN" | "REVIEW";

export function activityFromMode(mode: UIMode): ConversationActivity {
  return mode;
}

export interface ReaderState {
  sourceId: string;
  page: number;
}

export interface TextSelection {
  text: string;
  sourceId: string;
  page: number;
}

export type QueryScope = "CURRENT_PAGE" | "CURRENT_SOURCE" | "ALL_SOURCES";

export interface AppState {
  user: User | null;
  projects: Project[];
  activeProject: Project | null;
  conversations: ConversationSummary[];
  activeConversation: ConversationSummary | null;
  conversationActivity: ConversationActivity;
  messages: ApiMessage[];
  summary: LearningSummary | null;
  misconceptions: MisconceptionView[];
  sources: LearningSourceView[];
  job: JobView | null;
  uploading: boolean;
  reader: ReaderState | null;
  textSelection: TextSelection | null;
  pendingTask: TaskCardData | null;
  activeFollowupTaskId: string;
  composer: string;
  sending: boolean;
  status: "idle" | "loading" | "error";
  error: string;
  mode: UIMode;
  queryScope: QueryScope;
  recordQuestionSignal: boolean;
  leftDrawerOpen: boolean;
  rightDrawerOpen: boolean;
}

export type Action =
  | { type: "SET_USER"; user: User | null }
  | { type: "SET_PROJECTS"; projects: Project[] }
  | { type: "OPEN_PROJECT"; project: Project; mode: UIMode; conversationActivity: ConversationActivity; conversations: ConversationSummary[]; summary: LearningSummary | null; sources: LearningSourceView[]; misconceptions: MisconceptionView[] }
  | { type: "SET_ACTIVE_PROJECT"; project: Project }
  | { type: "SET_CONVERSATIONS"; conversations: ConversationSummary[]; activity?: ConversationActivity; projectId?: string }
  | { type: "SET_ACTIVE_CONVERSATION"; conversation: ConversationSummary | null; messages: ApiMessage[]; activity?: ConversationActivity; projectId?: string }
  | { type: "SET_MESSAGES"; messages: ApiMessage[] }
  | { type: "APPEND_MESSAGE"; message: ApiMessage }
  | { type: "SET_SUMMARY"; summary: LearningSummary | null; projectId?: string }
  | { type: "SET_MISCONCEPTIONS"; misconceptions: MisconceptionView[]; projectId?: string }
  | { type: "SET_SOURCES"; sources: LearningSourceView[]; projectId?: string }
  | { type: "SET_JOB"; job: JobView | null; projectId?: string }
  | { type: "SET_UPLOADING"; uploading: boolean; projectId?: string }
  | { type: "SET_READER"; reader: ReaderState | null; projectId?: string }
  | { type: "SET_TEXT_SELECTION"; selection: TextSelection | null }
  | { type: "SET_PENDING_TASK"; pendingTask: TaskCardData | null; activity?: ConversationActivity; projectId?: string }
  | { type: "SET_FOLLOWUP_TASK"; taskId: string; activity?: ConversationActivity; projectId?: string }
  | { type: "SET_COMPOSER"; composer: string }
  | { type: "SET_SENDING"; sending: boolean; conversationId?: string }
  | { type: "SET_STATUS"; status: "idle" | "loading" | "error"; error?: string }
  | { type: "SET_MODE"; mode: UIMode }
  | { type: "SET_QUERY_SCOPE"; scope: QueryScope }
  | { type: "SET_RECORD_QUESTION_SIGNAL"; enabled: boolean }
  | { type: "SET_DRAWER"; left?: boolean; right?: boolean }
  | { type: "CLEAR_PROJECT" };

const initial: AppState = {
  user: null,
  projects: [],
  activeProject: null,
  conversations: [],
  activeConversation: null,
  conversationActivity: "LEARN",
  messages: [],
  summary: null,
  misconceptions: [],
  sources: [],
  job: null,
  uploading: false,
  reader: null,
  textSelection: null,
  pendingTask: null,
  activeFollowupTaskId: "",
  composer: "",
  sending: false,
  status: "idle",
  error: "",
  mode: "LEARN",
  queryScope: "CURRENT_SOURCE",
  recordQuestionSignal: true,
  leftDrawerOpen: false,
  rightDrawerOpen: false,
};

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "SET_USER":
      return { ...state, user: action.user };
    case "SET_PROJECTS":
      return { ...state, projects: action.projects };
    case "OPEN_PROJECT":
      return {
        ...state,
        activeProject: action.project,
        mode: action.mode,
        conversationActivity: action.conversationActivity,
        conversations: action.conversations,
        summary: action.summary,
        sources: action.sources,
        misconceptions: action.misconceptions,
        activeConversation: null,
        messages: [],
        job: null,
        uploading: false,
        pendingTask: null,
        activeFollowupTaskId: "",
        composer: "",
        sending: false,
        queryScope: "CURRENT_SOURCE",
        recordQuestionSignal: true,
        reader: action.project.last_source_id && action.sources.some(
          (source) => source.source_id === action.project.last_source_id,
        )
          ? { sourceId: action.project.last_source_id, page: action.project.last_source_page || 1 }
          : action.sources[0]
            ? { sourceId: action.sources[0].source_id, page: 1 }
            : null,
        textSelection: null,
        status: "idle",
      };
    case "SET_ACTIVE_PROJECT":
      return { ...state, activeProject: action.project };
    case "SET_CONVERSATIONS":
      if (action.activity && action.activity !== state.conversationActivity) return state;
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, conversations: action.conversations };
    case "SET_ACTIVE_CONVERSATION":
      if (action.activity && action.activity !== state.conversationActivity) return state;
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, activeConversation: action.conversation, messages: action.messages };
    case "SET_MESSAGES":
      return { ...state, messages: action.messages };
    case "APPEND_MESSAGE":
      return { ...state, messages: [...state.messages, action.message] };
    case "SET_SUMMARY":
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, summary: action.summary };
    case "SET_MISCONCEPTIONS":
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, misconceptions: action.misconceptions };
    case "SET_SOURCES":
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, sources: action.sources };
    case "SET_JOB":
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, job: action.job };
    case "SET_UPLOADING":
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, uploading: action.uploading };
    case "SET_READER":
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, reader: action.reader };
    case "SET_TEXT_SELECTION":
      return { ...state, textSelection: action.selection };
    case "SET_PENDING_TASK":
      if (action.activity && action.activity !== state.conversationActivity) return state;
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, pendingTask: action.pendingTask };
    case "SET_FOLLOWUP_TASK":
      if (action.activity && action.activity !== state.conversationActivity) return state;
      if (action.projectId && action.projectId !== state.activeProject?.project_id) return state;
      return { ...state, activeFollowupTaskId: action.taskId };
    case "SET_COMPOSER":
      return { ...state, composer: action.composer };
    case "SET_SENDING":
      if (action.conversationId && action.conversationId !== state.activeConversation?.conversation_id) return state;
      return { ...state, sending: action.sending };
    case "SET_STATUS":
      return { ...state, status: action.status, error: action.error || "" };
    case "SET_MODE":
      return {
        ...state,
        mode: action.mode,
        conversationActivity: activityFromMode(action.mode),
        conversations: [],
        activeConversation: null,
        messages: [],
        pendingTask: null,
        activeFollowupTaskId: "",
        textSelection: null,
        composer: "",
        sending: false,
      };
    case "SET_QUERY_SCOPE":
      return { ...state, queryScope: action.scope };
    case "SET_RECORD_QUESTION_SIGNAL":
      return { ...state, recordQuestionSignal: action.enabled };
    case "SET_DRAWER":
      return {
        ...state,
        leftDrawerOpen: action.left ?? state.leftDrawerOpen,
        rightDrawerOpen: action.right ?? state.rightDrawerOpen,
      };
    case "CLEAR_PROJECT":
      // P0-04: clear ALL project-scoped state so leaving a project is clean.
      return {
        ...state,
        activeProject: null,
        conversations: [],
        activeConversation: null,
        conversationActivity: "LEARN",
        messages: [],
        summary: null,
        misconceptions: [],
        sources: [],
        job: null,
        uploading: false,
        reader: null,
        textSelection: null,
        pendingTask: null,
        activeFollowupTaskId: "",
        composer: "",
        sending: false,
        leftDrawerOpen: false,
        rightDrawerOpen: false,
      };
    default:
      return state;
  }
}

interface Ctx {
  state: AppState;
  dispatch: Dispatch<Action>;
}

const AppCtx = createContext<Ctx | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initial);
  return <AppCtx.Provider value={{ state, dispatch }}>{children}</AppCtx.Provider>;
}

export function useApp(): Ctx {
  const ctx = useContext(AppCtx);
  if (!ctx) throw new Error("useApp must be used within AppProvider");
  return ctx;
}
