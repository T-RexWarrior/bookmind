// Conversation actions — the send/stream/new/open/rename/delete logic.
// Centralized so the ConversationPane and sidebar share one source of truth.
// The send loop: optimistic user message → POST /messages → SSE replay →
// replace placeholder with the authoritative server message (PRODUCTIZATION
// §6.3 atomic turn boundary, §5.5).

import { useCallback, useRef } from "react";
import { useApp } from "../../store/appStore";
import * as api from "../../api/client";
import {
  applySseEvent,
  newLiveRun,
} from "../../events/adapter";
import { detectPendingTask } from "../shared";
import { loadActivityConversation } from "../assessment/ModeSwitcher";
import type { ApiMessage, ConsolidationFilter, ConsolidationMode, ConversationActivity } from "../../types/blocks";
import { toast } from "../../components/ui/primitives";

export function useConversationActions() {
  const { state, dispatch } = useApp();
  // P1-07: a ref to the currently-active conversation id, kept in sync on each
  // render. Async callbacks (SSE onEvent, post-stream reload) read this ref so a
  // conversation switch mid-stream drops stale updates instead of overwriting
  // the new conversation's view.
  const activeCidRef = useRef<string | null>(state.activeConversation?.conversation_id ?? null);
  const hintRequestsRef = useRef(new Set<string>());
  activeCidRef.current = state.activeConversation?.conversation_id ?? null;

  const refreshSidebars = useCallback(
    async (pid: string) => {
      try {
        const [summary, misconceptions] = await Promise.all([
          api.learningSummary(pid),
          api.listMisconceptions(pid),
        ]);
        dispatch({ type: "SET_SUMMARY", summary, projectId: pid });
        dispatch({ type: "SET_MISCONCEPTIONS", misconceptions, projectId: pid });
      } catch {
        /* non-fatal */
      }
    },
    [dispatch],
  );

  const openConversation = useCallback(
    async (cid: string) => {
      const activity = state.conversationActivity;
      const projectId = state.activeProject?.project_id;
      try {
        const conv = await api.getConversation(cid);
        const messages = conv.messages.map((m) => ({
          role: m.role,
          content_blocks: m.content_blocks,
          message_id: m.message_id,
          run_id: m.run_id,
          created_at: m.created_at,
        })) as ApiMessage[];
        dispatch({ type: "SET_ACTIVE_CONVERSATION", conversation: conv, messages, activity, projectId });
        dispatch({ type: "SET_PENDING_TASK", pendingTask: detectPendingTask(messages), activity, projectId });
        dispatch({ type: "SET_FOLLOWUP_TASK", taskId: conv.practice_state?.phase === "FOLLOWUP" ? conv.practice_state.task_id : "", activity, projectId });
      } catch (e) {
        toast((e as Error).message || "这段对话暂时没打开，再试一次好吗？");
      }
    },
    [dispatch, state.conversationActivity, state.activeProject?.project_id],
  );

  const newConversation = useCallback(
    async (pid: string) => {
      const activity = state.conversationActivity;
      try {
        const conv = await api.createConversation(pid, activity);
        dispatch({
          type: "SET_CONVERSATIONS",
          conversations: [conv, ...state.conversations],
          activity,
          projectId: pid,
        });
        dispatch({ type: "SET_ACTIVE_CONVERSATION", conversation: conv, messages: [], activity, projectId: pid });
        dispatch({ type: "SET_PENDING_TASK", pendingTask: null, activity, projectId: pid });
        dispatch({ type: "SET_FOLLOWUP_TASK", taskId: "", activity, projectId: pid });
      } catch (e) {
        toast((e as Error).message || "新对话暂时没建好，再试一次好吗？");
      }
    },
    [dispatch, state.conversations, state.conversationActivity],
  );

  const reloadConversation = useCallback(
    async (cid: string) => {
      const fresh = await api.getConversation(cid);
      const messages = fresh.messages.map((m) => ({
        role: m.role,
        content_blocks: m.content_blocks,
        message_id: m.message_id,
        run_id: m.run_id,
        created_at: m.created_at,
      })) as ApiMessage[];
      dispatch({ type: "SET_MESSAGES", messages });
      dispatch({ type: "SET_PENDING_TASK", pendingTask: detectPendingTask(messages) });
      dispatch({ type: "SET_FOLLOWUP_TASK", taskId: fresh.practice_state?.phase === "FOLLOWUP" ? fresh.practice_state.task_id : "" });
    },
    [dispatch],
  );

  const sendPrompt = useCallback(async (prompt: string) => {
    const { activeConversation, activeProject, messages } = state;
    const selectedText = state.mode === "LEARN" ? state.textSelection : null;
    const text = prompt.trim();
    if (!text || !activeConversation) return;
    // P1-12: a stable idempotency key per send so a retried POST (network
    // blip, double-click) replays the same run instead of duplicating messages.
    const idempotencyKey = `send_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    // P1-07: remember which conversation this send belongs to. If the user
    // switches conversations mid-stream, later SET_MESSAGES/reload calls must
    // NOT touch the now-active conversation's view. Stale callbacks are dropped
    // by comparing against the live activeConversation at dispatch time.
    const sendConversationId = activeConversation.conversation_id;
    // Optimistic user message.
    dispatch({ type: "SET_COMPOSER", composer: "" });
    dispatch({ type: "SET_SENDING", sending: true, conversationId: sendConversationId });
    const optimistic: ApiMessage = {
      message_id: `opt_${Date.now()}`,
      role: "user",
      content_blocks: [{ type: "text", text }],
      created_at: new Date().toISOString(),
    };
    dispatch({ type: "APPEND_MESSAGE", message: optimistic });

    try {
      const res = await api.sendMessage(sendConversationId, text, idempotencyKey, {
        source_id: selectedText?.sourceId || state.reader?.sourceId,
        source_page: selectedText?.page || state.reader?.page,
        source_scope: selectedText ? "CURRENT_PAGE" : state.queryScope,
        selection_text: selectedText?.text,
        record_question_signal: state.recordQuestionSignal,
      });
      dispatch({ type: "SET_RECORD_QUESTION_SIGNAL", enabled: true });
      dispatch({ type: "SET_TEXT_SELECTION", selection: null });
      // Stream the run events into a live assistant message.
      const snap = newLiveRun();
      const liveId = `live_${res.run_id}`;

      // Render the live snapshot as ContentBlocks while streaming. We keep the
      // optimistic user message + the live assistant message in the array.
      const renderLive = () => {
        // P1-07: only update the view if this conversation is still active.
        if (activeCidRef.current !== sendConversationId) return;
        const blocks: ApiMessage["content_blocks"] = [];
        if (snap.toolStatus) blocks.push({ type: "status", text: snap.toolStatus });
        if (snap.fallback)
          blocks.push({ type: "status", text: "模型不可用，本次未生成回答" });
        if (snap.text) blocks.push({ type: "text", text: snap.text });
        for (const c of snap.citations)
          blocks.push({ type: "citation", label: c.label, source_id: c.book_id, book_id: c.book_id, page: c.page, chunk_id: c.chunk_id });
        if (snap.error) blocks.push({ type: "error", text: snap.error });
        const live: ApiMessage = {
          message_id: liveId,
          role: "assistant",
          content_blocks: blocks.length ? blocks : [{ type: "text", text: "" }],
          run_id: res.run_id,
          created_at: new Date().toISOString(),
        };
        dispatch({ type: "SET_MESSAGES", messages: [...messages, optimistic, live] });
      };
      renderLive(); // initial empty assistant bubble

      await new Promise<void>((resolve) => {
        api.subscribeRun(res.run_id, {
          onEvent: (type, data) => {
            applySseEvent(snap, type, data);
            renderLive();
          },
          onDone: resolve,
        });
      });

      // Replace the streamed placeholder with the authoritative conversation —
      // but only if this conversation is still the active one (P1-07).
      if (activeCidRef.current === sendConversationId) {
        await reloadConversation(sendConversationId);
      }
      if (activeProject) await refreshSidebars(activeProject.project_id);
    } catch (e) {
      if (activeCidRef.current === sendConversationId) {
        dispatch({
          type: "APPEND_MESSAGE",
          message: {
            message_id: `send_error_${Date.now()}`,
            role: "assistant",
            content_blocks: [{
              type: "error",
              text: "这次没有发送成功。你的输入已经保留，可以检查网络后重新发送。",
            }],
            created_at: new Date().toISOString(),
          } as ApiMessage,
        });
        dispatch({ type: "SET_COMPOSER", composer: text });
      }
    } finally {
      // Sending is global UI state. Always clear it even if the user switched
      // conversations while the old request was completing.
      dispatch({ type: "SET_SENDING", sending: false, conversationId: sendConversationId });
    }
  }, [state, dispatch, reloadConversation, refreshSidebars]);

  const send = useCallback(async () => {
    await sendPrompt(state.composer);
  }, [sendPrompt, state.composer]);

  const startTask = useCallback(async ({
    selection = "RECOMMENDED",
    conceptId = "",
    fromTaskId = "",
    mode,
  }: {
    selection?: ConsolidationFilter;
    conceptId?: string;
    fromTaskId?: string;
    mode?: ConsolidationMode;
  }) => {
    const project = state.activeProject;
    if (!project) return;
    if (state.activeFollowupTaskId) {
      toast("请先结束上一题的追问，再开始下一题。");
      return;
    }
    const projectId = project.project_id;
    const taskMode = mode || "PRACTICE";
    const targetMode = "REVIEW" as const;
    const activity: ConversationActivity = "REVIEW";
    try {
      if (state.mode !== targetMode) {
        dispatch({ type: "SET_MODE", mode: targetMode });
        await api.updateProject(projectId, {
          default_mode: "Review",
        });
      }

      let conversations = state.mode === targetMode ? state.conversations : [];
      let conversation = state.mode === targetMode && state.activeConversation?.activity_type === activity
        ? state.activeConversation
        : null;
      if (!conversation) {
        conversations = await api.listConversations(projectId, activity);
        if (!conversations.length) conversations = [await api.createConversation(projectId, activity)];
        conversation = conversations[0];
      }
      dispatch({ type: "SET_CONVERSATIONS", conversations, activity, projectId });

      const before = await api.getConversation(conversation.conversation_id);
      const beforeMessages = before.messages as ApiMessage[];
      dispatch({ type: "SET_ACTIVE_CONVERSATION", conversation, messages: beforeMessages, activity, projectId });
      dispatch({ type: "SET_PENDING_TASK", pendingTask: detectPendingTask(beforeMessages), activity, projectId });
      dispatch({ type: "SET_FOLLOWUP_TASK", taskId: before.practice_state?.phase === "FOLLOWUP" ? before.practice_state.task_id : "", activity, projectId });
      if (before.practice_state?.phase === "FOLLOWUP") {
        toast("请先结束上一题的追问，再开始下一题。");
        return;
      }
      dispatch({ type: "SET_SENDING", sending: true });

      const created = await api.createTask(conversation.conversation_id, {
        mode: taskMode,
        selection,
        concept_id: conceptId,
        from_task_id: fromTaskId,
        idempotency_key: `task_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`,
      });
      if (created.message) {
        dispatch({ type: "APPEND_MESSAGE", message: created.message });
        dispatch({
          type: "SET_PENDING_TASK",
          pendingTask: detectPendingTask([...beforeMessages, created.message]),
          activity,
          projectId,
        });
        toast("已生成练习题，请在下方作答。");
      } else {
        const fresh = await api.getConversation(conversation.conversation_id);
        const messages = fresh.messages as ApiMessage[];
        dispatch({ type: "SET_ACTIVE_CONVERSATION", conversation, messages, activity, projectId });
        dispatch({ type: "SET_PENDING_TASK", pendingTask: detectPendingTask(messages), activity, projectId });
        if (created.existing) toast("当前题仍待完成：可补充答案、查看讲解或跳过后再开始新题。");
      }
      await refreshSidebars(projectId);
    } catch (error) {
      toast((error as Error).message || "这道题暂时没有准备好，请稍后再试。");
    } finally {
      dispatch({ type: "SET_SENDING", sending: false });
    }
  }, [state.activeProject, state.activeConversation, state.activeFollowupTaskId, state.conversations, state.mode, dispatch, refreshSidebars]);

  const openTaskSource = useCallback(async (sourceId: string, page: number) => {
    const project = state.activeProject;
    if (!project) return;
    const projectId = project.project_id;
    dispatch({ type: "SET_READER", reader: { sourceId, page }, projectId });
    if (state.mode === "LEARN") return;
    dispatch({ type: "SET_MODE", mode: "LEARN" });
    try {
      await Promise.all([
        api.updateProject(projectId, { default_mode: "Deep Learning", last_source_id: sourceId, last_source_page: page }),
        loadActivityConversation(projectId, "LEARN", dispatch),
      ]);
    } catch (error) {
      toast((error as Error).message || "暂时无法回到原文");
    }
  }, [state.activeProject, state.mode, dispatch]);

  const finishConsolidation = useCallback(async () => {
    const conversation = state.activeConversation;
    const project = state.activeProject;
    if (!conversation || !project) return;
    try {
      dispatch({ type: "SET_SENDING", sending: true });
      await api.finishConsolidation(conversation.conversation_id);
      await reloadConversation(conversation.conversation_id);
      await refreshSidebars(project.project_id);
    } catch (error) {
      toast((error as Error).message || "暂时无法生成本次小结");
    } finally {
      dispatch({ type: "SET_SENDING", sending: false });
    }
  }, [state.activeConversation, state.activeProject, dispatch, reloadConversation, refreshSidebars]);

  const explainTask = useCallback(async (taskId: string) => {
    const conversation = state.activeConversation;
    if (!conversation) return;
    try {
      dispatch({ type: "SET_SENDING", sending: true });
      await api.explainTask(conversation.conversation_id, taskId);
      await reloadConversation(conversation.conversation_id);
    } catch (error) {
      toast((error as Error).message || "暂时无法生成讲解");
    } finally {
      dispatch({ type: "SET_SENDING", sending: false });
    }
  }, [state.activeConversation, state.activeFollowupTaskId, dispatch, reloadConversation]);

  const followupTask = useCallback(async (taskId: string, question: string) => {
    const conversation = state.activeConversation;
    if (!conversation || !question.trim() || state.activeFollowupTaskId !== taskId) return;
    try {
      dispatch({ type: "SET_SENDING", sending: true });
      await api.followupTask(conversation.conversation_id, taskId, question.trim());
      dispatch({ type: "SET_COMPOSER", composer: "" });
      await reloadConversation(conversation.conversation_id);
    } catch (error) {
      toast((error as Error).message || "这条题后追问暂时没有回答成功");
    } finally {
      dispatch({ type: "SET_SENDING", sending: false });
    }
  }, [state.activeConversation, dispatch, reloadConversation]);

  const startTaskFollowup = useCallback(async (taskId: string) => {
    const conversation = state.activeConversation;
    if (!conversation) return;
    try {
      dispatch({ type: "SET_SENDING", sending: true });
      await api.startTaskFollowup(conversation.conversation_id, taskId);
      dispatch({ type: "SET_FOLLOWUP_TASK", taskId });
      dispatch({ type: "SET_COMPOSER", composer: "" });
    } catch (error) {
      toast((error as Error).message || "无法开始题后追问");
    } finally {
      dispatch({ type: "SET_SENDING", sending: false });
    }
  }, [state.activeConversation, dispatch]);

  const endTaskFollowup = useCallback(async (taskId: string) => {
    const conversation = state.activeConversation;
    if (!conversation) return;
    try {
      dispatch({ type: "SET_SENDING", sending: true });
      await api.closeTaskFollowup(conversation.conversation_id, taskId);
      dispatch({ type: "SET_FOLLOWUP_TASK", taskId: "" });
      dispatch({ type: "SET_COMPOSER", composer: "" });
      await reloadConversation(conversation.conversation_id);
    } catch (error) {
      toast((error as Error).message || "无法结束题后追问");
    } finally {
      dispatch({ type: "SET_SENDING", sending: false });
    }
  }, [state.activeConversation, dispatch, reloadConversation]);

  const stopGeneration = useCallback(async () => {
    const msgs = state.messages;
    const last = msgs[msgs.length - 1];
    if (last?.run_id) {
      try {
        await api.cancelRun(last.run_id);
      } catch {
        /* ignore */
      }
    }
    dispatch({ type: "SET_SENDING", sending: false });
  }, [state.messages, dispatch]);

  const restoreLastMessage = useCallback(() => {
    const lastUser = [...state.messages].reverse().find((message) => message.role === "user");
    const text = lastUser?.content_blocks.find((block) => block.type === "text")?.text || "";
    if (text) {
      dispatch({ type: "SET_COMPOSER", composer: text });
      window.setTimeout(() => document.getElementById("composer")?.focus(), 0);
    }
  }, [state.messages, dispatch]);

  const submitTaskAnswer = useCallback(async () => {
    // When a pending task exists, the composer holds the answer; send() routes
    // it as SUBMIT_ANSWER (the orchestrator decides server-side).
    const text = state.composer.trim();
    if (!text) return;
    await send();
  }, [state.composer, send]);

  const requestHint = useCallback(
    async (taskId: string) => {
      if (hintRequestsRef.current.has(taskId)) return;
      hintRequestsRef.current.add(taskId);
      try {
        const res = await api.requestHint(taskId);
        dispatch({
          type: "APPEND_MESSAGE",
          message: {
            message_id: `hint_${Date.now()}`,
            role: "assistant",
            content_blocks: [
              { type: "status", text: res.hint_notice },
              { type: "text", text: res.hint_text },
            ],
            created_at: new Date().toISOString(),
          } as ApiMessage,
        });
      } catch (e) {
        toast((e as Error).message || "无法获取提示");
      } finally {
        hintRequestsRef.current.delete(taskId);
      }
    },
    [dispatch],
  );

  const skipTask = useCallback(
    async (taskId: string) => {
      try {
        await api.skipTask(taskId);
        dispatch({ type: "SET_PENDING_TASK", pendingTask: null });
        if (state.activeConversation) await reloadConversation(state.activeConversation.conversation_id);
        toast("已跳过本题，不会记为错误；现在可以开始下一题。");
      } catch (e) {
        toast((e as Error).message || "跳过失败");
      }
    },
    [dispatch, state.activeConversation, reloadConversation],
  );

  const uploadFile = useCallback(
    async (file: File) => {
      const { activeProject } = state;
      if (!file || !activeProject) return;
      const projectId = activeProject.project_id;
      dispatch({ type: "SET_UPLOADING", uploading: true, projectId });
      try {
        const res = await api.uploadSource(
          projectId,
          file,
          file.name.replace(/\.pdf$/i, ""),
        );
        if (res.reused) toast("这份资料之前整理过啦，已经直接放进来了。");
        // The original file is available as soon as the upload request returns.
        // Show it immediately while parsing/indexing continues in the source rail.
        const initialSources = await api.listSources(projectId);
        dispatch({ type: "SET_SOURCES", sources: initialSources, projectId });
        dispatch({ type: "SET_READER", reader: { sourceId: res.source_id || res.book_id, page: 1 }, projectId });
        const finalJob = await api.pollJob(res.job_id, {
          onProgress: (job) => dispatch({ type: "SET_JOB", job, projectId }),
        });
        // P1-08: on success refresh sources + summary and clear the job card. On
        // failure (or timeout) KEEP the job so IngestionCard shows the reason +
        // retry — the old code always did SET_JOB=null, hiding the failure.
        if (finalJob && finalJob.state === "SUCCEEDED") {
          const [sources, summary] = await Promise.all([
            api.listSources(projectId),
            api.learningSummary(projectId),
          ]);
          dispatch({ type: "SET_SOURCES", sources, projectId });
          dispatch({ type: "SET_SUMMARY", summary, projectId });
          dispatch({ type: "SET_JOB", job: null, projectId });
        }
        // otherwise: keep the failed job card visible with its error + retry
      } catch (e) {
        toast((e as Error).message || "这份资料没传上去，再试一次好吗？");
      } finally {
        dispatch({ type: "SET_UPLOADING", uploading: false, projectId });
      }
    },
    [state.activeProject, dispatch],
  );

  return {
    openConversation,
    newConversation,
    send,
    sendPrompt,
    startTask,
    openTaskSource,
    finishConsolidation,
    explainTask,
    followupTask,
    startTaskFollowup,
    endTaskFollowup,
    stopGeneration,
    restoreLastMessage,
    submitTaskAnswer,
    requestHint,
    skipTask,
    uploadFile,
  };
}
