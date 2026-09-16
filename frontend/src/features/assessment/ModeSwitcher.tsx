import { useApp, activityFromMode } from "../../store/appStore";
import * as api from "../../api/client";
import { toast } from "../../components/ui/primitives";
import { detectPendingTask } from "../shared";
import type { ApiMessage, ConversationActivity } from "../../types/blocks";
import type { UIMode, Action } from "../../store/appStore";
import type { Dispatch } from "react";

export const MODE_META: Record<UIMode, { label: string; short: string; icon: string }> = {
  LEARN: { label: "资料学习", short: "边读边问，回答回到原文", icon: "▤" },
  REVIEW: { label: "练习巩固", short: "做题、纠错与到期复验", icon: "↻" },
};

const MODE_TO_PRESET: Record<UIMode, string> = {
  LEARN: "Deep Learning",
  REVIEW: "Review",
};
const PRESET_TO_MODE: Record<string, UIMode> = {
  "Quiet Reading": "LEARN",
  "Deep Learning": "LEARN",
  Review: "REVIEW",
  // Historical assessment projects now open in the unified practice space.
  Assessment: "REVIEW",
};

function messagesFromConversation(
  conversation: Awaited<ReturnType<typeof api.getConversation>>,
): ApiMessage[] {
  return conversation.messages.map((message) => ({
    role: message.role,
    content_blocks: message.content_blocks,
    message_id: message.message_id,
    run_id: message.run_id,
    created_at: message.created_at,
  }));
}

export async function loadActivityConversation(
  projectId: string,
  activity: ConversationActivity,
  dispatch: Dispatch<Action>,
): Promise<void> {
  let conversations = await api.listConversations(projectId, activity);
  if (!conversations.length) conversations = [await api.createConversation(projectId, activity)];
  dispatch({ type: "SET_CONVERSATIONS", conversations, activity, projectId });
  const active = await api.getConversation(conversations[0].conversation_id);
  const messages = messagesFromConversation(active);
  dispatch({ type: "SET_ACTIVE_CONVERSATION", conversation: conversations[0], messages, activity, projectId });
  dispatch({ type: "SET_PENDING_TASK", pendingTask: detectPendingTask(messages), activity, projectId });
  dispatch({ type: "SET_FOLLOWUP_TASK", taskId: active.practice_state?.phase === "FOLLOWUP" ? active.practice_state.task_id : "", activity, projectId });
}

export function ModeSwitcher() {
  const { state, dispatch } = useApp();

  async function switchTo(mode: UIMode) {
    if (!state.activeProject || mode === state.mode) return;
    const projectId = state.activeProject.project_id;
    const previous = state.mode;
    dispatch({ type: "SET_MODE", mode });
    try {
      await Promise.all([
        api.updateProject(projectId, { default_mode: MODE_TO_PRESET[mode] }),
        loadActivityConversation(projectId, activityFromMode(mode), dispatch),
      ]);
    } catch (error) {
      dispatch({ type: "SET_MODE", mode: previous });
      void loadActivityConversation(projectId, activityFromMode(previous), dispatch).catch(() => undefined);
      toast((error as Error).message || "这个板块暂时没打开，再试一次好吗？");
    }
  }

  return (
    <div className="module-switcher">
      <nav className="activity-tabs" aria-label="主要功能">
        <button className={state.mode === "LEARN" ? "is-active" : ""} onClick={() => switchTo("LEARN")} aria-current={state.mode === "LEARN" ? "page" : undefined}>
          <span className="activity-tabs__icon">▤</span>
          <span><strong>资料学习</strong><small>阅读、提问并核对原文</small></span>
        </button>
        <button className={state.mode === "REVIEW" ? "is-active" : ""} onClick={() => switchTo("REVIEW")} aria-current={state.mode === "REVIEW" ? "page" : undefined}>
          <span className="activity-tabs__icon">✓</span>
          <span><strong>练习巩固</strong><small>做题、纠错与到期复验</small></span>
        </button>
      </nav>
    </div>
  );
}

export function modeFromProjectDefault(defaultMode: string | undefined): UIMode {
  return defaultMode ? PRESET_TO_MODE[defaultMode] ?? "LEARN" : "LEARN";
}
