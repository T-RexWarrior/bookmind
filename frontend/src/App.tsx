import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  BrowserRouter,
  Link,
  NavLink,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useOutletContext,
  useParams,
} from "react-router-dom";
import {
  ArrowRight,
  BookMarked,
  BookOpen,
  Check,
  ChevronDown,
  ChevronLeft,
  CircleHelp,
  FileText,
  GraduationCap,
  LayoutGrid,
  Library,
  LoaderCircle,
  Settings as SettingsIcon,
  Sparkles,
  Sprout,
  Target,
  Upload,
  X,
} from "lucide-react";
import { api, post, put, prettyDate } from "./api";
import { Empty, Spinner } from "./components";
import type { Job, Material, Ref, Settings, Space } from "./types";
import Learn from "./Learn";
import Practice from "./Practice";
import Notes from "./Notes";
import SettingsPage from "./Settings";

type AppContextType = {
  toast: (text: string, error?: boolean) => void;
  settings?: Settings;
  refreshSettings: () => Promise<void>;
};
const AppContext = createContext<AppContextType>(null!);
export const useApp = () => useContext(AppContext);
export type Workspace = {
  space: Space;
  material?: Material;
  page: number;
  focus: (mid: string, page: number) => Promise<void>;
  refresh: () => Promise<void>;
  job?: Job;
  jobVersion: number;
  startJob: (kind: string, body: object) => Promise<void>;
  openSource: (ref: Partial<Ref>) => void;
  saveNote: (title: string, content: string, source: object) => Promise<void>;
};
export const useWorkspace = () => useOutletContext<Workspace>();

function Logo() {
  return (
    <span className="brand">
      <span className="brand-symbol">
        <Sprout size={23} />
      </span>
      <span>
        BookMind <small>学迹 · AI 学习助手</small>
      </span>
    </span>
  );
}

function Shell() {
  const [settings, setSettings] = useState<Settings>();
  const [toast, setToast] = useState<{ text: string; error: boolean }>();
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const showToast = useCallback((text: string, error = false) => {
    setToast({ text, error });
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setToast(undefined), 6500);
  }, []);
  const refreshSettings = useCallback(async () => {
    try {
      setSettings(await api("/settings"));
    } catch (e) {
      showToast(String(e), true);
    }
  }, [showToast]);
  useEffect(() => {
    void refreshSettings();
    return () => clearTimeout(timer.current);
  }, [refreshSettings]);
  return (
    <AppContext.Provider
      value={{ toast: showToast, settings, refreshSettings }}
    >
      <div className="app-shell">
        <aside className="global-sidebar">
          <Link to="/" className="logo-link">
            <Logo />
          </Link>
          <div className="nav-label">我的学习</div>
          <NavLink end to="/" className="global-nav">
            <Library size={18} />
            学习空间
          </NavLink>
          <div className="sidebar-description">
            <span className="tiny-square" />
            一点点读懂
            <br />
            一步步学会
          </div>
          <div className="sidebar-bottom">
            <NavLink to="/settings" className="global-nav">
              <SettingsIcon size={18} />
              API 设置
              <span
                className={
                  "status-dot " + (settings?.has_api_key ? "connected" : "")
                }
              />
            </NavLink>
            <div className="local-label">
              <span />
              本地学习工作台 <small>v0.1</small>
            </div>
          </div>
        </aside>
        <div className="app-main">
          <Outlet />
        </div>
        {toast && (
          <div
            role="status"
            className={"toast " + (toast.error ? "danger" : "")}
          >
            {toast.error ? <CircleHelp size={18} /> : <Check size={18} />}
            <span>{toast.text}</span>
            <button aria-label="关闭提示" onClick={() => setToast(undefined)}>
              <X size={16} />
            </button>
          </div>
        )}
      </div>
    </AppContext.Provider>
  );
}

