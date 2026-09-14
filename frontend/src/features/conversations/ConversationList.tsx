// ConversationList — the left-pane list of conversations with rename + delete.
// Deleting a conversation does NOT delete Evidence (PRODUCTIZATION §5.12).

import { useState } from "react";
import { useApp } from "../../store/appStore";
import { Button, Dialog } from "../../components/ui/primitives";
import * as api from "../../api/client";
import { toast } from "../../components/ui/primitives";
import { useConversationActions } from "./useConversationActions";

export function ConversationList() {
  const { state, dispatch } = useApp();
  const actions = useConversationActions();
  const [renaming, setRenaming] = useState<string | null>(null);
  const [renameVal, setRenameVal] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);

  async function doRename() {
    if (!renaming) return;
    const projectId = state.activeProject?.project_id;
    try {
      const res = await api.renameConversation(renaming, renameVal);
      const convs = state.conversations.map((c) =>
        c.conversation_id === renaming ? { ...c, title: res.title } : c,
      );
      dispatch({ type: "SET_CONVERSATIONS", conversations: convs, activity: state.conversationActivity, projectId });
    } catch (e) {
      toast((e as Error).message || "名字暂时没改好，再试一次好吗？");
    }
    setRenaming(null);
  }

  async function doDelete() {
    if (!deleting) return;
    const projectId = state.activeProject?.project_id;
    try {
      await api.deleteConversation(deleting);
      const convs = state.conversations.filter((c) => c.conversation_id !== deleting);
      dispatch({ type: "SET_CONVERSATIONS", conversations: convs, activity: state.conversationActivity, projectId });
      if (state.activeConversation?.conversation_id === deleting) {
        if (convs.length) {
          await actions.openConversation(convs[0].conversation_id);
        } else {
          // P0-15: deleting the last conversation must leave activeConversation
          // as null (not an empty object) so the composer is disabled and no
          // request goes to /api/conversations/undefined/messages.
          dispatch({ type: "SET_ACTIVE_CONVERSATION", conversation: null, messages: [], activity: state.conversationActivity, projectId });
        }
      }
      toast("这段对话已经删掉了，学习记录还好好保留着");
    } catch (e) {
      toast((e as Error).message || "这段对话暂时没删掉，再试一次好吗？");
    }
    setDeleting(null);
  }

  return (
    <>
      <Button
        variant="ghost"
        className=""
        style={{ width: "100%", margin: "12px 0" }}
        onClick={() => state.activeProject && actions.newConversation(state.activeProject.project_id)}
      >
        + 在这个板块新开一段对话
      </Button>
      <ul style={{ listStyle: "none", padding: 0, marginTop: 12 }}>
        {state.conversations.map((c) => (
          <li
            key={c.conversation_id}
            onClick={() => actions.openConversation(c.conversation_id)}
            className={state.activeConversation?.conversation_id === c.conversation_id ? "bg-accent-soft c-accent" : ""}
            style={{
              padding: "8px 10px",
              borderRadius: 8,
              cursor: "pointer",
              fontSize: 13,
              color: state.activeConversation?.conversation_id === c.conversation_id ? "var(--accent)" : "var(--muted)",
              fontWeight: state.activeConversation?.conversation_id === c.conversation_id ? 500 : 400,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 4,
            }}
          >
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>{c.title || "新对话"}</span>
            <span style={{ display: "flex", gap: 2, flexShrink: 0 }} onClick={(e) => e.stopPropagation()}>
              <button
                className="btn ghost"
                style={{ padding: "2px 6px", fontSize: 11 }}
                aria-label="重命名"
                onClick={() => {
                  setRenaming(c.conversation_id);
                  setRenameVal(c.title || "");
                }}
              >
                ✎
              </button>
              <button
                className="btn ghost"
                style={{ padding: "2px 6px", fontSize: 11, color: "var(--danger)" }}
                aria-label="删除"
                onClick={() => setDeleting(c.conversation_id)}
              >
                ✕
              </button>
            </span>
          </li>
        ))}
      </ul>

      <Dialog
        open={!!renaming}
        onClose={() => setRenaming(null)}
        title="给这段对话换个名字"
        footer={
          <>
            <Button onClick={() => setRenaming(null)}>取消</Button>
            <Button variant="primary" onClick={doRename}>保存</Button>
          </>
        }
      >
        <input
          value={renameVal}
          onChange={(e) => setRenameVal(e.target.value)}
          autoFocus
          style={{ width: "100%", padding: "8px 10px", border: "1px solid var(--border)", borderRadius: 8, fontSize: 14, background: "var(--panel)", color: "var(--text)" }}
        />
      </Dialog>

      <Dialog
        open={!!deleting}
        onClose={() => setDeleting(null)}
        title="删掉这段对话吗？"
        footer={
          <>
            <Button onClick={() => setDeleting(null)}>取消</Button>
            <Button variant="danger" onClick={doDelete}>删除</Button>
          </>
        }
      >
        <p style={{ fontSize: 14 }}>只会删掉这里的聊天内容，已经形成的学习记录和进度都会保留。</p>
      </Dialog>
    </>
  );
}
