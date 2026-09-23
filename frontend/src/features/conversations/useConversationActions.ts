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

const BINARY_TREE_PROFILE_DEMO_QUERY = "我已经读过这个了，那我还有什么要学的";
const BINARY_TREE_PRACTICE_DEMO_TASK_ID = "demo_binary_tree_independent_practice";
const BINARY_TREE_ARCHIVE_DEMO_TASK_ID = "demo_binary_tree_followup_practice";

function binaryTreeProfileDemoEnabled(text: string): boolean {
  // A presentation-only fixture, opt-in through the URL.  It never calls the
  // API or writes learning evidence, so the ordinary product flow and the
  // learner's real archive remain untouched.
  const demo = new URLSearchParams(window.location.search).get("demo");
  const compact = text.replace(/\s+/g, "").replace(/[？?！!。．]/g, "");
  return demo === "binary-tree-profile" && compact === BINARY_TREE_PROFILE_DEMO_QUERY;
}

function binaryTreeProfileDemoBlocks(): ApiMessage["content_blocks"] {
  return [
    {
      type: "text",
      text: "根据目前的阅读和提问记录，你已经阅读了二叉树的定义与实现方式，并在当前对话中主动询问过代码如何描述。现有档案显示该知识点已标记为“已学”，但还没有独立作答证据，因此我不能据此判断你具体哪里不会。\n\n下一步最值得验证的是：能否不看提示地说明结点、左/右孩子引用各自承担什么作用；再给出空树、单结点和只有一个孩子时的处理，并解释为什么这种表示能支持后续的遍历和插入等操作。完成一次独立练习后，系统才能把“已阅读”升级为有依据的掌握记录。",
    },
    {
      type: "context",
      data: {
        kind: "answer_context",
        scope: "当前资料 · 二叉树学习单元",
        reason: "教材位置用于解释二叉树的定义与表示；学习档案和对话上下文用于判断下一步验证目标。",
        items: [
          { source_id: "book_bda7a5df20e9", title: "dsacpp-3rd-edn", locator: "p.132–135 · 第5章 二叉树 · §5.1 二叉树及其表示" },
          { source_id: "book_bda7a5df20e9", title: "dsacpp-3rd-edn", locator: "p.139–140 · 第5章 二叉树 · §5.3 二叉树的实现" },
        ],
        learner_basis: [
          { label: "本轮提问", text: "我已经读过这个了，那我还有什么要学的？" },
          { label: "上下文解析", text: "将“这个”解析为上一轮讨论的“§5.1 二叉树及其表示”。" },
          { label: "学习档案", text: "已标记已学；已阅读不等于独立掌握，目前尚无独立作答验证。" },
          { label: "提问记录", text: "已提问二叉树如何用代码描述，可作为后续练习的上下文，不作为掌握证据。" },
        ],
      },
    },
    {
      type: "question_signal",
      data: {
        signal_id: "demo_binary_tree_profile",
        record_label: "已阅读 · 待验证",
        message: "已记录：你已阅读该知识点，并了解基本实现方式，但结点表示、边界处理与独立应用尚待验证。",
        concepts: [{ concept_id: "lc_83f419949512bb34", name: "§5.1 二叉树及其表示", question_count: 1 }],
      },
    },
    {
      type: "citation",
      book_id: "book_bda7a5df20e9",
      source_id: "book_bda7a5df20e9",
      page: "132",
      label: "[1] dsacpp-3rd-edn · p.132–135 · §5.1 二叉树及其表示",
    },
    {
      type: "citation",
      book_id: "book_bda7a5df20e9",
      source_id: "book_bda7a5df20e9",
      page: "139",
      label: "[2] dsacpp-3rd-edn · p.139–140 · §5.3 二叉树的实现",
    },
  ];
}

function binaryTreePracticeDemoEnabled(): boolean {
  // Like the learning-profile fixture above, this is deliberately opt-in and
  // client-only. It gives the presentation a reproducible assessment story
  // without fabricating evidence in the learner's real archive.
  return new URLSearchParams(window.location.search).get("demo") === "practice-binary-tree";
}

function binaryTreeArchiveDemoEnabled(): boolean {
  return new URLSearchParams(window.location.search).get("demo") === "binary-tree-archive";
}

function binaryTreeAssessmentDemoEnabled(): boolean {
  return binaryTreePracticeDemoEnabled() || binaryTreeArchiveDemoEnabled();
}

