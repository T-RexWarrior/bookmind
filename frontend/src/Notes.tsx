import { useEffect, useState } from "react";
import {
  ArrowUpRight,
  BookOpen,
  Check,
  Download,
  FileText,
  Network,
  Pencil,
  Plus,
  Save,
  Search,
  Trash2,
} from "lucide-react";
import { useApp, useWorkspace } from "./App";
import { api, post, put, prettyDate } from "./api";
import { Diagram, Empty, Markdown, Spinner } from "./components";
import type { Artifact, Note } from "./types";

export default function Notes() {
  const { space, openSource, refresh } = useWorkspace();
  const { toast } = useApp();
  const [notes, setNotes] = useState<Note[]>([]),
    [selected, setSelected] = useState(""),
    [search, setSearch] = useState(""),
    [draft, setDraft] = useState<Note>(),
    [edit, setEdit] = useState(false),
    [busy, setBusy] = useState(false),
    [artifact, setArtifact] = useState<Artifact>();
  const load = async () => {
    const result = await api<Note[]>(`/spaces/${space.id}/notes`);
    setNotes(result);
    return result;
  };
  useEffect(() => {
    let dead = false;
    api<Note[]>(`/spaces/${space.id}/notes`)
      .then((result) => {
        if (dead) return;
        setNotes(result);
        if (result[0]) {
          setSelected(result[0].id);
          setDraft(result[0]);
        }
      })
      .catch((e) => toast(e.message, true));
    return () => {
      dead = true;
    };
  }, [space.id, toast]);
  useEffect(() => {
    let dead = false;
    setArtifact(undefined);
    if (draft?.source.artifact_id)
      api<Artifact>(`/spaces/${space.id}/artifacts/${draft.source.artifact_id}`)
        .then((a) => {
          if (!dead) setArtifact(a);
        })
        .catch((e) => toast(e.message, true));
    return () => {
      dead = true;
    };
  }, [draft?.source.artifact_id, space.id, toast]);
  const isDirty =
    !!draft &&
    (draft.id === "new" ||
      draft.title !== notes.find((n) => n.id === draft.id)?.title ||
      draft.content !== notes.find((n) => n.id === draft.id)?.content);
  useEffect(() => {
    if (!isDirty) return;
    const listener = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", listener);
    return () => window.removeEventListener("beforeunload", listener);
  }, [isDirty]);
  const select = (note: Note) => {
    if (isDirty && !window.confirm("当前笔记尚未保存，放弃修改并切换吗？"))
      return;
    setSelected(note.id);
    setDraft(note);
    setEdit(false);
  };
  const create = () => {
    if (isDirty && !window.confirm("当前笔记尚未保存，放弃修改并新建吗？"))
      return;
    setSelected("new");
    setDraft({
      id: "new",
      title: "",
      content: "",
      source: {},
      updated: new Date().toISOString(),
    });
    setEdit(true);
  };
  const save = async () => {
    if (!draft?.title.trim()) return;
    setBusy(true);
    try {
      const value =
        draft.id === "new"
          ? await post<Note>(`/spaces/${space.id}/notes`, draft)
          : await put<Note>(`/spaces/${space.id}/notes/${draft.id}`, draft);
      await load();
      setDraft(value);
      setSelected(value.id);
      setEdit(false);
      await refresh();
      toast("笔记已保存。");
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!draft || !window.confirm(`删除笔记“${draft.title || "未命名"}”？`))
      return;
    try {
      if (draft.id !== "new")
        await api(`/spaces/${space.id}/notes/${draft.id}`, {
          method: "DELETE",
        });
      const result = await load();
      setDraft(result[0]);
      setSelected(result[0]?.id || "");
      setEdit(false);
      await refresh();
      toast("笔记已删除。");
    } catch (e) {
      toast((e as Error).message, true);
    }
  };
  const download = () => {
    if (!draft) return;
    const provenance = draft.source.material_title
      ? `\n\n---\n来源：${draft.source.material_title} · 第 ${draft.source.page} 页/节\n`
      : "";
    const diagram = artifact
      ? `\n\n## 图解\n\n${artifact.format === "steps" ? artifact.payload.steps?.map((s, i) => `### ${i + 1}. ${s.title}\n${s.description}\n\n${s.state}`).join("\n\n") : "```" + artifact.format + "\n" + artifact.payload.code + "\n```"}`
      : "";
    const url = URL.createObjectURL(
      new Blob(
        ["# " + draft.title + "\n\n" + draft.content + diagram + provenance],
        { type: "text/markdown;charset=utf-8" },
      ),
    );
    const link = document.createElement("a");
    link.href = url;
    link.download = draft.title.replace(/[\\/:*?"<>|]/g, "_") + ".md";
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const filtered = notes.filter((n) =>
    (n.title + " " + n.content).toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className="notes-page">
      <div className="notes-list">
        <div className="notes-heading">
          <div>
            <h1>
              我的笔记 <span>{notes.length}</span>
            </h1>
            <p>把讲解，变成自己的理解。</p>
          </div>
          <button
            className="icon-button primary-icon"
            aria-label="新建笔记"
            onClick={create}
          >
            <Plus size={20} />
          </button>
        </div>
        <div className="note-search">
          <Search size={15} />
          <input
            aria-label="搜索笔记"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="搜索标题或内容"
          />
        </div>
        <div className="note-items">
          {filtered.length ? (
            filtered.map((n) => (
              <button
                key={n.id}
                className={"note-item " + (selected === n.id ? "active" : "")}
                onClick={() => select(n)}
              >
                <div className="note-item-heading">
                  <FileText size={16} />
                  <strong>{n.title}</strong>
                </div>
                <p>{n.content.replace(/[#*>`$]/g, "").slice(0, 95)}</p>
                <div>
                  <span>{prettyDate(n.updated)}</span>
                  {n.source.material_title && (
                    <span>
                      <BookOpen size={11} />
                      {n.source.page} 页/节
                    </span>
                  )}
                  {n.source.artifact_id && <Network size={12} />}
                </div>
              </button>
            ))
          ) : (
            <div className="notes-list-empty">
              {search
                ? "没有找到相关笔记。"
                : "可以新建笔记，也可以从讲解、图解和练习中保存。"}
            </div>
          )}
        </div>
        <div className="notes-list-footer">
          <BookOpen size={14} />
          所有笔记属于「{space.title}」
        </div>
      </div>
      <main className="note-main">
        {!draft ? (
          <Empty
            title="让理解，留下痕迹。"
            text="读到重要的地方、听到清楚的讲解，就把它留在这里。"
          >
            <button className="btn primary" onClick={create}>
              <Plus size={16} />
              写第一篇笔记
            </button>
          </Empty>
        ) : (
          <>
            <div className="note-toolbar">
              <span className="note-save-state">
                {isDirty ? (
                  <>
                    <span className="status-dot" />
                    尚未保存
                  </>
                ) : (
                  <>
                    <Check size={14} />
                    已保存到本地
                  </>
                )}
              </span>
              <div>
                <button
                  className="btn small subtle"
                  onClick={() => setEdit((v) => !v)}
                >
                  <Pencil size={14} />
                  {edit ? "预览" : "编辑"}
                </button>
                <button className="btn small subtle" onClick={download}>
                  <Download size={14} />
                  导出
                </button>
                <button
                  className="icon-button"
                  aria-label="删除笔记"
                  onClick={() => void remove()}
                >
                  <Trash2 size={15} />
                </button>
                <button
                  className="btn small primary"
                  disabled={busy || !draft.title.trim() || !isDirty}
                  onClick={() => void save()}
                >
                  {busy ? (
                    <Spinner label="保存中" />
                  ) : (
                    <>
                      <Save size={14} />
                      保存
                    </>
                  )}
                </button>
              </div>
            </div>
            <div className="note-content">
              {edit ? (
                <>
                  <input
                    aria-label="笔记标题"
                    className="note-title-input"
                    placeholder="给笔记起个名字"
                    value={draft.title}
                    maxLength={150}
                    onChange={(e) =>
                      setDraft({ ...draft, title: e.target.value })
                    }
                  />
                  <div className="note-editor-help">
                    支持 Markdown · # 标题 · **重点** · $数学公式$
                  </div>
                  <textarea
                    aria-label="笔记内容"
                    className="note-editor"
                    value={draft.content}
                    placeholder="写下你自己的理解，或者整理一个还没想明白的问题…"
                    onChange={(e) =>
                      setDraft({ ...draft, content: e.target.value })
                    }
                  />
                </>
              ) : (
                <>
                  <div className="eyebrow">MY UNDERSTANDING</div>
                  <h1>{draft.title}</h1>
                  <span className="note-date">
                    更新于 {new Date(draft.updated).toLocaleString("zh-CN")}
                  </span>
                  <Markdown>
                    {draft.content || "这篇笔记还没有正文，点击编辑开始记录。"}
                  </Markdown>
                </>
              )}
              {artifact && (
                <section className="note-diagram">
                  <h3>
                    <Network size={17} />
                    关联图解 · {artifact.title}
                  </h3>
                  <Diagram artifact={artifact} />
                </section>
              )}
              {draft.source.material_id && (
                <button
                  className="note-source-card"
                  onClick={() => openSource(draft.source)}
                >
                  <span className="card-icon green">
                    <BookOpen size={20} />
                  </span>
                  <div>
                    <small>回到学习现场</small>
                    <strong>
                      {draft.source.material_title || "来源资料"} · 第{" "}
                      {draft.source.page} 页/节
                    </strong>
                  </div>
                  <ArrowUpRight size={19} />
                </button>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
