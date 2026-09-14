// App — routes + bootstrap. On mount, bootstrap the anonymous session and load
// the project list. Routes: / (welcome), /projects/:id (project shell).
// The server-derived learner cookie means no internal IDs are ever typed.

import { useEffect, useState } from "react";
import { HashRouter, Routes, Route, useNavigate, useParams } from "react-router-dom";
import { AppProvider, useApp, activityFromMode } from "./store/appStore";
import { WelcomePage } from "./features/projects/WelcomePage";
import { AppShell } from "./components/AppShell";
import { ToastHost, toast } from "./components/ui/primitives";
import { modeFromProjectDefault } from "./features/assessment/ModeSwitcher";
import { detectPendingTask } from "./features/shared";
import * as api from "./api/client";
import type { LearningSourceView, LearningSummary, MisconceptionView } from "./types/blocks";

export default function App() {
  return (
    <AppProvider>
      <HashRouter>
        <Bootstrap />
        <Routes>
          <Route path="/" element={<WelcomePage onOpenProject={(pid) => window.location.assign(`#/projects/${pid}`)} />} />
          <Route path="/projects/:projectId" element={<ProjectRoute />} />
        </Routes>
        <ToastHost />
      </HashRouter>
    </AppProvider>
  );
}

function Bootstrap() {
  const { dispatch } = useApp();
  useEffect(() => {
    (async () => {
      try {
        const me = await api.getMe().catch(() => api.bootstrap());
        dispatch({ type: "SET_USER", user: me });
        const projects = await api.listProjects();
        dispatch({ type: "SET_PROJECTS", projects });
      } catch {
        /* ignore — welcome page will retry on action */
      }
    })();
  }, [dispatch]);
  return null;
}

function ProjectRoute() {
  const { projectId = "" } = useParams();
  const navigate = useNavigate();
  const { state, dispatch } = useApp();
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    dispatch({ type: "CLEAR_PROJECT" });
    (async () => {
      if (!projectId) return;
      try {
        const proj = await api.getProject(projectId);
        const mode = modeFromProjectDefault(proj.default_mode);
        const activity = activityFromMode(mode);
        let [convs, summary, sources, misconceptions] = await Promise.all([
          api.listConversations(projectId, activity),
          api.learningSummary(projectId).catch(() => null),
          api.listSources(projectId).catch(() => [] as LearningSourceView[]),
          api.listMisconceptions(projectId).catch(() => [] as MisconceptionView[]),
        ]);
        if (!convs.length) convs = [await api.createConversation(projectId, activity)];
        const conversation = await api.getConversation(convs[0].conversation_id);
        if (cancelled) return;
        dispatch({
          type: "OPEN_PROJECT",
          project: proj,
          mode,
          conversationActivity: activity,
          conversations: convs,
          summary: summary as LearningSummary | null,
          sources: sources as LearningSourceView[],
          misconceptions: misconceptions as MisconceptionView[],
        });
        const messages = conversation.messages.map((message) => ({
          role: message.role,
          content_blocks: message.content_blocks,
          message_id: message.message_id,
          run_id: message.run_id,
          created_at: message.created_at,
        }));
        dispatch({ type: "SET_ACTIVE_CONVERSATION", conversation: convs[0], messages, activity, projectId });
        dispatch({ type: "SET_PENDING_TASK", pendingTask: detectPendingTask(messages), activity, projectId });
        setLoaded(true);

        // Resume or surface a real ingestion job after navigation/reload. The
        // welcome-page sample import uses the same background pipeline as a
        // manual upload, so its progress must survive leaving the welcome page.
        const trackedSource = (sources as LearningSourceView[]).find(
          (source) => source.job_id && source.state !== "SUCCEEDED",
        );
        if (trackedSource?.job_id) {
          void (async () => {
            const initialJob = await api.getJob(trackedSource.job_id!);
            if (cancelled) return;
            dispatch({ type: "SET_JOB", job: initialJob, projectId });
            if (["FAILED", "RETRYABLE_FAILED", "CANCELLED"].includes(initialJob.state)) return;
            const finalJob = await api.pollJob(trackedSource.job_id!, {
              onProgress: (job) => {
                if (!cancelled) dispatch({ type: "SET_JOB", job, projectId });
              },
            });
            if (cancelled) return;
            dispatch({ type: "SET_JOB", job: finalJob, projectId });
            const [freshSources, freshSummary, freshMisconceptions] = await Promise.all([
              api.listSources(projectId),
              api.learningSummary(projectId).catch(() => null),
              api.listMisconceptions(projectId).catch(() => [] as MisconceptionView[]),
            ]);
            if (cancelled) return;
            dispatch({ type: "SET_SOURCES", sources: freshSources, projectId });
            dispatch({ type: "SET_SUMMARY", summary: freshSummary, projectId });
            dispatch({ type: "SET_MISCONCEPTIONS", misconceptions: freshMisconceptions, projectId });
            if (finalJob.state === "SUCCEEDED") dispatch({ type: "SET_JOB", job: null, projectId });
          })();
        }
      } catch (e) {
        if (cancelled) return;
        toast((e as Error).message || "打开项目失败");
        navigate("/");
      }
    })();
    return () => { cancelled = true; };
  }, [projectId, dispatch, navigate]);

  if (!loaded || !state.activeProject) {
    return <div className="c-muted" style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%" }}>正在打开这个学习空间…</div>;
  }
  return <AppShell />;
}
