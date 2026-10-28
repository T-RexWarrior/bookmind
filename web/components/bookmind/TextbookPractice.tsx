'use client'

import Link from 'next/link'
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { BookOpen, CheckCheck, Clock3, GraduationCap, Loader2, RotateCcw, X } from 'lucide-react'
import QuizConfigPanel from '@/components/quiz/QuizConfigPanel'
import QuizViewer from '@/components/quiz/QuizViewer'
import QuizFollowupTabBody from '@/components/quiz/QuizFollowupTabBody'
import { QuizFollowupProvider, useQuizFollowupController, type QuizFollowupTabContext } from '@/context/QuizFollowupContext'
import { useChatStateAdapter } from '@/features/chat/ChatStateAdapter'
import { DEFAULT_QUIZ_CONFIG, buildQuizWSConfig, extractQuizQuestions, type DeepQuestionFormConfig, type QuizQuestion } from '@/lib/quiz-types'
import { fetchMasteryTopic, type TopicReview } from '@/lib/learning-api'
import { masterySessionRoute } from '@/lib/mastery-mode'
import { setPendingPrompt } from '@/lib/pending-prompt'
import { apiFetch } from '@/shared/api/client'
import TextbookMistakes from './TextbookMistakes'
import { useBookMind, textbookUnitLabel, type Textbook } from './BookMindProvider'

type PracticeSnapshot = {
  session_id: string
  turn_id: string | null
  questions: Record<string, unknown>[]
  objective_id?: string
  start?: number
  end?: number
}

async function openPractice(bookId: string, signal?: AbortSignal): Promise<PracticeSnapshot> {
  const response = await apiFetch(`/api/bookmind/books/${bookId}/practice/session`, { method: 'POST', signal })
  if (!response.ok) throw new Error('练习会话加载失败，请重试。')
  return response.json()
}

export default function TextbookPractice() {
  const { activeBook: book, refresh } = useBookMind()
  const [tab, setTab] = useState<'quiz' | 'mistakes' | 'review'>('quiz')

  if (!book) return <div className="bm-empty">正在打开教材练习…</div>
  return (
    <div className="bm-practice-page">
      <nav className="bm-practice-tabs" aria-label="练习内容">
        {([{ id: 'quiz', label: '专项练习', icon: CheckCheck }, { id: 'mistakes', label: '错题本', icon: RotateCcw }, { id: 'review', label: '复习计划', icon: Clock3 }] as const).map(item => (
          <button key={item.id} type="button" className={tab === item.id ? 'active' : ''} aria-pressed={tab === item.id} onClick={() => { setTab(item.id); void refresh() }}><item.icon size={15} />{item.label}</button>
        ))}
      </nav>
      <div className="bm-practice-panel" hidden={tab !== 'quiz'}><PracticeGenerator key={book.id} book={book} /></div>
      {tab === 'mistakes' && <TextbookMistakes />}
      {tab === 'review' && <ReviewPlan key={book.id} book={book} />}
    </div>
  )
}

