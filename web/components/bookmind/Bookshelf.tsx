'use client'

import Link from 'next/link'
import { useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { ArrowRight, BookOpen, FileUp, GraduationCap, Loader2, NotebookPen, RotateCcw, Sparkles, Trash2 } from 'lucide-react'
import { Dialog } from '@/shared/ui/Dialog'
import { apiFetch } from '@/shared/api/client'
import { uploadMaterial } from '@/lib/reading-api'
import { Textbook, useBookMind } from './BookMindProvider'
import GoalDrafts from './GoalDrafts'

export default function Bookshelf() {
  const { books, removedBooks, refresh, error } = useBookMind()
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState('')
  const [removing, setRemoving] = useState<Textbook | null>(null)
  const [changing, setChanging] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const router = useRouter()
  async function changeBook(id: string, restore = false) {
    if (changing) return
    setChanging(id); setFailure('')
    try {
      const response = await apiFetch(`/api/bookmind/books/${encodeURIComponent(id)}${restore ? '/restore' : ''}`, { method: restore ? 'POST' : 'DELETE' })
      if (!response.ok) throw new Error(restore ? '恢复教材失败，请重试。' : '删除教材失败，请重试。')
      setRemoving(null)
      await refresh()
    } catch (reason) { setFailure(reason instanceof Error ? reason.message : String(reason)) }
    finally { setChanging('') }
  }
  async function upload(file?: File) {
    if (!file || busy) return
    setBusy(true)
    setFailure('')
    try {
      const material = await uploadMaterial(file)
      const response = await apiFetch('/api/bookmind/books', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ material_id: material.material_id }),
      })
      if (!response.ok) throw new Error('教材已经上传，关联学习路径失败。请重试或检查服务日志。')
      const book: Textbook = await response.json()
      await refresh()
      router.push(book.links.learn)
    } catch (reason) {
      setFailure(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
      if (input.current) input.current.value = ''
    }
  }
  return (
    <main className="bm-bookshelf">
      <div className="bm-home-intro">
        <p className="bm-eyebrow">READ · UNDERSTAND · REMEMBER</p>
        <h1>一本教材，一条学习主线。</h1>
        <p>在原文里读懂，在练习里验证，把理解留在自己的笔记里。</p>
        <div className="bm-start-choices">
          <section><h2><FileUp size={18} />已有资料</h2><p>导入教材、讲义或文档，围绕同一份资料阅读、讨论和练习。</p>
        <button className="bm-primary" onClick={() => input.current?.click()} disabled={busy}>
          {busy ? <Loader2 className="animate-spin" size={17} /> : <FileUp size={17} />}
          {busy ? '正在导入教材与目录…' : '导入一份学习资料'}
        </button>
        <small>PDF、Markdown、TXT、EPUB 等</small></section>
          <section><h2><Sparkles size={18} />已有目标</h2><p>说明想学什么，由 AI 组织学习内容，生成一份可阅读、可讨论、可练习的学习资料。</p><Link className="bm-secondary" href="/learning/goals"><Sparkles size={16} />从目标生成学习资料</Link><small>拟定方案 · 确认目录 · 逐章生成</small></section>
        </div>
        <input
          hidden
          ref={input}
          type="file"
          accept=".pdf,.md,.txt,.epub,.docx,.pptx"
          onChange={e => void upload(e.target.files?.[0])}
        />
      </div>
      <div className="bm-three-steps">
        {[
          [BookOpen, '学习', '自主阅读与探索 · AI 导学循序推进'],
          [GraduationCap, '练习', '知识点自测 · 错题巩固 · 到期复习'],
          [NotebookPen, '笔记', '摘录 · 总结 · 关联图解 · 返回原文'],
        ].map(([Icon, title, description]) => {
          const Glyph = Icon as typeof BookOpen
          return (
            <div key={String(title)}>
              <Glyph size={21} />
              <strong>{String(title)}</strong>
              <span>{String(description)}</span>
            </div>
          )
        })}
      </div>
      {(failure || error) && (
        <div role="alert" className="bm-error">
          {failure || error}
        </div>
      )}
      <GoalDrafts />
      <section>
        <div className="bm-section-heading">
          <h2>继续学习</h2>
          <span>{books.length} 本教材</span>
        </div>
        {books.length === 0 ? (
          <div className="bm-empty">
            <BookOpen size={30} />
            <p>导入资料或从目标生成资料后，四个学习页面会关联同一份内容与学习目标。</p>
          </div>
        ) : (
          <div className="bm-book-grid">
            {books.map(book => (
              <article key={book.id}>
                <div className="bm-book-cover">
                  <BookOpen size={34} />
                  <span>BookMind</span>
                </div>
                <div className="bm-book-body">
                  <div className="bm-book-title"><h3>{book.title}</h3><button type="button" className="bm-delete-book" aria-label={`删除教材《${book.title}》`} title="删除教材" onClick={() => setRemoving(book)} disabled={Boolean(changing)}><Trash2 size={16} /></button></div>
                  <p>
                    {book.objectives.length} 个目录学习目标 · {book.note_count} 条笔记
                  </p>
                  <span className="bm-status">{book.generated_book_id && 'AI 生成 · '}
                    {book.objectives.reduce((sum, p) => sum + p.evidence_count, 0)} 条作答证据
                  </span>
                  <div>
                    <Link href={book.links.learn}>
                      继续阅读 <ArrowRight size={15} />
                    </Link>
                    <Link href={book.links.practice}>练习巩固</Link>
                    <Link href={book.links.notes}>学习笔记</Link>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
      {removedBooks.length > 0 && <details className="bm-removed-books"><summary>已删除的教材（{removedBooks.length}）</summary><p>学习记录与笔记已保留，可随时恢复。</p>{removedBooks.map(book => <div key={book.id}><span>{book.title}</span><button className="bm-secondary" disabled={Boolean(changing)} onClick={() => void changeBook(book.id, true)}><RotateCcw size={14} />恢复教材</button></div>)}</details>}
      <Dialog open={Boolean(removing)} title="删除这本教材？" onClose={() => setRemoving(null)} busy={Boolean(changing)} alert size="sm" closeLabel="关闭" footer={<><button className="bm-secondary" disabled={Boolean(changing)} onClick={() => setRemoving(null)} data-autofocus>取消</button><button className="bm-primary" disabled={Boolean(changing)} onClick={() => removing && void changeBook(removing.id)}>{changing ? '正在删除…' : '确认删除'}</button></>}>
        <p>《{removing?.title}》将从书架移除，笔记、对话和练习记录会保留。可在书架底部恢复，重新导入同一文件也会恢复。电脑中的原始文件不会删除。</p>
      </Dialog>
    </main>
  )
}