function Home() {
  const { toast } = useApp();
  const navigate = useNavigate();
  const [spaces, setSpaces] = useState<Space[]>([]),
    [busy, setBusy] = useState("");
  const [mode, setMode] = useState<"upload" | "goal" | "">("");
  const [title, setTitle] = useState(""),
    [goal, setGoal] = useState("");
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => {
    api<Space[]>("/spaces")
      .then(setSpaces)
      .catch((e) => toast(e.message, true));
  }, [toast]);
  const createWithFile = async (f: File, demo = false) => {
    setBusy("upload");
    try {
      const space = await post<Space>("/spaces", {
        title:
          title.trim() ||
          (demo ? "数据结构 · 二叉树" : f.name.replace(/\.[^.]+$/, "")),
      });
      const data = new FormData();
      data.append("file", f);
      const mat = await api<Material>(`/spaces/${space.id}/materials`, {
        method: "POST",
        body: data,
      });
      if (mat.warning) toast(mat.warning);
      navigate(`/space/${space.id}/learn`);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy("");
    }
  };
  const demo = async () => {
    setBusy("demo");
    try {
      const text = await fetch("/demo.md").then((r) => r.text());
      await createWithFile(
        new File([text], "二叉树学习资料.md", { type: "text/markdown" }),
        true,
      );
    } catch (e) {
      toast((e as Error).message, true);
      setBusy("");
    }
  };
  const createGoal = async () => {
    if (!goal.trim()) return;
    setBusy("goal");
    try {
      const space = await post<Space>("/spaces", {
        title: title.trim() || goal.slice(0, 28),
        goal,
      });
      await post(`/spaces/${space.id}/handbook`, { goal });
      navigate(`/space/${space.id}/learn`);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="home-page">
      <header className="home-header">
        <span className="breadcrumb">
          <LayoutGrid size={15} />
          学习空间
        </span>
        <span className="header-label">为理解而学，让知识留下痕迹</span>
      </header>
      <main className="home-content">
        <div className="hero-intro">
          <div className="eyebrow">
            <span /> YOUR LEARNING, CONNECTED
          </div>
          <h1>
            从一个问题，
            <br />
            到真正<span>理解。</span>
          </h1>
          <p>
            带上一份资料，或说出一个学习目标。
            <br />让 AI 陪你读懂、练会，再把理解整理成自己的笔记。
          </p>
        </div>
        <div className="start-cards">
          <button className="start-card" onClick={() => setMode("upload")}>
            <span className="card-icon warm">
              <BookOpen size={26} />
            </span>
            <span className="card-tag">我有学习资料</span>
            <h2>从资料开始</h2>
            <p>
              上传教材、课件或讲义，
              <br />
              边读边问，讲解始终关联原文。
            </p>
            <span className="card-action">
              上传一份资料 <ArrowRight size={18} />
            </span>
            <span className="card-corner">01</span>
          </button>
          <button className="start-card goal" onClick={() => setMode("goal")}>
            <span className="card-icon green">
              <Sparkles size={26} />
            </span>
            <span className="card-tag">我有学习目标</span>
            <h2>从目标开始</h2>
            <p>
              告诉 AI 你想学什么，
              <br />
              先生成学习手册，再逐节深入。
            </p>
            <span className="card-action">
              创建学习手册 <ArrowRight size={18} />
            </span>
            <span className="card-corner">02</span>
          </button>
        </div>
        <div className="demo-link">
          <span>想先体验一下？</span>
          <button disabled={!!busy} onClick={() => void demo()}>
            {busy === "demo" || busy === "upload" ? (
              <LoaderCircle size={14} className="spin" />
            ) : (
              <GraduationCap size={16} />
            )}
            打开「二叉树」示例资料 <ArrowRight size={14} />
          </button>
        </div>
        <section className="recent-section">
          <div className="section-title">
            <h2>
              继续学习 <span>{spaces.length.toString().padStart(2, "0")}</span>
            </h2>
            <span>资料、练习与笔记，都在各自的学习空间里</span>
          </div>
          {spaces.length ? (
            <div className="space-grid">
              {spaces.map((s, i) => (
                <Link
                  key={s.id}
                  to={`/space/${s.id}/learn`}
                  className="space-card"
                >
                  <div className={"space-icon tone-" + (i % 3)}>
                    <BookMarked size={23} />
                  </div>
                  <div className="space-card-text">
                    <h3>{s.title}</h3>
                    <p>
                      {s.material_count} 份资料 <span>·</span> {s.note_count}{" "}
                      篇笔记
                    </p>
                    <small>最近学习 · {prettyDate(s.updated)}</small>
                  </div>
                  <ArrowRight size={18} />
                </Link>
              ))}
            </div>
          ) : (
            <div className="home-empty">
              <FileText size={20} />
              <span>还没有学习空间。从上方选择一种方式开始吧。</span>
            </div>
          )}
        </section>
        <div className="learning-path">
          <div>
            <BookOpen size={19} />
            <span>读懂资料</span>
          </div>
          <i />
          <div>
            <Target size={19} />
            <span>练习验证</span>
          </div>
          <i />
          <div>
            <FileText size={19} />
            <span>整理理解</span>
          </div>
          <span className="path-caption">在同一个学习空间，连接每一步。</span>
        </div>
      </main>
      {mode && (
        <div className="modal-overlay" onClick={() => !busy && setMode("")}>
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-heading"
            onClick={(e) => e.stopPropagation()}
          >
            <button
              className="modal-close icon-button"
              aria-label="关闭"
              onClick={() => setMode("")}
              disabled={!!busy}
            >
              <X size={19} />
            </button>
            <span className="card-icon green">
              {mode === "goal" ? <Sparkles size={25} /> : <Upload size={25} />}
            </span>
            <h2 id="create-heading">
              {mode === "goal" ? "你想学会什么？" : "带上你的学习资料"}
            </h2>
            <p className="muted">
              {mode === "goal"
                ? "写得越具体，学习手册越贴合你的目标。"
                : "支持可复制文字的 PDF、Markdown 和 TXT，单份不超过 80MB。"}
            </p>
            <label className="field">
              学习空间名称（选填）
              <input
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="例如：数据结构 · 树与图"
                maxLength={100}
              />
            </label>
            {mode === "goal" ? (
              <>
                <label className="field">
                  学习目标
                  <textarea
                    autoFocus
                    rows={5}
                    value={goal}
                    onChange={(e) => setGoal(e.target.value)}
                    placeholder="我是大一学生，想理解二叉树的前序、中序和后序遍历，能手动推导，也能写 Python 代码。"
                    maxLength={3000}
                  />
                </label>
                <div className="notice small-notice">
                  AI 将编写一份短学习手册。生成内容需结合课程资料核对。
                </div>
                <button
                  className="btn primary full"
                  disabled={!!busy || !goal.trim()}
                  onClick={() => void createGoal()}
                >
                  {busy ? (
                    <Spinner label="正在创建" />
                  ) : (
                    <>
                      <Sparkles size={17} />
                      生成学习手册
                    </>
                  )}
                </button>
              </>
            ) : (
              <>
                <input
                  ref={file}
                  type="file"
                  accept=".pdf,.md,.markdown,.txt"
                  hidden
                  onChange={(e) => {
                    if (e.target.files?.[0])
                      void createWithFile(e.target.files[0]);
                  }}
                />
                <button
                  className="upload-zone"
                  disabled={!!busy}
                  onClick={() => file.current?.click()}
                >
                  {busy ? (
                    <Spinner label="正在上传与分节" />
                  ) : (
                    <>
                      <Upload size={30} />
                      <strong>选择一份资料</strong>
                      <span>PDF / Markdown / TXT</span>
                    </>
                  )}
                </button>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function WorkspaceLayout() {
  const { id = "" } = useParams();
  const { toast } = useApp();
  const navigate = useNavigate();
  const location = useLocation();
  const [space, setSpace] = useState<Space>(),
    [error, setError] = useState(""),
    [job, setJob] = useState<Job>(),
    [jobVersion, setJobVersion] = useState(0);
  const handled = useRef(new Set<string>());
  const alive = useRef(true);
  const focusQueue = useRef<Promise<unknown>>(Promise.resolve());
  const refresh = useCallback(async () => {
    await focusQueue.current.catch(() => undefined);
    const value = await api<Space>(`/spaces/${id}`);
    if (alive.current) setSpace(value);
  }, [id]);
  const focus = useCallback(
    async (mid: string, page: number) => {
      setSpace((s) =>
        s ? { ...s, focus_material_id: mid, focus_page: page } : s,
      );
      focusQueue.current = focusQueue.current
        .catch(() => undefined)
        .then(() => put(`/spaces/${id}/focus`, { material_id: mid, page }));
      await focusQueue.current;
    },
    [id],
  );
  useEffect(() => {
    alive.current = true;
    setSpace(undefined);
    setError("");
    setJob(undefined);
    handled.current.clear();
    void refresh().catch((e) => setError(e.message));
    api<Job[]>(`/spaces/${id}/jobs`)
      .then((js) =>
        setJob(js.find((j) => j.status === "queued" || j.status === "running")),
      )
      .catch((e) => toast(e.message, true));
    return () => {
      alive.current = false;
    };
  }, [id, refresh, toast]);
  useEffect(() => {
    const params = new URLSearchParams(location.search),
      mid = params.get("material"),
      page = Number(params.get("page") || 1);
    if (
      space &&
      mid &&
      (space.focus_material_id !== mid || space.focus_page !== page) &&
      space.materials.some(
        (m) => m.id === mid && page >= 1 && page <= m.page_count,
      )
    )
      void focus(mid, page).catch((e) => toast(e.message, true));
  }, [location.search, space, focus, toast]);
  useEffect(() => {
    if (!job || !["queued", "running"].includes(job.status)) return;
    let dead = false;
    let timeout: ReturnType<typeof setTimeout>;
    const tick = async () => {
      try {
        const next = await api<Job>(`/spaces/${id}/jobs/${job.id}`);
        if (dead) return;
        setJob(next);
        if (next.status === "completed" || next.status === "failed") {
          if (!handled.current.has(next.id)) {
            handled.current.add(next.id);
            setJobVersion((v) => v + 1);
            await refresh();
            if (next.status === "failed")
              toast(next.error || "生成失败，请重试。", true);
            else
              toast(
                next.kind === "handbook"
                  ? "学习手册已生成，可以开始阅读。"
                  : next.kind === "quiz"
                    ? "练习已生成，请前往练习页。"
                    : next.kind === "visualize"
                      ? "图解已生成，可展开查看。"
                      : "讲解已完成。",
              );
          }
        } else timeout = setTimeout(() => void tick(), 800);
      } catch (e) {
        if (!dead) {
          toast((e as Error).message, true);
          timeout = setTimeout(() => void tick(), 3000);
        }
      }
    };
    timeout = setTimeout(() => void tick(), 500);
    return () => {
      dead = true;
      clearTimeout(timeout);
    };
  }, [job?.id, job?.status, id, refresh, toast]);
  const startJob = async (kind: string, body: object) => {
    const result = await post<{ job_id: string }>(
      `/spaces/${id}/${kind}`,
      body,
    );
    setJob(await api(`/spaces/${id}/jobs/${result.job_id}`));
  };
  const openSource = (ref: Partial<Ref>) => {
    if (ref.material_id)
      navigate(
        `/space/${id}/learn?material=${ref.material_id}&page=${ref.page || 1}`,
      );
  };
  const saveNote = async (title: string, content: string, source: object) => {
    await post(`/spaces/${id}/notes`, {
      title: title.slice(0, 150) || "学习笔记",
      content,
      source,
    });
    toast("已保存到笔记页。");
    await refresh();
  };
  if (error)
    return (
      <Empty title="学习空间打开失败" text={error}>
        <Link to="/" className="btn">
          返回学习空间
        </Link>
      </Empty>
    );
  if (!space)
    return (
      <div className="page-loading">
        <Spinner label="正在打开学习空间" />
      </div>
    );
  const material =
    space.materials.find((m) => m.id === space.focus_material_id) ||
    space.materials[0];
  const active = job && ["queued", "running"].includes(job.status);
  return (
    <div className="workspace-layout">
      <header className="workspace-header">
        <Link to="/" className="back-link">
          <ChevronLeft size={16} />
          <span>学习空间</span>
        </Link>
        <div className="header-separator" />
        <div className="workspace-name">
          <BookOpen size={17} />
          <strong>{space.title}</strong>
          <ChevronDown size={14} />
        </div>
        <span className="space-count">{space.materials.length} 份资料</span>
        <nav className="workspace-tabs">
          <NavLink to={`/space/${id}/learn`}>
            <BookOpen size={16} />
            学习
          </NavLink>
          <NavLink to={`/space/${id}/practice`}>
            <Target size={16} />
            练习
          </NavLink>
          <NavLink to={`/space/${id}/notes`}>
            <FileText size={16} />
            笔记{" "}
            {space.progress.note_count > 0 && (
              <small>{space.progress.note_count}</small>
            )}
          </NavLink>
        </nav>
      </header>
      {active && (
        <div className="job-banner">
          <Spinner label={job.stage} />
          <span>可以切换页面，任务会继续进行</span>
        </div>
      )}
      <Outlet
        context={
          {
            space,
            material,
            page: space.focus_page,
            focus,
            refresh,
            job,
            jobVersion,
            startJob,
            openSource,
            saveNote,
          } satisfies Workspace
        }
      />
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Shell />}>
          <Route index element={<Home />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="space/:id" element={<WorkspaceLayout />}>
            <Route index element={<Navigate to="learn" replace />} />
            <Route path="learn" element={<Learn />} />
            <Route path="practice" element={<Practice />} />
            <Route path="notes" element={<Notes />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