function ReviewPlan({ book }: { book: Textbook }) {
  const [reviews, setReviews] = useState<TopicReview[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    void fetchMasteryTopic(book.path_id, { cache: 'no-store' }).then(topic => {
      if (active) setReviews([...topic.reviews].sort((a, b) => Number(b.due) - Number(a.due) || a.due_at - b.due_at))
    }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : '复习计划加载失败。') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [book.path_id])
  return <main className="bm-practice-content">
    <div className="bm-page-intro"><span className="bm-eyebrow">REVIEW</span><h1>复习计划</h1><p>自主学习、AI 导学与练习中的有效作答，共同形成这份复习计划。</p></div>
    {loading ? <p>正在读取复习计划…</p> : error ? <p role="alert" className="bm-error">{error}</p> : reviews.length === 0 ? <div className="bm-empty">完成一次随堂测验或专项练习后，这里会出现复习计划。</div> : (
      <div className="bm-review-list">{reviews.map(review => (
        <article key={review.id}><div><span className={review.due ? 'bm-review-due' : 'bm-review-later'}>{review.due ? '待复习' : '已安排'}</span><h2>{review.knowledge_point_name}</h2><p>{new Date(review.due_at * 1000).toLocaleString('zh-CN', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</p></div>
          <Link className="bm-secondary" href={masterySessionRoute(book.path_id, 'review')} onClick={() => setPendingPrompt(`请带我复习「${review.knowledge_point_name}」（知识点 ID：${review.knowledge_point_id}），依据教材进行随堂测验，并按原有规则记录结果。`, 'mastery_path')}>开始复习 <GraduationCap size={15} /></Link>
        </article>
      ))}</div>
    )}
  </main>
}

function PracticeGenerator({ book }: { book: Textbook }) {
  const objectiveParam = useSearchParams().get('objective')
  const { state, loadSession, configureSession, sendMessage, cancelStreamingTurn } = useChatStateAdapter()
  const [selected, setSelected] = useState('')
  const [startInput, setStartInput] = useState('')
  const [endInput, setEndInput] = useState('')
  const [config, setConfig] = useState<DeepQuestionFormConfig>({ ...DEFAULT_QUIZ_CONFIG, question_types: ['choice'] })
  const [snapshot, setSnapshot] = useState<PracticeSnapshot | null>(null)
  const [sessionId, setSessionId] = useState('')
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')
  const streamSeen = useRef(false)
  const sequence = useRef(0)
  const point = book.objectives.find(row => row.id === (selected || objectiveParam || book.current_objective_id)) || book.objectives.find(row => row.start !== null)
  const start = Number(startInput || point?.start || 1)
  const end = Number(endInput || Math.min(point?.end || start, start + 23))
  const validRange = Boolean(point && point.start !== null && point.end !== null && Number.isSafeInteger(start) && Number.isSafeInteger(end) && point.start <= start && start <= end && end <= point.end && end - start < 24)
  const ready = Boolean(sessionId && state.sessionId === sessionId && !loading)
  const busy = generating || (ready && state.isStreaming)
  const unit = textbookUnitLabel(book.unit)

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    void openPractice(book.id, controller.signal).then(async result => {
      await loadSession(result.session_id, { signal: controller.signal })
      if (!active) return
      configureSession({ capability: 'deep_question', workspaceMode: null, masteryPathId: null, masterySessionMode: null, knowledgeBases: [], enabledTools: [] }, result.session_id)
      setSessionId(result.session_id)
      setSnapshot(result)
    }).catch(reason => { if (active) setError(reason instanceof Error ? reason.message : '练习加载失败。') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false; controller.abort(); sequence.current += 1 }
  }, [book.id, loadSession, configureSession])

  useEffect(() => {
    if (!ready) return
    if (state.isStreaming) {
      streamSeen.current = true
      return
    }
    const failed = state.lastTurnFailed || state.submissionFailed
    if (!streamSeen.current && !failed) return
    streamSeen.current = false
    const version = ++sequence.current
    void openPractice(book.id).then(result => {
      if (sequence.current === version) {
        setSnapshot(result)
        if (failed) setError('本次出题未完成，请检查 API 连接后重试。')
      }
    }).catch(reason => { if (sequence.current === version) setError(String(reason)) })
      .finally(() => { if (sequence.current === version) setGenerating(false) })
  }, [book.id, ready, state.isStreaming, state.lastTurnFailed, state.submissionFailed])

  function generate() {
    if (!point || !validRange || !ready || busy) return
    setError('')
    setGenerating(true)
    setSnapshot(null)
    sendMessage(`请依据本次附带的「${book.title}」原文，为知识点「${point.name}」出题。原文范围：第 ${start}–${end} ${unit}。严格遵守题数、难度、题型及配比，用中文出题。${config.topic.trim() ? `补充要求：${config.topic.trim()}` : ''}`,
      undefined, { ...buildQuizWSConfig(config), bookmind_objective_id: point.id, bookmind_start: start, bookmind_end: end },
      undefined, undefined, { capability: 'deep_question' })
  }
  const questions = snapshot?.questions.length ? extractQuizQuestions({ summary: { results: snapshot.questions } }) || [] : []
  const snapshotPoint = book.objectives.find(row => row.id === snapshot?.objective_id)
  const currentAssistant = [...state.messages].reverse().find(message => message.role === 'assistant')
  const progress = ready && busy ? (currentAssistant?.events || []).filter(event => event.type === 'content' && event.metadata?.call_kind === 'quiz_question_emitted').length : 0

  return (
    <main className="bm-practice-content">
      <div className="bm-page-intro"><span className="bm-eyebrow">PRACTICE</span><h1>练习巩固</h1><p>围绕教材知识点，自定题数、难度与题型。作答后查看解析、请求 AI 评阅，也可以继续追问。</p></div>
      <section className="bm-practice-config" aria-label="练习配置">
        <fieldset disabled={busy || loading}>
          <div className="bm-practice-scope">
            <label>知识点<select aria-label="练习知识点" value={point?.id || ''} onChange={event => { setSelected(event.target.value); setStartInput(''); setEndInput(''); setError('') }}>
              {book.objectives.filter(row => row.start !== null).map(row => <option key={row.id} value={row.id}>{row.module_name === row.name ? row.name : `${row.module_name} · ${row.name}`}</option>)}
            </select></label>
            <label>起始{unit}<input aria-label="练习起始位置" type="number" min={point?.start || 1} max={point?.end || 1} value={startInput || point?.start || 1} onChange={event => { setStartInput(event.target.value); setEndInput('') }} /></label>
            <label>结束{unit}<input aria-label="练习结束位置" type="number" min={start} max={Math.min(point?.end || start, start + 23)} value={endInput || end} onChange={event => setEndInput(event.target.value)} /></label>
          </div>
          <p className="bm-scope-hint">知识点原文：第 {point?.start}–{point?.end} {unit}；本次依据第 {start}–{end} {unit} 出题，每次最多选取 24 个原文单元。</p>
          <div className="bm-native-quiz-config"><QuizConfigPanel customOnly value={config} onChange={setConfig} uploadedPdf={null} onUploadPdf={() => {}} /></div>
          <label className="bm-practice-requirements">补充要求（可选）<textarea value={config.topic} maxLength={2000} placeholder="例如：多考察递归过程，结合具体例子，暂不涉及代码。" onChange={event => setConfig(previous => ({ ...previous, topic: event.target.value }))} /></label>
        </fieldset>
        <div className="bm-practice-actions">
          <p>客观题自动判分；简答、论述与编程题可查看参考答案并请求 AI 评阅，未判定的答案不会直接增加掌握度。</p>
          {busy && state.isStreaming && <button type="button" className="bm-secondary" onClick={cancelStreamingTurn}>停止出题</button>}
          <button type="button" className="bm-primary" disabled={!validRange || !ready || busy} onClick={generate}>{busy || loading ? <Loader2 size={16} className="animate-spin" /> : <CheckCheck size={16} />}{loading ? '正在加载…' : busy ? `正在出题${progress ? ` · 已生成 ${progress} 题` : '…'}` : '生成练习'}</button>
        </div>
      </section>
      {error && <p role="alert" className="bm-error">{error}</p>}
      {busy && <p className="bm-generation-status" role="status"><Loader2 size={15} className="animate-spin" />{state.currentStage === 'quizzing' ? '正在逐题生成题目与解析' : '正在分析教材、规划本次练习'}{progress > 0 ? `，已生成 ${progress} 题` : '，请稍候…'}</p>}
      {snapshot?.turn_id && questions.length > 0 && <section className="bm-practice-quiz" aria-label="本次练习题">
        <div className="bm-practice-quiz-heading"><h2>{snapshotPoint?.name} · {questions.length} 道题</h2><Link href={`${book.links.learn}?locator=${snapshot.start || 1}`}><BookOpen size={14} />核对原文 · 第 {snapshot.start}–{snapshot.end} {unit}</Link></div>
        <PracticeQuestions key={snapshot.turn_id} questions={questions} sessionId={snapshot.session_id} turnId={snapshot.turn_id} />
      </section>}
      {!snapshot?.questions.length && !busy && !loading && <div className="bm-practice-empty"><CheckCheck size={32} /><h2>从刚学过的知识点开始</h2><p>自主学习和 AI 导学中的随堂测验适合边学边检验；这里可以配置一组完整的专项练习。</p><Link href={book.links.guide}>进入 AI 导学 →</Link></div>}
    </main>
  )
}

function PracticeQuestions(props: { questions: QuizQuestion[]; sessionId: string; turnId: string }) {
  return <QuizFollowupProvider><PracticeQuestionBody {...props} /></QuizFollowupProvider>
}

function PracticeQuestionBody(props: { questions: QuizQuestion[]; sessionId: string; turnId: string }) {
  const [followup, setFollowup] = useState<QuizFollowupTabContext | null>(null)
  const { setOpenTabHandler } = useQuizFollowupController()
  useEffect(() => {
    setOpenTabHandler(setFollowup)
    return () => setOpenTabHandler(null)
  }, [setOpenTabHandler])
  return <>
    <QuizViewer {...props} language="zh" allowImages={false} />
    {followup && <section className="bm-practice-followup" aria-label="题目追问"><header><h3>{followup.tabLabel}</h3><button type="button" aria-label="收起题目追问" onClick={() => setFollowup(null)}><X size={17} /></button></header><QuizFollowupTabBody context={followup} /></section>}
  </>
}