function binaryTreePracticeTaskMessage(): ApiMessage {
  return {
    message_id: `demo_task_${Date.now()}`,
    role: "assistant",
    created_at: new Date().toISOString(),
    content_blocks: [{
      type: "task",
      data: {
        kind: "quiz",
        task_id: BINARY_TREE_PRACTICE_DEMO_TASK_ID,
        prompt_text: "请用自己的话说明：二叉树结点为什么要分别保存 left 和 right 两个孩子引用？当某个孩子不存在时应如何表示？这样的结点表示为什么能支持后续的遍历操作？",
        focus: "§5.1 二叉树及其表示",
        source_scope: [{
          source_id: "book_bda7a5df20e9",
          title: "dsacpp-3rd-edn",
          locator: "p.132–135 · 第5章 二叉树 · §5.1 二叉树及其表示",
        }],
        generation_reason: "你已标记已学，并阅读过定义与实现；这题用于收集第一次独立作答证据（L1）。",
        generation_mode: "llm",
        generation_notice: "已根据当前资料、学习档案和本轮练习目标生成题目；提交后将依据作答要点更新学习档案。",
      },
    }],
  } as ApiMessage;
}

function binaryTreePracticeResultMessage(answer: string): ApiMessage {
  return {
    message_id: `demo_judgment_${Date.now()}`,
    role: "assistant",
    created_at: new Date().toISOString(),
    content_blocks: [
      {
        type: "task",
        data: {
          kind: "judgment",
          task_id: BINARY_TREE_PRACTICE_DEMO_TASK_ID,
          judgment: {
            result: "PASS",
            judgment_status: "DECIDED",
            reason: "你的回答说明了左右孩子引用的分工、空孩子的表示，并把这种递归结构与遍历联系起来，满足本轮独立作答的验证目标。",
            criterion_results: [
              { criterion_id: "node_links", satisfied: true, note: "说明 left / right 分别连接左右子树" },
              { criterion_id: "empty_child", satisfied: true, note: "说明不存在的孩子以 None / 空指针表示" },
              { criterion_id: "traversal", satisfied: true, note: "说明表示能够递归支持遍历" },
            ],
          },
          written: true,
          next_action_code: "VERIFY",
          source_scope: [{
            source_id: "book_bda7a5df20e9",
            title: "dsacpp-3rd-edn",
            page: 132,
            locator: "第5章 二叉树 · §5.1 二叉树及其表示",
          }],
        },
      },
      {
        type: "state_change",
        data: {
          mastery_transitions: [{
            concept_id: "lc_83f419949512bb34",
            old_state: "L0",
            new_state: "L1",
          }],
        },
      },
      {
        type: "context",
        data: {
          kind: "answer_context",
          scope: "学习档案更新 · 二叉树",
          reason: "本轮为未查看提示的独立作答；判分结果与教材依据共同构成本次状态更新的证据。",
          items: [{
            source_id: "book_bda7a5df20e9",
            title: "dsacpp-3rd-edn",
            locator: "p.132–135 · 第5章 二叉树 · §5.1 二叉树及其表示",
          }],
          learner_basis: [
            { label: "本轮作答", text: answer },
            { label: "独立性", text: "未请求提示、未查看讲解后作答，可作为 L1 验证证据。" },
            { label: "已记录", text: "§5.1 二叉树及其表示：从“已阅读 · 待验证”更新为“L1 已验证”。" },
            { label: "下一步建议", text: "换一种结点结构或边界情境，继续验证遍历与实现的应用能力。" },
          ],
        },
      },
      {
        type: "status",
        text: "已记录：本次独立作答通过；学习档案保留了作答事实、判分依据和下一步验证建议。",
      },
      {
        type: "task",
        data: {
          kind: "task_complete",
          task_id: BINARY_TREE_PRACTICE_DEMO_TASK_ID,
          completion_status: "ANSWERED",
          source_scope: [{
            source_id: "book_bda7a5df20e9",
            title: "dsacpp-3rd-edn",
            page: 132,
            locator: "第5章 二叉树 · §5.1 二叉树及其表示",
          }],
        },
      },
    ],
  } as ApiMessage;
}

