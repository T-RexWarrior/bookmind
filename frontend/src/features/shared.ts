// Helpers shared across features.

import type { ApiMessage, ContentBlock, TaskCardData } from "../types/blocks";

/** Detect a pending (unanswered) task from the last assistant message so the
 * composer can switch to "输入你的答案" mode. A task block whose kind is an
 * assessment (probe/changed_task/quiz) and is the last task block with no
 * following judgment counts as pending. */
export function detectPendingTask(messages: ApiMessage[]): TaskCardData | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "assistant") continue;
    const taskBlocks = (m.content_blocks || []).filter((b: ContentBlock) => b.type === "task");
    if (!taskBlocks.length) continue;
    const last = taskBlocks[taskBlocks.length - 1];
    const data = (last.data || {}) as unknown as TaskCardData;
    if (["probe", "changed_task", "quiz"].includes(data.kind)) return data;
    // NEEDS_REVIEW is a request to clarify, not the end of the task. Continue
    // scanning for the original task card so the composer remains in answer
    // mode after reload. A decided judgment closes the pending task.
    if (data.kind === "judgment" && Boolean((last.data || {}).needs_review)) continue;
    return null;
  }
  return null;
}

export const PROCESSING_STAGES = ["upload", "parse", "index", "graph", "done"] as const;
export const STAGE_LABELS: Record<string, string> = {
  upload: "已经收好原资料",
  parse: "正在读页面和章节",
  index: "正在整理可提问的内容",
  graph: "正在梳理知识脉络",
  done: "可以开始学习啦",
};

export function isTerminalJobState(state: string): boolean {
  return ["SUCCEEDED", "FAILED", "RETRYABLE_FAILED", "CANCELLED"].includes(state);
}
