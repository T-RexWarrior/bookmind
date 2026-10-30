'use client'

import Link from 'next/link'
import { Suspense, useCallback, useEffect, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, BookOpen, Check, Download, Loader2, Pause, Plus, RotateCcw, Sparkles, Trash2 } from 'lucide-react'
import { bookApi, type BookWsEvent } from '@/lib/book-api'
import type { BookDepth, BookDetail, BookProposal, Chapter, Spine } from '@/lib/book-types'
import { randomUuid } from '@/lib/random-uuid'
import { apiFetch } from '@/shared/api/client'
import { useBookMind, type Textbook } from './BookMindProvider'

const LEARNING_BLOCKS = ['section', 'text', 'callout', 'quiz', 'code', 'flash_cards', 'timeline', 'figure']
const STATUS: Record<string, string> = { pending: '等待生成', planning: '组织内容', generating: '正在生成', ready: '已完成', partial: '部分内容未完成', error: '生成失败' }

export async function importGoalBook(bookId: string): Promise<Textbook> {
  const response = await apiFetch(`/api/bookmind/generated/${encodeURIComponent(bookId)}/import`, { method: 'POST' })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(typeof body?.detail === 'string' ? body.detail : '学习资料关联失败，请重试。')
  }
  return response.json()
}

export default function GoalLearning() {
  return <Suspense fallback={<div className="bm-empty">正在打开目标学习…</div>}><GoalRoute /></Suspense>
}

function GoalRoute() {
  const bookId = useSearchParams().get('book') || ''
  return <GoalWorkspace key={bookId || 'new'} bookId={bookId} />
}

