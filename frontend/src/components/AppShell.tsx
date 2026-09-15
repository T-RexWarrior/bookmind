import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useApp } from "../store/appStore";
import { Button, Dialog, Sheet, toast } from "./ui/primitives";
import { ModeSwitcher, MODE_META } from "../features/assessment/ModeSwitcher";
import { ConsolidationOverview } from "../features/assessment/ConsolidationOverview";
import { ConversationPane } from "../features/conversations/ConversationPane";
import { ConversationList } from "../features/conversations/ConversationList";
import { LearningSidebar } from "../features/learning/LearningSidebar";
import { SourceLibrary } from "../features/sources/SourceLibrary";
import { ProjectSettingsDialog } from "../features/projects/ProjectSettingsDialog";
import type { LearningSourceView } from "../types/blocks";
import * as api from "../api/client";

const ReaderView = lazy(() => import("../features/reader/ReaderView").then(
  (module) => ({ default: module.ReaderView }),
));

export function AppShell() {
  const { state, dispatch } = useApp();
  const navigate = useNavigate();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const positionTimer = useRef<number | null>(null);
  const project = state.activeProject!;
  const activeSource = state.sources.find((source) => source.source_id === state.reader?.sourceId) || state.sources[0];
  const mode = MODE_META[state.mode];

  // A newly opened project has a first source before it has an explicit reader
  // position.  Seed that position so “当前资料/当前页” is a real scope choice
  // rather than a disabled-looking control until the user manually clicks PDF.
  useEffect(() => {
    if (activeSource && !state.reader) {
      dispatch({ type: "SET_READER", reader: { sourceId: activeSource.source_id, page: 1 } });
    }
  }, [activeSource?.source_id, state.reader, dispatch]);

  const goHome = () => {
    dispatch({ type: "CLEAR_PROJECT" });
    navigate("/");
  };

  const persistPosition = useCallback((sourceId: string, page: number) => {
    dispatch({ type: "SET_READER", reader: { sourceId, page } });
    if (positionTimer.current) window.clearTimeout(positionTimer.current);
    positionTimer.current = window.setTimeout(() => {
      void api.updateProject(project.project_id, { last_source_id: sourceId, last_source_page: page });
    }, 650);
  }, [dispatch, project.project_id]);

  const selectSource = (source: LearningSourceView, page = 1) => persistPosition(source.source_id, page);

  return (
    <main className={`workspace mode-${state.mode.toLowerCase()}`}>
      <header className="workspace-header">
        <button className="brand compact" onClick={goHome}><span>迹</span><div><strong>学迹</strong><small>资料学习空间</small></div></button>
        <div className="workspace-header__context">
          <div><h1>{project.name}</h1><button onClick={() => setSettingsOpen(true)}>编辑学习设置</button></div>
          <p>{project.current_plan || project.goal || "挑一份资料，我们就从感兴趣的地方开始吧"}</p>
        </div>
        <div className="workspace-header__actions">
          <button className="header-action" onClick={() => dispatch({ type: "SET_DRAWER", left: true })}>☰ <span>学习对话</span></button>
          <button className="header-action" onClick={() => dispatch({ type: "SET_DRAWER", right: true })}>◌ <span>学习状态</span></button>
          <button className="header-action" onClick={() => setSettingsOpen(true)}>•••</button>
        </div>
      </header>

      <div className="workspace-nav">
        <ModeSwitcher />
        <div className="activity-current"><span>{mode.icon}</span><div><strong>{mode.label}</strong><small>{mode.short}</small></div></div>
      </div>

      <ActivityPage
        activeSource={activeSource}
        onSelectSource={selectSource}
        onPageChange={(page) => activeSource && persistPosition(activeSource.source_id, page)}
      />

      <Sheet open={state.leftDrawerOpen} side="left" title="学习对话" onClose={() => dispatch({ type: "SET_DRAWER", left: false })}>
        <ConversationList />
      </Sheet>
      <Sheet open={state.rightDrawerOpen} side="right" title="学习状态" onClose={() => dispatch({ type: "SET_DRAWER", right: false })}>
        <LearningSidebar />
      </Sheet>
      <ProjectSettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onDelete={() => { setSettingsOpen(false); setDeleteOpen(true); }}
      />
      <Dialog open={deleteOpen} onClose={() => setDeleteOpen(false)} title="删除学习空间" footer={<><Button onClick={() => setDeleteOpen(false)}>取消</Button><Button variant="danger" onClick={async () => {
        try { await api.deleteProject(project.project_id); goHome(); } catch (error) { toast((error as Error).message || "删除失败"); }
      }}>删除</Button></>}>
        <p>空间会从首页移除，已经形成的学习证据仍会保留。</p>
      </Dialog>
    </main>
  );
}

function ActivityPage({ activeSource, onSelectSource, onPageChange }: {
  activeSource?: LearningSourceView;
  onSelectSource: (source: LearningSourceView, page?: number) => void;
  onPageChange: (page: number) => void;
}) {
  const { state, dispatch } = useApp();
  const sourceViewer = activeSource ? (
    <Suspense fallback={<div className="reader-loading" role="status"><span className="thinking-spinner" />正在打开资料…</div>}>
      <ReaderView
        sourceId={activeSource.source_id}
        title={activeSource.title}
        initialPage={state.reader?.page || 1}
        onPageChange={onPageChange}
        onAskSelection={(text, page) => {
          dispatch({ type: "SET_TEXT_SELECTION", selection: { text, sourceId: activeSource.source_id, page } });
          dispatch({ type: "SET_QUERY_SCOPE", scope: "CURRENT_PAGE" });
          dispatch({ type: "SET_COMPOSER", composer: "请解释这段原文，并说明它和当前知识点的关系。" });
          window.setTimeout(() => document.getElementById("composer")?.focus(), 0);
        }}
      />
    </Suspense>
  ) : <NoSource />;

  if (state.mode === "LEARN") {
    return (
      <div className="activity-layout reading-layout">
        <SourceLibrary onSelect={onSelectSource} />
        <div className="activity-main">{sourceViewer}</div>
        <div className="activity-aside assistant-aside"><ConversationPane heading="边学边问" suggestions={["帮我讲讲这一页最重要的内容。", "能举个更直观的例子吗？", "这部分需要哪些前置知识？", "我对这部分还有疑问，请换一种方式解释。"]} /></div>
      </div>
    );
  }

  if (state.mode === "REVIEW") {
    return (
      <div className="activity-layout review-layout">
        <ConsolidationOverview />
        <ConversationPane focused heading="练习巩固" suggestions={["从我问过但还没验证的知识点出一道题。", "从我最薄弱的知识点出一道练习题。", "给我一道到期复验题。", "先帮我理清上次理解偏差的地方。"]} />
        <aside className="evidence-rail"><LearningSidebar /></aside>
      </div>
    );
  }

  if (state.mode === "ASSESSMENT") {
    return (
      <div className="activity-layout review-layout">
        <ConsolidationOverview />
        <ConversationPane focused heading="能力评估" suggestions={["从我问过但还没验证的知识点出一道评估题。", "从我最薄弱的知识点出一道评估题。", "给我一道到期复验题。"]} />
        <aside className="evidence-rail"><LearningSidebar /></aside>
      </div>
    );
  }

  return null;
}

function NoSource() {
  return <div className="no-source"><span>＋</span><h2>先放进一份学习资料吧</h2><p>原文件会马上显示，我会在后台慢慢读页面、认目录，再整理出可以提问的内容。</p><button className="btn primary" onClick={() => document.getElementById("learning-source-upload")?.click()}>选择 PDF 资料</button></div>;
}
