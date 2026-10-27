import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  BookOpen,
  BookmarkPlus,
  ChevronDown,
  ChevronRight,
  FileText,
  Focus,
  Maximize2,
  Network,
  PanelLeftClose,
  Plus,
  Send,
  Sparkles,
  Target,
  Upload,
  X,
} from "lucide-react";
import { useApp, useWorkspace } from "./App";
import { api } from "./api";
import { Diagram, Empty, Markdown, SourceLinks, Spinner } from "./components";
import type { Artifact, Material, Message } from "./types";
import PdfReader from "./PdfReader";

export default function Learn() {
  const {
    space,
    material,
    page,
    focus,
    refresh,
    job,
    jobVersion,
    startJob,
    openSource,
    saveNote,
  } = useWorkspace();
  const { toast } = useApp();
  const navigate = useNavigate();
  const [content, setContent] = useState<{ text: string; title: string }>(),
    [pageError, setPageError] = useState("");
  const [messages, setMessages] = useState<Message[]>([]),
    [artifacts, setArtifacts] = useState<Record<string, Artifact>>({});
  const [diagram, setDiagram] = useState<string>(),
    [textView, setTextView] = useState(false),
    [input, setInput] = useState(""),
    [selection, setSelection] = useState("");
  const [scope, setScope] = useState("page"),
    [uploading, setUploading] = useState(false),
    [sending, setSending] = useState(false),
    [sidebar, setSidebar] = useState(true);
  const upload = useRef<HTMLInputElement>(null),
    scroll = useRef<HTMLDivElement>(null),
    reader = useRef<HTMLDivElement>(null);
  const busy = sending || (!!job && ["running", "queued"].includes(job.status));
  const loadMessages = useCallback(async () => {
    const result = await api<Message[]>(`/spaces/${space.id}/messages`);
    setMessages(result);
    const ids = [
      ...new Set(result.map((m) => m.artifact_id).filter(Boolean)),
    ] as string[];
    const values = await Promise.all(
      ids.map((id) => api<Artifact>(`/spaces/${space.id}/artifacts/${id}`)),
    );
    setArtifacts(Object.fromEntries(values.map((a) => [a.id, a])));
  }, [space.id]);
  useEffect(() => {
    void loadMessages().catch((e) => toast(e.message, true));
  }, [loadMessages, jobVersion, job?.id, toast]);
  useEffect(() => {
    let dead = false;
    setContent(undefined);
    setPageError("");
    setSelection("");
    if (material)
      api<{ text: string; title: string }>(
        `/spaces/${space.id}/materials/${material.id}/pages/${page}`,
      )
        .then((value) => {
          if (!dead) setContent(value);
        })
        .catch((e) => {
          if (!dead) setPageError(e.message);
        });
    reader.current?.scrollTo(0, 0);
    return () => {
      dead = true;
    };
  }, [material?.id, page, space.id]);
  useEffect(() => {
    scroll.current?.scrollTo({
      top: scroll.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, job?.partial]);
  useEffect(() => {
    if (
      job?.status === "completed" &&
      job.kind === "visualize" &&
      job.result?.artifact_id
    )
      setDiagram(job.result.artifact_id);
  }, [jobVersion, job]);
  const changePage = async (p: number, mid = material?.id) => {
    if (!mid) return;
    setDiagram(undefined);
    try {
      navigate({ search: `?material=${mid}&page=${p}` }, { replace: true });
      await focus(mid, p);
    } catch (e) {
      toast((e as Error).message, true);
    }
  };
  const uploadFile = async (f: File) => {
    setUploading(true);
    try {
      const form = new FormData();
      form.append("file", f);
      const mat = await api<Material>(`/spaces/${space.id}/materials`, {
        method: "POST",
        body: form,
      });
      await refresh();
      await changePage(1, mat.id);
      if (mat.warning) toast(mat.warning);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setUploading(false);
      if (upload.current) upload.current.value = "";
    }
  };
  const ask = async (kind: "chat" | "visualize", text = input) => {
    const message =
      text.trim() ||
      (kind === "visualize"
        ? "请用最合适的图解讲清楚本页的核心知识；如果是算法或遍历，请逐步演示。"
        : "请用简单的语言和例子讲解本页的核心知识。");
    setSending(true);
    try {
      await startJob(kind, {
        material_id: material?.id,
        page,
        scope,
        selection,
        message,
      });
      setInput("");
      await loadMessages();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setSending(false);
    }
  };
  const captureSelection = () => {
    const selected = window.getSelection();
    if (
      selected &&
      reader.current?.contains(selected.anchorNode) &&
      selected.toString().trim()
    )
      setSelection(selected.toString().trim().slice(0, 5000));
  };
  const currentArtifact = diagram ? artifacts[diagram] : undefined;
  const unit = material?.kind === "pdf" ? "页" : "节";
  return (
    <div className={"learn-layout " + (!sidebar ? "hide-outline" : "")}>
      <aside className="reading-sidebar">
        <div className="panel-heading">
          <span>
            <BookOpen size={16} />
            学习资料
          </span>
          <button
            className="icon-button"
            aria-label="收起目录"
            onClick={() => setSidebar(false)}
          >
            <PanelLeftClose size={16} />
          </button>
        </div>
        <div className="material-select">
          <select
            aria-label="当前资料"
            value={material?.id || ""}
            onChange={(e) => void changePage(1, e.target.value)}
          >
            {!material && <option value="">尚未添加资料</option>}
            {space.materials.map((m) => (
              <option key={m.id} value={m.id}>
                {m.title}
              </option>
            ))}
          </select>
          <ChevronDown size={14} />
        </div>
        {material && (
          <div className="material-meta">
            <span className="file-badge">
              {material.kind === "pdf"
                ? "PDF"
                : material.kind === "generated"
                  ? "AI 手册"
                  : "文档"}
            </span>
            <span>
              {material.page_count} {unit}
            </span>
          </div>
        )}
        <div className="outline-label">章节目录</div>
        <div className="outline-list">
          {material?.outline.length ? (
            material.outline.map((o, i) => (
              <button
                key={i}
                style={{
                  paddingLeft: `${14 + Math.min(o.level - 1, 3) * 12}px`,
                }}
                className={page === o.page ? "active" : ""}
                onClick={() => void changePage(o.page)}
              >
                <span>{o.title}</span>
                <small>{o.page}</small>
              </button>
            ))
          ) : material ? (
            Array.from(
              { length: Math.min(material.page_count, 300) },
              (_, i) => (
                <button
                  key={i}
                  className={page === i + 1 ? "active" : ""}
                  onClick={() => void changePage(i + 1)}
                >
                  <span>
                    第 {i + 1} {unit}
                  </span>
                </button>
              ),
            )
          ) : (
            <p className="muted outline-empty">上传资料后，这里会显示目录。</p>
          )}
        </div>
        <div className="outline-footer">
          <input
            ref={upload}
            type="file"
            accept=".pdf,.md,.markdown,.txt"
            hidden
            onChange={(e) => {
              if (e.target.files?.[0]) void uploadFile(e.target.files[0]);
            }}
          />
          <button
            className="btn subtle full"
            disabled={uploading}
            onClick={() => upload.current?.click()}
          >
            {uploading ? (
              <Spinner label="正在解析" />
            ) : (
              <>
                <Plus size={16} />
                添加学习资料
              </>
            )}
          </button>
          <span>
            <span className="status-dot connected" />
            阅读位置自动保存
          </span>
        </div>
      </aside>
      <section className="reading-center">
        <div className="reader-toolbar">
          <div className="reader-view-tabs">
            {!sidebar && (
              <button
                className="icon-button"
                aria-label="展开目录"
                onClick={() => setSidebar(true)}
              >
                <BookOpen size={17} />
              </button>
            )}
            <button
              className={!diagram ? "active" : ""}
              onClick={() => setDiagram(undefined)}
            >
              <FileText size={15} />
              原文
            </button>
            {diagram && (
              <span className="diagram-tab active">
                <Network size={15} />
                图解
                <button
                  className="mini-close"
                  aria-label="关闭图解"
                  onClick={(e) => {
                    e.stopPropagation();
                    setDiagram(undefined);
                  }}
                >
                  <X size={13} />
                </button>
              </span>
            )}
          </div>
          <div className="reader-tools">
            <select
              className="mobile-material-select"
              aria-label="切换学习资料"
              value={material?.id || ""}
              onChange={(e) => void changePage(1, e.target.value)}
            >
              {!material && <option value="">暂无资料</option>}
              {space.materials.map((m) => (
                <option value={m.id} key={m.id}>
                  {m.title}
                </option>
              ))}
            </select>
            {!diagram && material?.kind === "pdf" && (
              <button
                onClick={() => setTextView((v) => !v)}
                className="text-button"
              >
                {textView ? "PDF 原版" : "文字视图"}
              </button>
            )}
            {diagram && currentArtifact && (
              <button
                className="text-button"
                onClick={() =>
                  void saveNote(
                    currentArtifact.title,
                    currentArtifact.payload.explanation,
                    {
                      ...currentArtifact.refs[0],
                      artifact_id: currentArtifact.id,
                    },
                  ).catch((e) => toast(e.message, true))
                }
              >
                <BookmarkPlus size={15} />
                存入笔记
              </button>
            )}
          </div>
        </div>
        <div
          className="reader-scroll"
          ref={reader}
          onMouseUp={captureSelection}
        >
          {diagram ? (
            currentArtifact ? (
              <div className="diagram-page">
                <div className="eyebrow">LEARNING VISUALIZED</div>
                <h2>{currentArtifact.title}</h2>
                <p className="muted">
                  {currentArtifact.format === "steps"
                    ? "跟着每一步观察变化，把过程真正看明白。"
                    : "把抽象概念放到同一张图里。"}
                </p>
                <Diagram artifact={currentArtifact} />
                <div className="diagram-explanation">
                  <Markdown>{currentArtifact.payload.explanation}</Markdown>
                  <SourceLinks
                    refs={currentArtifact.refs}
                    onOpen={openSource}
                  />
                </div>
              </div>
            ) : (
              <Spinner label="正在加载图解" />
            )
          ) : !material ? (
            <Empty
              title="先带上一份学习资料"
              text={
                job?.kind === "handbook" && busy
                  ? "AI 正在编写学习手册，完成后会出现在这里。"
                  : "上传教材或讲义，开始边读边问。"
              }
            >
              <button
                className="btn primary"
                disabled={uploading}
                onClick={() => upload.current?.click()}
              >
                <Upload size={16} />
                上传资料
              </button>
              {space.goal && !busy && (
                <button
                  className="btn"
                  onClick={() =>
                    void startJob("handbook", { goal: space.goal }).catch((e) =>
                      toast(e.message, true),
                    )
                  }
                >
                  <Sparkles size={16} />
                  生成学习手册
                </button>
              )}
            </Empty>
          ) : material.kind === "pdf" && !textView ? (
            <div className="pdf-container">
              <PdfReader
                url={`/files/${space.id}/${material.id}`}
                page={page}
                preferCompatibility={material.page_count >= 200}
              />
            </div>
          ) : pageError ? (
            <Empty title="原文加载失败" text={pageError} />
          ) : !content ? (
            <div className="page-loading">
              <Spinner label="正在打开原文" />
            </div>
          ) : (
            <article className="document-page">
              <div className="document-kicker">
                <span>
                  {material.kind === "generated"
                    ? "AI 生成学习手册 · 请结合课程核对"
                    : "学习资料"}
                </span>
                <span>
                  {String(page).padStart(2, "0")} /{" "}
                  {String(material.page_count).padStart(2, "0")}
                </span>
              </div>
              {material.kind === "pdf" ? (
                <>
                  <h1>{content.title}</h1>
                  <div className="plain-source">
                    {content.text || "本页没有可提取的文字。"}
                  </div>
                </>
              ) : (
                <Markdown>{content.text}</Markdown>
              )}
              <div className="document-end">
                <span />
                学一点，留下一点理解
                <span />
              </div>
            </article>
          )}
        </div>
        {selection && !diagram && (
          <div className="selection-bar">
            <Focus size={15} />
            <span title={selection}>
              已选中：{selection.slice(0, 50)}
              {selection.length > 50 ? "…" : ""}
            </span>
            <button
              onClick={() =>
                void ask(
                  "chat",
                  "请解释我选中的这段文字，用简单例子帮助我理解。",
                )
              }
              disabled={busy}
            >
              解释选区
            </button>
            <button
              aria-label="取消选区"
              onClick={() => {
                setSelection("");
                window.getSelection()?.removeAllRanges();
              }}
            >
              <X size={14} />
            </button>
          </div>
        )}
        <div className="reader-bottom">
          <span>
            {diagram
              ? "图解基于原文与 AI 补充知识生成"
              : "选中文字，可针对这一段提问"}
          </span>
          {material && (
            <div className="page-controls">
              <button
                aria-label="上一页"
                className="icon-button"
                disabled={page <= 1}
                onClick={() => void changePage(page - 1)}
              >
                <ArrowLeft size={15} />
              </button>
              <label>
                <input
                  aria-label="当前页码"
                  key={`${material.id}:${page}`}
                  type="number"
                  defaultValue={page}
                  min={1}
                  max={material.page_count}
                  onBlur={(e) => {
                    const p = Number(e.target.value);
                    if (p >= 1 && p <= material.page_count && p !== page)
                      void changePage(p);
                    else e.target.value = String(page);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") e.currentTarget.blur();
                  }}
                />{" "}
                / {material.page_count} {unit}
              </label>
              <button
                aria-label="下一页"
                className="icon-button"
                disabled={page >= material.page_count}
                onClick={() => void changePage(page + 1)}
              >
                <ArrowRight size={15} />
              </button>
            </div>
          )}
        </div>
      </section>
      <aside className="assistant-panel">
        <div className="assistant-heading">
          <span className="ai-avatar">
            <Sparkles size={18} />
          </span>
          <div>
            <strong>学习助教</strong>
            <span>陪你把问题想明白</span>
          </div>
          <span className="ai-tag">AI</span>
        </div>
        <div className="context-strip">
          <span className="status-dot connected" />
          {material ? `已关联当前资料 · 第 ${page} ${unit}` : "通用学习问答"}
          <BookOpen size={13} />
        </div>
        <div className="chat-scroll" ref={scroll}>
          {!messages.length && !busy && (
            <div className="chat-welcome">
              <span className="welcome-spark">
                <Sparkles size={24} />
              </span>
              <h3>哪里还没想明白？</h3>
              <p>
                告诉我你的困惑，或者选中一段原文。
                <br />
                我们可以举例、画图，也可以练一道题。
              </p>
              <div className="chat-suggestions">
                <button
                  disabled={busy}
                  onClick={() =>
                    void ask(
                      "chat",
                      "请先用最简单的话，讲解本页的核心概念，再给我一个例子。",
                    )
                  }
                >
                  <BookOpen size={16} />
                  <span>这节在讲什么？</span>
                  <ChevronRight size={14} />
                </button>
                <button disabled={busy} onClick={() => void ask("visualize")}>
                  <Network size={16} />
                  <span>画个图，帮我理解</span>
                  <ChevronRight size={14} />
                </button>
                <button
                  disabled={!material}
                  onClick={() => navigate(`/space/${space.id}/practice`)}
                >
                  <Target size={16} />
                  <span>用练习检验一下</span>
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          )}
          {messages.map((m) => (
            <div key={m.id} className={"chat-message " + m.role}>
              {m.role === "assistant" && (
                <span className="message-role">
                  <Sparkles size={13} />
                  BookMind 助教
                </span>
              )}
              <Markdown>{m.content}</Markdown>
              {m.artifact_id && artifacts[m.artifact_id] && (
                <button
                  className="artifact-preview"
                  onClick={() => setDiagram(m.artifact_id)}
                >
                  <span className="artifact-preview-icon">
                    <Network size={27} />
                  </span>
                  <div>
                    <strong>{artifacts[m.artifact_id].title}</strong>
                    <small>
                      {artifacts[m.artifact_id].format === "steps"
                        ? "可逐步播放的过程演示"
                        : "知识图解"}{" "}
                      · 点击展开
                    </small>
                  </div>
                  <Maximize2 size={15} />
                </button>
              )}
              {m.role === "assistant" && (
                <>
                  <SourceLinks refs={m.refs} onOpen={openSource} />
                  <div className="message-actions">
                    <button
                      onClick={() =>
                        void saveNote(
                          m.artifact_id
                            ? artifacts[m.artifact_id]?.title || "知识图解"
                            : m.content
                                .replace(/[#*]/g, "")
                                .split("\n")[0]
                                .slice(0, 45),
                          m.content,
                          {
                            ...m.refs[0],
                            message_id: m.id,
                            ...(m.artifact_id
                              ? { artifact_id: m.artifact_id }
                              : {}),
                          },
                        ).catch((e) => toast(e.message, true))
                      }
                    >
                      <BookmarkPlus size={13} />
                      存入笔记
                    </button>
                    {m.artifact_id && (
                      <button onClick={() => setDiagram(m.artifact_id)}>
                        <Maximize2 size={12} />
                        展开图解
                      </button>
                    )}
                  </div>
                </>
              )}
            </div>
          ))}
          {busy && job?.kind === "chat" && (
            <div className="chat-message assistant generating">
              <span className="message-role">
                <Sparkles size={13} />
                BookMind 助教
              </span>
              {job.partial ? (
                <Markdown>{job.partial}</Markdown>
              ) : (
                <Spinner label="正在阅读资料、组织讲解" />
              )}
              <span className="typing-dot" />
            </div>
          )}
          {busy && job?.kind === "visualize" && (
            <div className="chat-message assistant">
              <Spinner label="正在设计图解，请稍等" />
            </div>
          )}
          {job?.status === "failed" && (
            <div className="error-inline">
              上次任务未完成：{job.error}
              <br />
              可以调整问题后重新发送。
            </div>
          )}
        </div>
        <div className="chat-composer">
          <div className="composer-actions">
            <button
              disabled={busy}
              onClick={() =>
                void ask(
                  "chat",
                  "请用一个具体例子帮我理解本页内容，先解释每一步为什么这样做。",
                )
              }
            >
              <BookOpen size={13} />
              举个例子
            </button>
            <button disabled={busy} onClick={() => void ask("visualize")}>
              <Network size={13} />
              画图理解
            </button>
            <button
              disabled={!material}
              onClick={() => navigate(`/space/${space.id}/practice`)}
            >
              <Target size={13} />
              练习本节
            </button>
          </div>
          {selection && (
            <div className="composer-selection">
              <Focus size={12} />
              <span>针对已选文字</span>
              <button
                aria-label="清除已选文字"
                onClick={() => setSelection("")}
              >
                <X size={12} />
              </button>
            </div>
          )}
          <div className="composer-input">
            <textarea
              aria-label="向学习助教提问"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={
                material
                  ? "这段为什么这样解释？可以举个例子吗…"
                  : "先聊聊你的学习问题…"
              }
              maxLength={6000}
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing
                ) {
                  e.preventDefault();
                  if (!busy && input.trim()) void ask("chat");
                }
              }}
            />
            <div>
              <select
                aria-label="提问范围"
                value={scope}
                onChange={(e) => setScope(e.target.value)}
              >
                <option value="page">当前页 / 节</option>
                <option value="material">整份资料</option>
              </select>
              <button
                className="send-button"
                aria-label="发送问题"
                disabled={busy || !input.trim()}
                onClick={() => void ask("chat")}
              >
                <Send size={16} />
              </button>
            </div>
          </div>
          <p className="composer-footnote">
            AI 可能出错，请结合原文判断 · Enter 发送
          </p>
        </div>
      </aside>
    </div>
  );
}