function GoalWorkspace({ bookId }: { bookId: string }) {
  const router = useRouter()
  const { books, refresh } = useBookMind()
  const [goal, setGoal] = useState('')
  const [background, setBackground] = useState('')
  const [depth, setDepth] = useState<BookDepth>('standard')
  const [detail, setDetail] = useState<BookDetail | null>(null)
  const [loading, setLoading] = useState(Boolean(bookId))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [activity, setActivity] = useState('')
  const bound = books.find(book => book.generated_book_id === bookId)
  const book = detail?.book
  const failedPages = detail?.pages.filter(page => page.status === 'error' || page.status === 'partial') || []
  const completed = detail?.pages.filter(page => page.status === 'ready').length || 0
  const step = !book || book.status === 'draft' ? 0 : book.status === 'spine_ready' ? 1 : book.status === 'ready' && failedPages.length === 0 ? 3 : 2

  const load = useCallback(async () => {
    const next = await bookApi.get(bookId, { includeBlocks: false })
    setDetail(next)
    return next
  }, [bookId])

  useEffect(() => {
    if (!bookId) return
    let active = true
    void bookApi.get(bookId, { includeBlocks: false }).then(next => { if (active) setDetail(next) })
      .catch(reason => { if (active) setError(reason instanceof Error ? reason.message : '生成记录加载失败。') })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [bookId])

  useEffect(() => {
    if (!bookId || busy || !book || book.status !== 'compiling') return
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const next = await bookApi.get(bookId, { includeBlocks: false })
        if (active) { setDetail(next); setError('') }
      } catch { if (active) setError('暂时无法读取生成进度，正在重连…') }
      if (active) timer = setTimeout(poll, 2500)
    }
    timer = setTimeout(poll, 1500)
    return () => { active = false; clearTimeout(timer) }
  }, [bookId, book?.status, busy])

  const onEvent = (event: BookWsEvent) => {
    const stage = String(event.stage || '')
    if (stage.includes('ideation')) setActivity('正在梳理你的学习目标，拟定学习方案…')
    else if (stage.includes('exploration')) setActivity('正在分析学习范围和所需的基础知识…')
    else if (stage.includes('critique')) setActivity('正在检查目录的连贯性与知识顺序…')
    else if (stage.includes('synth') || stage.includes('spine')) setActivity('正在组织章节目录和学习目标…')
  }

  async function run(action: () => Promise<void>, label: string) {
    if (busy) return
    setBusy(true); setError(''); setActivity(label)
    try { await action() } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败，请重试。')
      // Native mutations may already have claimed a revision before a model
      // or connection fails. Reload it so retrying does not reuse a stale token.
      if (bookId) { try { await load() } catch { /* Preserve the original error. */ } }
    }
    finally { setBusy(false); setActivity('') }
  }

  function create() {
    if (!goal.trim()) return
    void run(async () => {
      const result = await bookApi.create({ user_intent: `${goal.trim()}${background.trim() ? `\n\n我的基础与要求：${background.trim()}` : ''}`, language: 'zh', fallback_language: 'zh', depth }, onEvent)
      router.replace(`/learning/goals?book=${encodeURIComponent(result.book.id)}`)
    }, '正在拟定学习方案…')
  }

  function confirmProposal(proposal: BookProposal) {
    if (!book) return
    void run(async () => { await bookApi.confirmProposal(book.id, proposal, book.revision, onEvent); await load() }, '正在生成章节目录…')
  }

  function generate(spine: Spine) {
    if (!book) return
    void run(async () => {
      await bookApi.confirmSpine(book.id, spine, true, book.revision, LEARNING_BLOCKS)
      await load()
    }, '正在安排逐章生成…')
  }

  function beginLearning() {
    if (!book) return
    void run(async () => {
      const textbook = bound || await importGoalBook(book.id)
      await refresh()
      router.push(textbook.links.learn)
    }, '正在关联阅读、导学、练习与笔记…')
  }

  return <main className="bm-goal-page">
    <div className="bm-goal-container">
      <Link className="bm-goal-back" href="/"><ArrowLeft size={16} />返回我的教材</Link>
      <div className="bm-page-intro"><span className="bm-eyebrow">LEARN FROM A GOAL</span><h1>{book?.title || '从一个目标，开始学习'}</h1><p>说明想学什么，由 AI 组织学习内容，生成一份可阅读、可讨论、可练习的学习资料。</p></div>
      <ol className="bm-goal-steps" aria-label="资料生成流程">{['学习方案', '章节目录', '逐章生成', '开始学习'].map((label, index) => <li key={label} className={index === step ? 'active' : index < step ? 'complete' : ''}><span>{index < step ? <Check size={14} /> : index + 1}</span>{label}</li>)}</ol>
      {error && <p role="alert" className="bm-error">{error}</p>}
      {busy && <p className="bm-goal-activity" role="status"><Loader2 size={17} className="animate-spin" />{activity}</p>}
      {loading ? <div className="bm-empty"><Loader2 size={20} className="animate-spin" />正在恢复生成记录…</div> : !bookId ? (
        <form className="bm-goal-card" onSubmit={event => { event.preventDefault(); create() }}>
          <label>想学习什么？<textarea aria-label="想学习什么？" required maxLength={4000} rows={4} value={goal} onChange={event => setGoal(event.target.value)} placeholder="例如：我想掌握数据结构中的二叉树，理解递归和遍历，并能用 C++ 实现。" disabled={busy} /></label>
          <label>已有基础与学习要求（可选）<textarea aria-label="已有基础与学习要求（可选）" maxLength={2000} rows={2} value={background} onChange={event => setBackground(event.target.value)} placeholder="例如：会 C++ 基础语法，但对指针不太熟。希望结合具体例子，先学原理再做题。" disabled={busy} /></label>
          <label>资料深度<select aria-label="资料深度" value={depth} onChange={event => setDepth(event.target.value as BookDepth)} disabled={busy}><option value="brief">精简入门 · 先建立框架</option><option value="standard">标准学习 · 讲解与示例</option><option value="deep">深入学习 · 更充分的推导与细节</option></select></label>
          <div className="bm-goal-actions"><p>先生成学习方案和目录，由你确认后再生成正文。</p><button className="bm-primary" type="submit" disabled={busy || !goal.trim()}><Sparkles size={16} />生成学习方案</button></div>
        </form>
      ) : book?.status === 'archived' ? <section className="bm-goal-card"><h2>生成记录已删除</h2><p>可以从新的学习目标开始，已加入书架的教材与学习记录仍保留。</p><Link className="bm-secondary" href="/">返回我的教材</Link></section>
        : book?.status === 'draft' && book.proposal ? <ProposalEditor key={`${book.id}.${book.revision}`} proposal={book.proposal} busy={busy} onConfirm={confirmProposal} />
        : book?.status === 'spine_ready' && detail?.spine ? <OutlineEditor key={`${book.id}.${book.revision}`} spine={detail.spine} busy={busy} onGenerate={generate} />
          : book && detail ? (
            <section className="bm-goal-card">
              <div className="bm-goal-generation-heading"><div><h2>{step === 3 ? '学习资料已生成' : book.status === 'paused' ? '生成已暂停' : '正在逐章生成学习资料'}</h2><p>{completed} / {detail.pages.length} 个章节已完成，可离开此页面，稍后从书架继续。</p></div><BookOpen size={28} /></div>
              <progress className="bm-goal-progress" aria-label="章节生成进度" max={detail.pages.length || 1} value={completed} />
              <ol className="bm-goal-pages">{[...detail.pages].sort((a, b) => a.order - b.order).map(page => <li key={page.id}><span>{page.title}</span><small>{STATUS[page.status] || page.status}</small></li>)}</ol>
              {book.status === 'paused' && <p className="bm-scope-hint">{book.metadata.pause_kind === 'user' ? '已完成的章节会保留，继续后生成剩余内容。' : '生成服务暂时未能完成后续内容。可检查 API 设置后继续。'}</p>}
              {failedPages.length > 0 && <p className="bm-error">{failedPages.length} 个章节尚未完整生成，可重试这些章节，已完成的章节会保留。</p>}
              <div className="bm-goal-actions">
                {step === 3 && <><a className="bm-secondary" href={bookApi.exportUrl(book.id)}><Download size={15} />下载 Markdown</a><button className="bm-primary" onClick={beginLearning} disabled={busy}>{bound ? '继续学习' : '加入教材并开始学习'}<ArrowRight size={16} /></button></>}
                {book.status === 'compiling' && <button className="bm-secondary" disabled={busy} onClick={() => void run(async () => { await bookApi.pause(book.id, book.revision); await load() }, '正在暂停生成…')}><Pause size={15} />暂停生成</button>}
                {book.status === 'paused' && <button className="bm-primary" disabled={busy} onClick={() => void run(async () => { await bookApi.resume(book.id, book.revision); await load() }, '正在继续生成…')}><ArrowRight size={15} />继续生成</button>}
                {failedPages.length > 0 && book.status !== 'compiling' && <button className="bm-secondary" disabled={busy} onClick={() => void run(async () => {
                  let revision = book.revision
                  for (const page of failedPages) { setActivity(`正在重试「${page.title}」…`); const result = await bookApi.compilePage(book.id, page.id, true, revision); revision = result.book_revision }
                  await load()
                }, '正在重试失败章节…')}><RotateCcw size={15} />重试未完成章节</button>}
              </div>
              {bound && <p className="bm-scope-hint">已关联同一份教材、学习路径和笔记本。重复打开会继续使用原有学习记录。</p>}
            </section>
          ) : !error && <p className="bm-empty">没有找到这份生成记录。</p>}
    </div>
  </main>
}

