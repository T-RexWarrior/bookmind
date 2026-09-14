import { useState } from "react";
import { useApp } from "../../store/appStore";
import { Button, Dialog, toast } from "../../components/ui/primitives";
import * as api from "../../api/client";

export function WelcomePage({ onOpenProject }: { onOpenProject: (pid: string) => void }) {
  const { state, dispatch } = useApp();
  const [creating, setCreating] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState({ name: "", goal: "", learning_scope: "", deadline: "", current_plan: "" });
  const recent = state.projects[0];

  async function ensureUser() {
    if (state.user) return state.user;
    const user = await api.getMe().catch(() => api.bootstrap());
    dispatch({ type: "SET_USER", user });
    return user;
  }

  async function startSample() {
    setCreating(true);
    try {
      await ensureUser();
      const project = await api.createProject({
        name: "数据结构学习空间",
        goal: "理解数据结构的核心思想，并能结合 C++ 实现",
        learning_scope: "示例资料全部内容",
        current_plan: "从目录浏览资料，选择一个章节开始",
      });
      await api.importSampleSource(project.project_id);
      dispatch({ type: "SET_PROJECTS", projects: await api.listProjects() });
      onOpenProject(project.project_id);
    } catch (error) {
      toast((error as Error).message || "示例资料载入失败");
    } finally {
      setCreating(false);
    }
  }

  async function createSpace() {
    if (!form.name.trim()) return;
    setCreating(true);
    try {
      await ensureUser();
      const project = await api.createProject(form);
      dispatch({ type: "SET_PROJECTS", projects: await api.listProjects() });
      setShowCreate(false);
      onOpenProject(project.project_id);
    } catch (error) {
      toast((error as Error).message || "创建学习空间失败");
    } finally {
      setCreating(false);
    }
  }

  return (
    <main className="home-page">
      <header className="home-nav">
        <a className="brand" href="#/"><span>迹</span><div><strong>学迹</strong><small>Learning Workspace</small></div></a>
        <button className="btn ghost" onClick={() => setShowCreate(true)}>＋ 新建学习空间</button>
      </header>

      <section className="home-hero">
        <div className="home-hero__copy">
          <span className="hero-kicker">从资料出发，而不是从空聊天框开始</span>
          <h1>把零散资料，变成<br /><em>真正学会的路径。</em></h1>
          <p>看原文时可以随时追问，也可以进入练习巩固做题和复验。每个回答和问题，都能回到它的资料依据。</p>
          <div className="hero-actions">
            <Button variant="primary" onClick={() => setShowCreate(true)}>创建学习空间</Button>
            <Button onClick={startSample} disabled={creating}>{creating ? "正在载入真实 PDF…" : "体验真实示例资料"}</Button>
          </div>
          <span className="hero-note">支持文字型、扫描型和复杂排版 PDF，会依次尝试 MinerU、文本解析与本地中文 OCR。</span>
        </div>
        <div className="hero-visual" aria-hidden>
          <div className="visual-source"><span>PDF</span><strong>你的学习资料</strong><small>原文、目录、知识范围</small></div>
          <div className="visual-line" />
          <div className="visual-cards">
            <div><span>01</span><strong>资料学习</strong><small>边看原文，边提问核对</small></div>
            <div><span>02</span><strong>练习巩固</strong><small>做题、纠错与到期复验</small></div>
          </div>
        </div>
      </section>

      <section className="home-content">
        <div className="section-heading"><div><span className="eyebrow">你的空间</span><h2>{recent ? "继续上次学习" : "从一份资料开始"}</h2></div></div>
        {recent ? (
          <>
            <button className="continue-card" onClick={() => onOpenProject(recent.project_id)}>
              <div className="continue-card__mark">{recent.source_count ? "▤" : "＋"}</div>
              <div className="continue-card__body">
                <span className="continue-card__meta">最近学习 · {formatTime(recent.last_activity_at || recent.updated_at)}</span>
                <h3>{recent.name}</h3>
                <p>{recent.current_plan || recent.goal || "继续添加资料并开始阅读"}</p>
                <div className="continue-card__chips">
                  {recent.learning_scope && <span>{recent.learning_scope}</span>}
                  <span>{recent.source_count || 0} 份资料</span>
                  {recent.last_source_page ? <span>上次第 {recent.last_source_page} 页</span> : null}
                </div>
              </div>
              <div className="continue-card__action">继续学习 <span>→</span></div>
            </button>
            {state.projects.length > 1 && (
              <div className="space-grid">
                {state.projects.slice(1).map((project) => (
                  <button key={project.project_id} className="space-card" onClick={() => onOpenProject(project.project_id)}>
                    <span>{project.source_count || 0} 份资料</span><h3>{project.name}</h3><p>{project.goal || "尚未填写学习目标"}</p><small>打开空间 →</small>
                  </button>
                ))}
              </div>
            )}
          </>
        ) : (
          <button className="first-source-card" onClick={() => setShowCreate(true)}><span>＋</span><div><strong>创建学习空间</strong><p>先说明想学什么，再加入相关资料。</p></div></button>
        )}
      </section>

      <Dialog open={showCreate} onClose={() => setShowCreate(false)} title="创建学习空间" size="md" footer={<><Button onClick={() => setShowCreate(false)}>先不建</Button><Button variant="primary" onClick={createSpace} disabled={creating || !form.name.trim()}>{creating ? "正在创建…" : "建好后添加资料"}</Button></>}>
        <div className="form-stack">
          <label><span>空间名称</span><input autoFocus value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：数据结构、摄影基础、产品设计" /></label>
          <label><span>学习目标</span><textarea rows={2} value={form.goal} onChange={(event) => setForm({ ...form, goal: event.target.value })} placeholder="希望通过这些资料理解或完成什么？" /></label>
          <label><span>学习范围</span><input value={form.learning_scope} onChange={(event) => setForm({ ...form, learning_scope: event.target.value })} placeholder="例如：第 1–5 章，或整套资料" /></label>
          <div className="form-grid-2">
            <label><span>可选截止日期</span><input type="date" value={form.deadline} onChange={(event) => setForm({ ...form, deadline: event.target.value })} /></label>
            <label><span>本次学习计划</span><input value={form.current_plan} onChange={(event) => setForm({ ...form, current_plan: event.target.value })} placeholder="先浏览目录，再学习第一节" /></label>
          </div>
          <p className="form-help">还没想好也没关系，除了空间名称，其他内容都可以之后再补。</p>
        </div>
      </Dialog>
    </main>
  );
}

function formatTime(value?: string): string {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "最近";
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
