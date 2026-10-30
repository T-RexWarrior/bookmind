'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { ArrowRight, Loader2, Sparkles, Trash2 } from 'lucide-react'
import { bookApi } from '@/lib/book-api'
import { apiFetch } from '@/shared/api/client'
import { Dialog } from '@/shared/ui/Dialog'
import type { Book } from '@/lib/book-types'
import { useBookMind } from './BookMindProvider'

const LABELS: Record<string, string> = { draft: '确认学习方案', spine_ready: '确认章节目录', compiling: '正在生成资料', paused: '生成已暂停', ready: '已生成，开始学习', error: '查看生成记录' }

export default function GoalDrafts() {
  const { books, removedBooks, loaded } = useBookMind()
  const [drafts, setDrafts] = useState<Book[]>([])
  const [error, setError] = useState('')
  const [removing, setRemoving] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const load = async () => {
      try {
        const result = await bookApi.list()
        if (active) { setDrafts(result.books); setError('') }
      } catch { if (active) setError('目标资料的生成记录暂时无法读取，请刷新重试。') }
      if (active) timer = setTimeout(load, 10000)
    }
    void load()
    return () => { active = false; clearTimeout(timer) }
  }, [])
  const pending = loaded ? drafts.filter(draft => draft.status !== 'archived' && ![...books, ...removedBooks].some(book => book.generated_book_id === draft.id)) : []
  async function removeRecords() {
    if (busy || !removing.length) return
    setBusy(true); setError('')
    try {
      const response = await apiFetch('/api/bookmind/generated/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ book_ids: removing }) })
      if (!response.ok) throw new Error('生成记录清理失败，请重试。')
      const result: { removed: string[] } = await response.json()
      setDrafts(current => current.filter(draft => !result.removed.includes(draft.id)))
      setRemoving([])
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)) }
    finally { setBusy(false) }
  }
  if (!pending.length && !error) return null
  return <section className="bm-goal-drafts" aria-label="目标学习生成记录">
    <div className="bm-section-heading"><h2>从目标开始的学习</h2>{pending.length > 0 && <button className="bm-secondary" disabled={busy} onClick={() => { setError(''); setRemoving(pending.map(draft => draft.id)) }}><Trash2 size={14} />清空记录</button>}</div>
    {error && !removing.length && <p role="alert" className="bm-error">{error}</p>}
    <div className="bm-goal-draft-list">{pending.map(draft => <article className="bm-goal-draft-card" key={draft.id}><Link href={`/learning/goals?book=${encodeURIComponent(draft.id)}`}>
      {draft.status === 'compiling' ? <Loader2 size={19} className="animate-spin" /> : <Sparkles size={19} />}<div><strong>{draft.title}</strong><small>{LABELS[draft.status] || '查看生成记录'}</small></div><ArrowRight size={16} />
    </Link><button className="bm-delete-book" aria-label={`删除生成记录《${draft.title}》`} disabled={busy} onClick={() => { setError(''); setRemoving([draft.id]) }}><Trash2 size={15} /></button></article>)}</div>
    <Dialog open={removing.length > 0} title={removing.length > 1 ? '清空这些生成记录？' : '删除这条生成记录？'} onClose={() => setRemoving([])} busy={busy} alert size="sm" closeLabel="关闭" footer={<><button className="bm-secondary" disabled={busy} data-autofocus onClick={() => setRemoving([])}>取消</button><button className="bm-primary" disabled={busy} onClick={() => void removeRecords()}>{busy ? '正在清理…' : '确认清理'}</button></>}>
      <p>将移除 {removing.length} 条方案或生成记录，并停止尚未完成的生成。已加入书架的教材、笔记和学习记录不受影响。</p>{error && <p role="alert" className="bm-error">{error}</p>}
    </Dialog>
  </section>
}