function ProposalEditor({ proposal, busy, onConfirm }: { proposal: BookProposal; busy: boolean; onConfirm: (proposal: BookProposal) => void }) {
  const [value, setValue] = useState(proposal)
  return <form className="bm-goal-card" onSubmit={event => { event.preventDefault(); onConfirm(value) }}>
    <h2>确认学习方案</h2><p className="bm-scope-hint">可以调整标题、范围、程度和章节数量，再让 AI 组织目录。</p>
    <fieldset disabled={busy}>
      <label>资料标题<input required value={value.title} onChange={event => setValue({ ...value, title: event.target.value })} /></label>
      <label>学习概述<textarea aria-label="学习概述" rows={2} value={value.description} onChange={event => setValue({ ...value, description: event.target.value })} /></label>
      <label>学习范围<textarea aria-label="学习范围" rows={3} value={value.scope} onChange={event => setValue({ ...value, scope: event.target.value })} /></label>
      <div className="bm-goal-fields"><label>适合程度<input value={value.target_level} onChange={event => setValue({ ...value, target_level: event.target.value })} /></label><label>预计章节数<input aria-label="预计章节数" type="number" min={1} max={20} value={value.estimated_chapters || 1} onChange={event => setValue({ ...value, estimated_chapters: Math.min(20, Math.max(1, Number(event.target.value) || 1)) })} /></label></div>
    </fieldset>
    <div className="bm-goal-actions"><button className="bm-primary" type="submit" disabled={busy || !value.title.trim()}>确认方案，生成目录<ArrowRight size={16} /></button></div>
  </form>
}

