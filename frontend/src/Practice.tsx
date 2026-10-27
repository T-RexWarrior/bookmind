import { useEffect, useState } from "react";
import {
  BookmarkPlus,
  Check,
  CheckCircle2,
  ChevronRight,
  Clock3,
  FileText,
  Lightbulb,
  RotateCcw,
  Sparkles,
  Target,
  XCircle,
} from "lucide-react";
import { useApp, useWorkspace } from "./App";
import { api, post, prettyDate } from "./api";
import { Empty, Markdown, SourceLinks, Spinner } from "./components";
import type { Question, Quiz } from "./types";

export default function Practice() {
  const {
    space,
    material,
    page,
    job,
    jobVersion,
    startJob,
    openSource,
    refresh,
    saveNote,
  } = useWorkspace();
  const { toast } = useApp();
  const [quizzes, setQuizzes] = useState<Quiz[]>([]),
    [selected, setSelected] = useState(""),
    [topic, setTopic] = useState(""),
    [scope, setScope] = useState("page"),
    [count, setCount] = useState(3),
    [filter, setFilter] = useState("all");
  const [answers, setAnswers] = useState<Record<string, number>>({}),
    [pending, setPending] = useState("");
  useEffect(() => {
    api<Quiz[]>(`/spaces/${space.id}/quizzes`)
      .then((qs) => {
        setQuizzes(qs);
        setSelected((s) => (qs.some((q) => q.id === s) ? s : qs[0]?.id || ""));
      })
      .catch((e) => toast(e.message, true));
  }, [space.id, jobVersion, toast]);
  useEffect(() => {
    if (
      job?.kind === "quiz" &&
      job.status === "completed" &&
      job.result?.quiz_id
    )
      setSelected(job.result.quiz_id);
  }, [jobVersion, job]);
  const quiz = quizzes.find((q) => q.id === selected),
    busy = !!pending || (!!job && ["queued", "running"].includes(job.status));
  const updateQuiz = async (value: Quiz) => {
    setQuizzes((qs) => qs.map((q) => (q.id === value.id ? value : q)));
    await refresh();
  };
  const generate = async () => {
    if (!material) return;
    setPending("generate");
    try {
      await startJob("quizzes", {
        material_id: material.id,
        page,
        scope,
        topic,
        count,
      });
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setPending("");
    }
  };
  const submit = async (q: Question) => {
    if (!quiz || answers[q.id] === undefined) return;
    setPending(q.id);
    try {
      await updateQuiz(
        await post<Quiz>(`/spaces/${space.id}/quizzes/${quiz.id}/answer`, {
          question_id: q.id,
          answer: answers[q.id],
        }),
      );
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setPending("");
    }
  };
  const hint = async (q: Question) => {
    if (!quiz) return;
    setPending(q.id);
    try {
      await updateQuiz(
        await post<Quiz>(
          `/spaces/${space.id}/quizzes/${quiz.id}/hints/${q.id}`,
        ),
      );
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setPending("");
    }
  };
  const wrongCount = quizzes.reduce(
    (n, q) =>
      n + q.questions.filter((x) => x.attempt && x.attempt.score < 1).length,
    0,
  );
  const completed = quiz?.questions.filter((q) => q.attempt).length || 0;
  const correct =
    quiz?.questions.filter((q) => q.attempt?.score === 1).length || 0;
  const items =
    filter === "wrong"
      ? quizzes.flatMap((set) =>
          set.questions
            .filter((q) => q.attempt && q.attempt.score < 1)
            .map((q) => ({ q, set })),
        )
      : quiz?.questions.map((q) => ({ q, set: quiz })) || [];
  return (
    <div className="practice-page">
      <div className="page-intro">
        <div>
          <div className="eyebrow">PRACTICE TO UNDERSTAND</div>
          <h1>练习，让理解有依据。</h1>
          <p>从正在读的内容出发，看看自己会了什么，还有哪里需要再想一想。</p>
        </div>
        <div className="page-intro-icon">
          <Target size={45} strokeWidth={1.3} />
        </div>
      </div>
      <div className="practice-layout">
        <aside className="practice-controls">
          <div className="white-panel generate-panel">
            <div className="panel-title">
              <Sparkles size={17} />
              <h3>生成一组练习</h3>
            </div>
            <label className="field">
              当前学习资料
              <div className="source-scope">
                <FileText size={18} />
                <div>
                  <strong>{material?.title || "请先添加资料"}</strong>
                  <span>
                    第 {page} {material?.kind === "pdf" ? "页" : "节"} ·
                    在学习页切换
                  </span>
                </div>
              </div>
            </label>
            <label className="field">
              出题范围
              <select value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="page">当前页 / 节</option>
                <option value="material">整份资料（按知识点检索）</option>
              </select>
            </label>
            <label className="field">
              重点知识点（选填）
              <input
                aria-label="重点知识点"
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                placeholder="例如：中序遍历、递归调用栈"
                maxLength={500}
              />
            </label>
            <label className="field">
              题目数量
              <select value={count} onChange={(e) => setCount(+e.target.value)}>
                <option value={3}>3 道 · 快速检查</option>
                <option value={5}>5 道 · 多练一点</option>
              </select>
            </label>
            <button
              className="btn primary full"
              onClick={() => void generate()}
              disabled={busy || !material}
            >
              {busy && job?.kind === "quiz" ? (
                <Spinner label="正在出题" />
              ) : (
                <>
                  <Sparkles size={16} />
                  生成练习
                </>
              )}
            </button>
            <p className="fine-print">
              第一版提供四选一理解题。题目附原文依据，提交后查看答案与解析。
            </p>
          </div>
          <div className="white-panel history-panel">
            <div className="panel-title">
              <Clock3 size={16} />
              <h3>练习记录</h3>
              <span>{quizzes.length}</span>
            </div>
            {quizzes.length ? (
              quizzes.map((q) => (
                <button
                  key={q.id}
                  className={
                    "quiz-history-item " +
                    (selected === q.id && filter === "all" ? "active" : "")
                  }
                  onClick={() => {
                    setSelected(q.id);
                    setFilter("all");
                  }}
                >
                  <strong>{q.title}</strong>
                  <span>
                    {prettyDate(q.created)} · 已完成{" "}
                    {q.questions.filter((x) => x.attempt).length}/
                    {q.questions.length} 题
                  </span>
                  <ChevronRight size={14} />
                </button>
              ))
            ) : (
              <p className="muted">完成的练习会保存在这里。</p>
            )}
          </div>
        </aside>
        <main className="practice-main">
          <div className="practice-stat-grid">
            <div>
              <span className="stat-icon">
                <CheckCircle2 size={20} />
              </span>
              <strong>{space.progress.answered}</strong>
              <span>已提交题目</span>
            </div>
            <div>
              <span className="stat-icon">
                <Target size={20} />
              </span>
              <strong>
                {space.progress.answered
                  ? Math.round(
                      (space.progress.correct / space.progress.answered) * 100,
                    ) + "%"
                  : "—"}
              </strong>
              <span>已提交题目正确率</span>
            </div>
            <div>
              <span className="stat-icon orange">
                <RotateCcw size={20} />
              </span>
              <strong>{wrongCount}</strong>
              <span>需要回顾的错题</span>
            </div>
          </div>
          <div className="practice-main-tabs">
            <button
              className={filter === "all" ? "active" : ""}
              onClick={() => setFilter("all")}
            >
              当前练习
            </button>
            <button
              className={filter === "wrong" ? "active" : ""}
              onClick={() => setFilter("wrong")}
            >
              错题回顾 <span>{wrongCount}</span>
            </button>
          </div>
          {!items.length ? (
            <Empty
              title={
                filter === "wrong" ? "暂时没有错题" : "从这一节，开始练一练"
              }
              text={
                filter === "wrong"
                  ? "已提交的错题会自动汇总在这里。"
                  : "选择知识点，生成一组小练习。答完后，回到原文核对理解。"
              }
            />
          ) : (
            <>
              <div className="quiz-heading">
                <div>
                  <h2>
                    {filter === "wrong" ? "需要再想一想的问题" : quiz?.title}
                  </h2>
                  <span>
                    {filter === "wrong"
                      ? `${wrongCount} 道错题 · 可查看解析、回到原文`
                      : `已提交 ${completed}/${quiz?.questions.length} 题 · 答对 ${correct} 题`}
                  </span>
                </div>
                {filter === "all" && quiz && (
                  <SourceLinks refs={quiz.refs} onOpen={openSource} />
                )}
              </div>
              {items.map(({ q, set }, i) => (
                <article
                  key={q.id}
                  className={
                    "question-card " +
                    (q.attempt
                      ? q.attempt.score === 1
                        ? "correct"
                        : "incorrect"
                      : "")
                  }
                >
                  <div className="question-meta">
                    <span className="question-index">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <span className="question-type">单选题</span>
                    <span className="topic-badge">{q.topic}</span>
                    {q.attempt && (
                      <span
                        className={
                          "answer-result " +
                          (q.attempt.score === 1 ? "right" : "wrong")
                        }
                      >
                        {q.attempt.score === 1 ? (
                          <CheckCircle2 size={14} />
                        ) : (
                          <XCircle size={14} />
                        )}{" "}
                        {q.attempt.score === 1
                          ? q.attempt.hint_used
                            ? "提示后答对"
                            : "独立答对"
                          : "还需巩固"}
                      </span>
                    )}
                  </div>
                  <Markdown>{q.stem}</Markdown>
                  <div className="question-options">
                    {q.choices.map((choice, index) => {
                      const chosen = q.attempt
                        ? Number(q.attempt.answer) === index
                        : answers[q.id] === index;
                      const status =
                        q.attempt && q.answer === index
                          ? "right"
                          : q.attempt && chosen
                            ? "wrong"
                            : chosen
                              ? "selected"
                              : "";
                      return (
                        <button
                          key={index}
                          disabled={!!q.attempt || busy}
                          className={"question-option " + status}
                          onClick={() =>
                            setAnswers((a) => ({ ...a, [q.id]: index }))
                          }
                        >
                          <span className="option-letter">
                            {String.fromCharCode(65 + index)}
                          </span>
                          <span>{choice}</span>
                          {q.attempt && q.answer === index ? (
                            <Check size={17} />
                          ) : chosen ? (
                            <span className="option-dot" />
                          ) : null}
                        </button>
                      );
                    })}
                  </div>
                  {q.hint && !q.attempt && (
                    <div className="hint-box">
                      <Lightbulb size={17} />
                      <div>
                        <strong>一点提示</strong>
                        <p>{q.hint}</p>
                        <small>
                          本题会记录为“使用提示”，与独立作答分开统计。
                        </small>
                      </div>
                    </div>
                  )}
                  {q.attempt ? (
                    <div className="answer-explanation">
                      <div className="explanation-heading">
                        <Lightbulb size={16} />
                        <strong>参考解析</strong>
                        <span>
                          正确答案：
                          {q.answer === undefined
                            ? ""
                            : String.fromCharCode(65 + q.answer)}
                        </span>
                      </div>
                      <Markdown>{q.explanation || q.attempt.feedback}</Markdown>
                      {q.evidence && (
                        <blockquote>
                          <span>原文依据</span>
                          {q.evidence}
                        </blockquote>
                      )}
                      <div className="explanation-actions">
                        <SourceLinks
                          refs={q.refs || set.refs}
                          onOpen={openSource}
                        />
                        <button
                          className="text-button"
                          onClick={() =>
                            void saveNote(
                              `${q.topic} · 练习回顾`,
                              `## ${q.stem}\n\n${q.choices.map((c, j) => `${String.fromCharCode(65 + j)}. ${c}`).join("\n\n")}\n\n我的答案：${String.fromCharCode(65 + Number(q.attempt!.answer))}\n\n正确答案：${String.fromCharCode(65 + (q.answer || 0))}\n\n${q.explanation}\n\n> ${q.evidence}`,
                              { ...(q.refs || set.refs)[0], quiz_id: set.id },
                            ).catch((e) => toast(e.message, true))
                          }
                        >
                          <BookmarkPlus size={14} />
                          存入笔记
                        </button>
                      </div>
                    </div>
                  ) : (
                    <div className="question-footer">
                      <button
                        className="text-button"
                        disabled={busy || !!q.hint}
                        onClick={() => void hint(q)}
                      >
                        <Lightbulb size={15} />
                        {q.hint ? "已查看提示" : "给我一点提示"}
                      </button>
                      <button
                        className="btn primary small"
                        disabled={busy || answers[q.id] === undefined}
                        onClick={() => void submit(q)}
                      >
                        {pending === q.id ? (
                          <Spinner label="提交中" />
                        ) : (
                          "提交答案"
                        )}
                        <ChevronRight size={14} />
                      </button>
                    </div>
                  )}
                </article>
              ))}
            </>
          )}
          {space.progress.topics.length > 0 && (
            <section className="white-panel learning-evidence">
              <div className="panel-title">
                <Target size={17} />
                <h3>知识点学习记录</h3>
              </div>
              <p className="muted">
                状态仅依据本空间的已提交题目。少量练习不能证明全面掌握。
              </p>
              {space.progress.topics.map((t) => (
                <div key={t.topic} className="topic-row">
                  <div>
                    <strong>{t.topic}</strong>
                    <span>
                      已答 {t.answered} 题 · 独立答对 {t.independent} 题 · 错题{" "}
                      {t.wrong} 道
                    </span>
                  </div>
                  <span className={"topic-status " + (t.wrong ? "warn" : "")}>
                    {t.status}
                  </span>
                </div>
              ))}
            </section>
          )}
        </main>
      </div>
    </div>
  );
}
