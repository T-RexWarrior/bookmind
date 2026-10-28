'use client'

import Link from 'next/link'
import { useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { ArrowRight, BookOpen, FileUp, GraduationCap, Loader2, NotebookPen } from 'lucide-react'
import { apiFetch } from '@/shared/api/client'
import { uploadMaterial } from '@/lib/reading-api'
import { Textbook, useBookMind } from './BookMindProvider'

export default function Bookshelf() {
  const { books, refresh, error } = useBookMind()
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState('')
  const input = useRef<HTMLInputElement>(null)
  const router = useRouter()
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
        <button className="bm-primary" onClick={() => input.current?.click()} disabled={busy}>
          {busy ? <Loader2 className="animate-spin" size={17} /> : <FileUp size={17} />}
          {busy ? '正在导入教材与目录…' : '导入一份学习资料'}
        </button>
        <input
          hidden
          ref={input}
          type="file"
          accept=".pdf,.md,.txt,.epub,.docx,.pptx"
          onChange={e => void upload(e.target.files?.[0])}
        />
        <small>PDF、Markdown、TXT、EPUB 等 · 使用本机文字模型 API</small>
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
      <section>
        <div className="bm-section-heading">
          <h2>继续学习</h2>
          <span>{books.length} 本教材</span>
        </div>
        {books.length === 0 ? (
          <div className="bm-empty">
            <BookOpen size={30} />
            <p>上传教材后，三个页面会自动关联到同一份资料与学习目标。</p>
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
                  <h3>{book.title}</h3>
                  <p>
                    {book.objectives.length} 个目录学习目标 · {book.note_count} 条笔记
                  </p>
                  <span className="bm-status">
                    {book.objectives.reduce((sum, p) => sum + p.evidence_count, 0)} 条作答证据
                  </span>
                  <div>
                    <Link href={book.links.learn}>
                      继续阅读 <ArrowRight size={15} />
                    </Link>
                    <Link href={book.links.practice}>练习</Link>
                    <Link href={book.links.notes}>笔记</Link>
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </main>
  )
}
