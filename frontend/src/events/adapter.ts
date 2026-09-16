// BookMindRuntimeAdapter — the single place that translates backend domain
// events into assistant-ui chat behaviour (PRODUCTIZATION §8.4). The rest of
// the UI is unaware of the EventType enum.
//
// Two layers:
//   1. `applySseEvent` accumulates an in-flight assistant message (text +
//      citations + tool status) as SSE events stream in — used while a run is
//      live.
//   2. `serverMessageToThreadMessageLike` converts a persisted ApiMessage
//      (content_blocks) into a ThreadMessageLike for assistant-ui's external
//      store — used to render history + replace the streamed placeholder once
//      the authoritative message lands.

import type { ThreadMessageLike } from "@assistant-ui/react";
import type { ContentBlock, EventType } from "../types/blocks";
const TOOL_LABELS: Record<string, string> = {
  retrieval: "正在检索学习资料",
  citation: "正在核对出处",
  diagnosis: "正在判断回答",
};

export interface LiveRunSnapshot {
  text: string;
  citations: {
    index: number;
    chunk_id: string;
    page: string;
    book_id: string;
    label: string;
  }[];
  status: "idle" | "running" | "completed" | "failed";
  toolStatus: string;
  error: string;
  fallback: boolean;
}

export function newLiveRun(): LiveRunSnapshot {
  return {
    text: "",
    citations: [],
    status: "idle",
    toolStatus: "",
    error: "",
    fallback: false,
  };
}

/** Fold one SSE event into the live-run snapshot (mutates and returns it). */
export function applySseEvent(snap: LiveRunSnapshot, type: EventType, data: Record<string, unknown>): LiveRunSnapshot {
  switch (type) {
    case "run_started":
      snap.status = "running";
      break;
    case "tool_started":
      snap.toolStatus = (TOOL_LABELS[data.tool as string] || "正在处理") + "…";
      break;
    case "tool_completed":
      snap.toolStatus = "";
      break;
    case "agent_delta":
      // Only user-visible text; never reasoning chains.
      snap.text += (data.text as string) || "";
      break;
    case "citation_attached": {
      const i = ((data.index as number) ?? snap.citations.length + 1) - 1;
      snap.citations[i] = {
        index: (data.index as number) ?? snap.citations.length + 1,
        chunk_id: (data.chunk_id as string) || "",
        page: String(data.page || ""),
        book_id: (data.book_id as string) || "",
        label: `[${data.index ?? snap.citations.length + 1}] 第 ${data.page || "?"} 页`,
      };
      break;
    }
    case "source_locations_ready": {
      const locations = Array.isArray(data.locations) ? data.locations as Record<string, unknown>[] : [];
      snap.citations = locations.map((location, index) => ({
        index: index + 1,
        chunk_id: String(location.chunk_id || ""),
        page: String(location.page_start || location.page || ""),
        book_id: String(location.book_id || ""),
        label: `${Array.isArray(location.section_path) ? location.section_path.join(" · ") + " · " : ""}第 ${location.page_start || location.page || "?"}${location.page_end && location.page_end !== location.page_start ? `～${location.page_end}` : ""} 页`,
      }));
      snap.toolStatus = "已找到相关教材位置，正在生成回答…";
      break;
    }
    case "fallback_used":
      snap.fallback = true;
      break;
    case "run_completed":
      snap.status = "completed";
      snap.toolStatus = "";
      break;
    case "run_failed":
      snap.status = "failed";
      snap.error = (data.error as string) || "处理失败";
      snap.toolStatus = "";
      break;
    case "run_cancelled":
      snap.status = "failed";
      snap.error = "已停止生成回答";
      snap.toolStatus = "";
      break;
    default:
      // action_selected, mode_selected, review_scheduled, agent_started/
      // agent_completed, evidence_created, state_updated — surfaced only in the
      // trace drawer, not the live bubble (history renders them from blocks).
      break;
  }
  return snap;
}

// --- converter: persisted ApiMessage -> ThreadMessageLike -------------------

// The assistant-ui content part union is complex; we type our parts loosely
// here and let ThreadMessageLike's union validate at the call site. Each part
// is either a text part or a `data-*` custom part carrying a structured block.
type AssistantPart = { type: "text"; text: string } | { type: string; data: unknown };

/** Build the assistant-ui content parts for one backend ContentBlock. */
function blockToParts(b: ContentBlock): AssistantPart[] {
  switch (b.type) {
    case "text":
      return b.text ? [{ type: "text", text: b.text }] : [];
    case "status":
      return [{ type: "data-status", data: { text: b.text || "" } }];
    case "error":
      return [{ type: "data-error", data: { text: b.text || "" } }];
    case "citation":
      return [
        {
          type: "data-citation",
          data: {
            chunk_id: b.chunk_id || "",
            page: b.page || "",
            book_id: b.book_id || "",
            label: b.label || "",
            quote: b.quote || "",
          },
        },
      ];
    case "task":
      return [{ type: "data-task", data: (b.data || {}) as unknown }];
    case "state_change":
      return [{ type: "data-state-change", data: (b.data || {}) as unknown }];
    default:
      return [];
  }
}

/** Convert one persisted server message to an assistant-ui thread message. */
export function serverMessageToThreadMessageLike(
  msg: { role: "user" | "assistant"; content_blocks: ContentBlock[]; message_id?: string },
): ThreadMessageLike {
  const parts: AssistantPart[] = [];
  for (const b of msg.content_blocks) parts.push(...blockToParts(b));
  if (msg.role === "user") {
    // User messages are plain text (the orchestrator always writes one text block).
    const text = msg.content_blocks.map((b) => b.text || "").join("");
    return {
      role: "user",
      content: [{ type: "text", text }],
      id: msg.message_id,
    };
  }
  return {
    role: "assistant",
    content: (parts.length ? parts : [{ type: "text", text: "" }]) as ThreadMessageLike["content"] extends string ? never : ThreadMessageLike["content"],
    id: msg.message_id,
    status: { type: "complete", reason: "stop" },
  };
}

/** Convert a live-run snapshot (mid-stream) to an assistant-ui thread message. */
export function liveRunToThreadMessageLike(snap: LiveRunSnapshot, id?: string): ThreadMessageLike {
  const parts: AssistantPart[] = [];
  if (snap.toolStatus) parts.push({ type: "data-status", data: { text: snap.toolStatus } });
  if (snap.fallback)
    parts.push({ type: "data-status", data: { text: "基础模式（模型不可用，使用离线回退）" } });
  if (snap.text) parts.push({ type: "text", text: snap.text });
  for (const c of snap.citations)
    parts.push({
      type: "data-citation",
      data: { chunk_id: c.chunk_id, page: c.page, book_id: c.book_id, label: c.label, quote: "" },
    });
  if (snap.error) parts.push({ type: "data-error", data: { text: snap.error } });
  // MessageStatus has no "error" variant; a failed run is surfaced via the
  // data-error part, and the message itself is marked complete.
  const status =
    snap.status === "running"
      ? { type: "running" as const }
      : { type: "complete" as const, reason: "stop" as const };
  return {
    role: "assistant",
    content: (parts.length ? parts : [{ type: "text", text: "" }]) as ThreadMessageLike["content"] extends string ? never : ThreadMessageLike["content"],
    id,
    status,
  };
}