function binaryTreeArchiveTaskMessage(): ApiMessage {
  return {
    message_id: `demo_archive_task_${Date.now()}`,
    role: "assistant",
    created_at: new Date().toISOString(),
    content_blocks: [{
      type: "task",
      data: {
        kind: "quiz",
        task_id: BINARY_TREE_ARCHIVE_DEMO_TASK_ID,
        prompt_text: "你已经能说明二叉树结点的 left/right 引用。现在请进一步描述递归前序遍历的实现：空树应如何处理？访问一个非空结点时，根、左子树、右子树的访问次序分别是什么？请结合 left/right 的作用说明理由。",
        focus: "§5.1 二叉树及其表示 · 结点表示到遍历应用",
        source_scope: [{
          source_id: "book_bda7a5df20e9",
          title: "dsacpp-3rd-edn",
          locator: "p.132–135 · §5.1 二叉树及其表示；p.145–147 · §5.4 遍历",
        }],
        generation_reason: "学习档案中已有一次结点表示的独立通过记录；本题换到遍历实现情境，确认你能否把表示理解应用到操作过程。",
        generation_mode: "llm",
        generation_notice: "已根据学习档案中的既有证据选择后续验证目标；本次判分会追加到同一知识点的证据链。",
      },
    }],
  } as ApiMessage;
}

