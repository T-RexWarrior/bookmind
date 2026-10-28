'use client'

import Link from 'next/link'
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import { BookOpen, CheckCheck, Clock3, GraduationCap, Loader2, RotateCcw } from 'lucide-react'
import { ExtensionResult } from '@/components/reading/ReadingExtensionBar'
import { runReadingExtension, type ReadingExtensionResult } from '@/lib/reading-api'
import { fetchMasteryTopic, type TopicReview } from '@/lib/learning-api'
import { masterySessionRoute } from '@/lib/mastery-mode'
import { setPendingPrompt } from '@/lib/pending-prompt'
import TextbookMistakes from './TextbookMistakes'
import { useBookMind, textbookUnitLabel } from './BookMindProvider'

type Quiz = { result: ReadingExtensionResult; locator: number; objectiveId: string }

export default function TextbookPractice() {
  const { activeBook: book, refresh } = useBookMind()
  const objectiveParam = useSearchParams().get('objective')
  const [tab, setTab] = useState<'quiz' | 'mistakes' | 'review'>('quiz')
  const [selected, setSelected] = useState('')
  const [pageInput, setPageInput] = useState('')
  const [quiz, setQuiz] = useState<Quiz | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [reviews, setReviews] = useState<TopicReview[]>([])
  const [reviewLoading, setReviewLoading] = useState(false)
  const [reviewError, setReviewError] = useState('')
  const requestVersion = useRef(0)
  const point = book?.objectives.find(row => row.id === (selected || objectiveParam || book.current_objective_id))
    || book?.objectives.find(row => row.start !== null)
  const locator = Number(pageInput || point?.start || 1)
  const validPage = Boolean(point && point.start !== null && point.end !== null && Number.isSafeInteger(locator) && locator >= point.start && locator <= point.end)

  useEffect(() => {
    requestVersion.current += 1
    setQuiz(null)
    setSelected('')
    setPageInput('')
    setBusy(false)
    setError('')
  }, [book?.id, objectiveParam])

  useEffect(() => {
    if (tab !== 'review' || !book) return
    let active = true
    setReviewLoading(true)
    setReviewError('')
    void fetchMasteryTopic(book.path_id, { cache: 'no-store' }).then(topic => {
      if (active) setReviews([...topic.reviews].sort((a, b) => Number(b.due) - Number(a.due) || a.due_at - b.due_at))
    }).catch(reason => { if (active) setReviewError(reason instanceof Error ? reason.message : '复习安排加载失败。') })
      .finally(() => { if (active) setReviewLoading(false) })
    return () => { active = false }
  }, [book?.path_id, tab])

  useEffect(() => () => { requestVersion.current += 1 }, [])

  async function generate() {
    if (!book || !point || !validPage || busy) return
    const version = ++requestVersion.current
    setBusy(true)
    setError('')
    // A regenerated native quiz replaces the answer key for this unit.
    setQuiz(null)
    try {
      const result = await runReadingExtension(book.material_id, 'quiz', 'start', { locator, locale: 'zh', objective_id: point.id })
      if (version === requestVersion.current) setQuiz({ result, locator, objectiveId: point.id })
    } catch (reason) {
      if (version === requestVersion.current) setError(reason instanceof Error ? reason.message : '出题失败，请重试。')
    } finally {
      if (version === requestVersion.current) setBusy(false)
    }
  }

  if (!book) return <div className="bm-empty">正在打开教材练习…</div>
  const unitLabel = textbookUnitLabel(book.unit)
  return (
    <div className="bm-practice-page">
      <nav className="bm-practice-tabs" aria-label="练习内容">
        {([{ id: 'quiz', label: '自测练习', icon: CheckCheck }, { id: 'mistakes', label: '错题本', icon: RotateCcw }, { id: 'review', label: '复习安排', icon: Clock3 }] as const).map(item => (
          <button key={item.id} type="button" className={tab === item.id ? 'active' : ''} aria-pressed={tab === item.id} onClick={() => { setTab(item.id); void refresh() }}><item.icon size={15} />{item.label}</button>
        ))}
      </nav>
      {tab === 'mistakes' ? <TextbookMistakes /> : tab === 'review' ? (
        <main className="bm-practice-content">
          <div className="bm-page-intro"><span className="bm-eyebrow">REVIEW</span><h1>让记住的知识，留得更久</h1><p>自主学习、AI 导学与练习中的有效作答，共同形成这份复习安排。</p></div>
          {reviewLoading ? <p>正在读取复习安排…</p> : reviewError ? <p role="alert" className="bm-error">{reviewError}</p> : reviews.length === 0 ? <div className="bm-empty">完成一次自测或导学检查后，这里会出现复习安排。</div> : (
            <div className="bm-review-list">{reviews.map(review => (
              <article key={review.id}><div><span className={review.due ? 'bm-review-due' : 'bm-review-later'}>{review.due ? '待复习' : '已安排'}</span><h2>{review.knowledge_point_name}</h2><p>{new Date(review.due_at * 1000).toLocaleString('zh-CN', { month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</p></div>
                <Link className="bm-secondary" href={masterySessionRoute(book.path_id, 'review')} onClick={() => setPendingPrompt(`请带我复习「${review.knowledge_point_name}」（知识点 ID：${review.knowledge_point_id}），依据教材进行理解检查，并按原有规则记录结果。`, 'mastery_path')}>开始复习 <GraduationCap size={15} /></Link>
              </article>
            ))}</div>
          )}
        </main>
      ) : (
        <main className="bm-practice-content">
          <div className="bm-page-intro"><span className="bm-eyebrow">PRACTICE</span><h1>用一道题，检验一份理解</h1><p>选择知识点与原文位置，生成三道理解题。作答结果会进入本书的学习记录。</p></div>
          <section className="bm-practice-setup" aria-label="出题范围">
            <label>知识点<select aria-label="练习知识点" disabled={busy} value={point?.id || ''} onChange={event => { setSelected(event.target.value); setPageInput(''); setQuiz(null); setError('') }}>
              {book.objectives.filter(row => row.start !== null).map(row => <option key={row.id} value={row.id}>{row.module_name === row.name ? row.name : `${row.module_name} · ${row.name}`}</option>)}
            </select></label>
            <label>出题位置（{unitLabel}）<input aria-label="练习原文位置" type="number" min={point?.start || 1} max={point?.end || 1} value={pageInput || point?.start || 1} disabled={busy} onChange={event => { setPageInput(event.target.value); setQuiz(null); setError('') }} /></label>
            <button type="button" className="bm-primary" disabled={!validPage || busy} onClick={() => void generate()}>{busy ? <Loader2 size={16} className="animate-spin" /> : <CheckCheck size={16} />}{busy ? '正在依据原文出题…' : quiz ? '重新出三道题' : '生成三道题'}</button>
            <p>范围：第 {point?.start}–{point?.end} {unitLabel}。本次依据所选原文位置出题，不代表已检查整个章节。</p>
          </section>
          {error && <p role="alert" className="bm-error">{error}</p>}
          {quiz ? <section className="bm-practice-quiz" aria-label="本次练习题"><div className="bm-practice-quiz-heading"><h2>{book.objectives.find(row => row.id === quiz.objectiveId)?.name}</h2><Link href={`${book.links.learn}?locator=${quiz.locator}`}><BookOpen size={14} />核对原文 · 第 {quiz.locator} {unitLabel}</Link></div>
            <ExtensionResult key={String(quiz.result.payload.questions && JSON.stringify(quiz.result.payload.questions))} result={quiz.result} materialId={book.material_id} locator={quiz.locator} closeLabel="收起本次练习" onClose={() => setQuiz(null)} onError={setError} variant="card" />
          </section> : !busy && <div className="bm-practice-empty"><CheckCheck size={32} /><h2>从刚学过的知识点开始</h2><p>也可以在自主学习时随堂自测，或让 AI 导学根据你的回答安排检查。</p><Link href={book.links.guide}>进入 AI 导学 →</Link></div>}
        </main>
      )}
    </div>
  )
}