function OutlineEditor({ spine, busy, onGenerate }: { spine: Spine; busy: boolean; onGenerate: (spine: Spine) => void }) {
  const [chapters, setChapters] = useState(spine.chapters.filter(chapter => !chapter.auto_overview && !chapter.deep_dive))
  const update = (id: string, patch: Partial<Chapter>) => setChapters(previous => previous.map(chapter => chapter.id === id ? { ...chapter, ...patch } : chapter))
  function move(index: number, offset: number) { setChapters(previous => { const next = [...previous]; [next[index], next[index + offset]] = [next[index + offset], next[index]]; return next }) }
  return <form className="bm-goal-card" onSubmit={event => {
    event.preventDefault()
    const ids = new Set(chapters.map(chapter => chapter.id))
    onGenerate({ ...spine, chapters: chapters.map((chapter, order) => ({ ...chapter, order, prerequisites: chapter.prerequisites.filter(id => ids.has(id)) })) })
  }}>
    <h2>调整章节与学习目标</h2><p className="bm-scope-hint">按照你希望的顺序安排内容。正文、示例和练习由 Books 模块逐章生成。</p>
    <fieldset disabled={busy} className="bm-goal-outline">{chapters.map((chapter, index) => <article key={chapter.id}>
      <div className="bm-goal-chapter-heading"><strong>第 {index + 1} 章</strong><div><button type="button" aria-label={`上移第 ${index + 1} 章`} disabled={index === 0} onClick={() => move(index, -1)}><ArrowUp size={15} /></button><button type="button" aria-label={`下移第 ${index + 1} 章`} disabled={index === chapters.length - 1} onClick={() => move(index, 1)}><ArrowDown size={15} /></button><button type="button" aria-label={`删除第 ${index + 1} 章`} disabled={chapters.length === 1} onClick={() => setChapters(previous => previous.filter(item => item.id !== chapter.id))}><Trash2 size={15} /></button></div></div>
      <label>章节标题<input required value={chapter.title} onChange={event => update(chapter.id, { title: event.target.value })} /></label>
      <label>学习目标（每行一个）<textarea aria-label="学习目标（每行一个）" rows={3} value={chapter.learning_objectives.join('\n')} onChange={event => update(chapter.id, { learning_objectives: event.target.value.split('\n') })} /></label>
      <label>章节说明<textarea aria-label="章节说明" rows={2} value={chapter.summary} onChange={event => update(chapter.id, { summary: event.target.value })} /></label>
    </article>)}<button className="bm-secondary" type="button" onClick={() => setChapters(previous => [...previous, { id: 'ch_' + randomUuid().replaceAll('-', '').slice(0, 12), title: '', summary: '', learning_objectives: [], content_type: 'theory', source_anchors: [], prerequisites: [], page_ids: [], order: previous.length }])}><Plus size={15} />添加章节</button></fieldset>
    <div className="bm-goal-actions"><p>当前使用文字模型生成内容，图解仍可在学习时按需调用。</p><button className="bm-primary" type="submit" disabled={busy || chapters.length === 0}>确认目录，生成资料<Sparkles size={16} /></button></div>
  </form>
}