function binaryTreeArchiveResultMessage(answer: string): ApiMessage {
  return {
    message_id: `demo_archive_judgment_${Date.now()}`,
    role: "assistant",
    created_at: new Date().toISOString(),
    content_blocks: [
      {
        type: "task",
        data: {
          kind: "judgment",
          task_id: BINARY_TREE_ARCHIVE_DEMO_TASK_ID,
          judgment: {
            result: "PARTIAL",
            judgment_status: "DECIDED",
            reason: "你正确说明了 left/right 的分工与空孩子表示，也知道遍历需要递归访问子树；但尚未明确空树的终止条件，以及前序遍历“根→左→右”的访问时机。",
            criterion_results: [
              { criterion_id: "node_links", satisfied: true, note: "说明 left / right 的左右子树分工" },
              { criterion_id: "empty_child", satisfied: true, note: "说明空孩子以 None / 空指针表示" },
              { criterion_id: "traversal_base_case", satisfied: false, note: "遗漏空树基线和根→左→右访问次序" },
            ],
          },
          written: true,
          next_action_code: "REMEDIATE",
          source_scope: [{
            source_id: "book_bda7a5df20e9",
            title: "dsacpp-3rd-edn",
            page: 145,
            locator: "第5章 二叉树 · §5.4 遍历",
          }],
        },
      },
      {
        type: "context",
        data: {
          kind: "answer_context",
          scope: "学习档案追加 · 二叉树",
          reason: "本轮独立作答被判为部分通过；系统保留已覆盖内容与遗漏项，而不是把一次部分正确简单归为“不会”。",
          items: [{
            source_id: "book_bda7a5df20e9",
            title: "dsacpp-3rd-edn",
            locator: "p.132–135 · §5.1 二叉树及其表示；p.145–147 · §5.4 遍历",
          }],
          learner_basis: [
            { label: "本轮作答", text: answer },
            { label: "本次事实", text: "已覆盖结点引用和空孩子表示；遗漏空树基线与前序访问顺序。" },
            { label: "既有证据", text: "上一次独立作答已通过结点表示（L1）；本次部分通过不会抹去该通过记录。" },
            { label: "下一步建议", text: "用空树、单结点和仅有左孩子三种边界情境，手写根→左→右的递归过程后再验证。" },
          ],
        },
      },
      {
        type: "status",
        text: "已记录：本次部分通过已追加到 §5.1 的证据链；当前保持 L1，待针对遍历边界完成下一次独立验证。",
      },
      {
        type: "task",
        data: {
          kind: "task_complete",
          task_id: BINARY_TREE_ARCHIVE_DEMO_TASK_ID,
          completion_status: "ANSWERED",
          source_scope: [{
            source_id: "book_bda7a5df20e9",
            title: "dsacpp-3rd-edn",
            page: 145,
            locator: "第5章 二叉树 · §5.4 遍历",
          }],
        },
      },
    ],
  } as ApiMessage;
}

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
    if (binaryTreeProfileDemoEnabled(text)) {
      dispatch({ type: "SET_COMPOSER", composer: "" });
      dispatch({ type: "APPEND_MESSAGE", message: {
        message_id: `demo_user_${Date.now()}`,
        role: "user",
        content_blocks: [{ type: "text", text }],
        created_at: new Date().toISOString(),
      } });
      dispatch({ type: "APPEND_MESSAGE", message: {
        message_id: `demo_binary_tree_profile_${Date.now()}`,
        role: "assistant",
        content_blocks: binaryTreeProfileDemoBlocks(),
        created_at: new Date().toISOString(),
      } });
      return;
    }
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
      if (binaryTreeAssessmentDemoEnabled()) {
        const demoTask = binaryTreeArchiveDemoEnabled()
          ? binaryTreeArchiveTaskMessage()
          : binaryTreePracticeTaskMessage();
        // A persisted real review conversation may contain an earlier task.
        // The presentation fixture must never juxtapose that task with this
        // fixture's answer: it would make a correct binary-tree answer appear
        // to fail an unrelated multiway-tree question. Keep the demo viewport
        // self-contained while leaving the server history untouched.
        dispatch({ type: "SET_MESSAGES", messages: [demoTask] });
        dispatch({
          type: "SET_PENDING_TASK",
          pendingTask: detectPendingTask([demoTask]),
          activity,
          projectId,
        });
        toast("已生成学习检测题，请在下方独立作答。 ");
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
    const project = state.activeProject;
    if (!conversation) return;
    try {
      dispatch({ type: "SET_SENDING", sending: true });
      await api.explainTask(conversation.conversation_id, taskId);
      await reloadConversation(conversation.conversation_id);
      // Showing an explanation is durable non-verifying learning evidence.
      // Keep an already-open learning sidebar in sync instead of requiring a
      // page refresh before the learner can see that interaction.
      if (project) await refreshSidebars(project.project_id);
    } catch (error) {
      toast((error as Error).message || "暂时无法生成讲解");
    } finally {
      dispatch({ type: "SET_SENDING", sending: false });
    }
  }, [state.activeConversation, state.activeProject, dispatch, reloadConversation, refreshSidebars]);

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
  }, [state.activeConversation, state.activeFollowupTaskId, dispatch, reloadConversation]);

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
    if (binaryTreeAssessmentDemoEnabled() && state.pendingTask?.task_id === BINARY_TREE_PRACTICE_DEMO_TASK_ID) {
      dispatch({ type: "SET_COMPOSER", composer: "" });
      dispatch({ type: "APPEND_MESSAGE", message: {
        message_id: `demo_answer_${Date.now()}`,
        role: "user",
        content_blocks: [{ type: "text", text }],
        created_at: new Date().toISOString(),
      } as ApiMessage });
      dispatch({ type: "APPEND_MESSAGE", message: binaryTreePracticeResultMessage(text) });
      dispatch({ type: "SET_PENDING_TASK", pendingTask: null });
      toast("本题已判定，学习档案已更新。 ");
      return;
    }
    if (binaryTreeArchiveDemoEnabled() && state.pendingTask?.task_id === BINARY_TREE_ARCHIVE_DEMO_TASK_ID) {
      dispatch({ type: "SET_COMPOSER", composer: "" });
      dispatch({ type: "APPEND_MESSAGE", message: {
        message_id: `demo_archive_answer_${Date.now()}`,
        role: "user",
        content_blocks: [{ type: "text", text }],
        created_at: new Date().toISOString(),
      } as ApiMessage });
      dispatch({ type: "APPEND_MESSAGE", message: binaryTreeArchiveResultMessage(text) });
      dispatch({ type: "SET_PENDING_TASK", pendingTask: null });
      toast("本次部分通过已记录；可打开学习档案查看完整证据链。 ");
      return;
    }
    await send();
  }, [state.composer, state.pendingTask?.task_id, dispatch, send]);

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
        // A hint is recorded as non-verifying evidence.  Refresh the visible
        // profile/timeline if the learner already has it open.
        if (state.activeProject) await refreshSidebars(state.activeProject.project_id);
      } catch (e) {
        toast((e as Error).message || "无法获取提示");
      } finally {
        hintRequestsRef.current.delete(taskId);
      }
    },
    [dispatch, refreshSidebars, state.activeProject],
  );

  const skipTask = useCallback(
    async (taskId: string) => {
      try {
        await api.skipTask(taskId);
        dispatch({ type: "SET_PENDING_TASK", pendingTask: null });
        if (state.activeConversation) await reloadConversation(state.activeConversation.conversation_id);
        if (state.activeProject) await refreshSidebars(state.activeProject.project_id);
        toast("已跳过本题，不会记为错误；现在可以开始下一题。");
      } catch (e) {
        toast((e as Error).message || "跳过失败");
      }
    },
    [dispatch, state.activeConversation, state.activeProject, reloadConversation, refreshSidebars],
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
