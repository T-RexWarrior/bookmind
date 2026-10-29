'use client'

import Link from 'next/link'
import { useEffect, useState } from 'react'
import { ArrowRight, Loader2, Sparkles } from 'lucide-react'
import { bookApi } from '@/lib/book-api'
import type { Book } from '@/lib/book-types'
import { useBookMind } from './BookMindProvider'

const LABELS: Record<string, string> = { draft: '确认学习方案', spine_ready: '确认章节目录', compiling: '正在生成资料', paused: '生成已暂停', ready: '已生成，开始学习', error: '查看生成记录' }

export default function GoalDrafts() {
  const { books, removedBooks } = useBookMind()
  const [drafts, setDrafts] = useState<Book[]>([])
  const [error, setError] = useState('')
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
  const pending = drafts.filter(draft => draft.status !== 'archived' && ![...books, ...removedBooks].some(book => book.generated_book_id === draft.id))
  if (!pending.length && !error) return null
  return <section className="bm-goal-drafts" aria-label="目标学习生成记录">
    <div className="bm-section-heading"><h2>从目标开始的学习</h2><span>方案与生成记录会保留</span></div>
    {error && <p role="alert" className="bm-error">{error}</p>}
    <div className="bm-goal-draft-list">{pending.map(draft => <Link href={`/learning/goals?book=${encodeURIComponent(draft.id)}`} key={draft.id}>
      {draft.status === 'compiling' ? <Loader2 size={19} className="animate-spin" /> : <Sparkles size={19} />}<div><strong>{draft.title}</strong><small>{LABELS[draft.status] || '查看生成记录'}</small></div><ArrowRight size={16} />
    </Link>)}</div>
  </section>
}
