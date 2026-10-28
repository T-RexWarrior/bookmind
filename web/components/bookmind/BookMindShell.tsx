'use client'

import Link from 'next/link'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { Suspense, useEffect, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { BookOpen, GraduationCap, Library, NotebookPen, Settings, Sprout, ListChecks } from 'lucide-react'
import { useBookMind } from './BookMindProvider'
import { useSidebarDrawer } from '@/components/layout/AppShell'
import TextbookSourceDrawer from './TextbookSourceDrawer'

export function BookMindSidebar() {
  const { books, activeBook } = useBookMind()
  const drawer = useSidebarDrawer()
  return (
    <aside className="bm-sidebar">
      <Link href="/" className="bm-logo" onClick={() => drawer?.close()}>
        <span className="bm-symbol">
          <Sprout size={23} />
        </span>
        <span>
          BookMind<small>学迹 · AI 学习助手</small>
        </span>
      </Link>
      <Link className="bm-library-link" href="/" onClick={() => drawer?.close()}>
        <Library size={17} /> 我的教材
      </Link>
      <p className="bm-eyebrow">围绕同一本书，持续学习</p>
      <div className="bm-book-list">
        {books.map(book => (
          <Link
            key={book.id}
            href={book.links.learn}
            onClick={() => drawer?.close()}
            className={activeBook?.id === book.id ? 'active' : ''}
          >
            <BookOpen size={16} />
            <span>{book.title}</span>
          </Link>
        ))}
      </div>
      <div className="bm-sidebar-bottom">
        <span>
          阅读留下疑问
          <br />
          练习验证理解
          <br />
          笔记连接知识
        </span>
        <Link href="/api-settings" onClick={() => drawer?.close()}>
          <Settings size={17} /> API 设置
        </Link>
      </div>
    </aside>
  )
}

export function BookMindFrame({ children }: { children: React.ReactNode }) {
  return (
    <Suspense>
      <BookMindFrameInner>{children}</BookMindFrameInner>
    </Suspense>
  )
}

function BookMindFrameInner({ children }: { children: React.ReactNode }) {
  const { activeBook: book } = useBookMind()
  const path = usePathname()
  const router = useRouter()
  const params = useSearchParams()
  const focusedId = params.get('objective')
  const query = params.toString()
  const [readerPosition, setReaderPosition] = useState({ materialId: '', locator: 0 })
  useEffect(() => {
    const update = (event: Event) => {
      const detail = (event as CustomEvent<{ materialId: string; locator: number }>).detail
      setReaderPosition(detail)
    }
    window.addEventListener('bookmind-reader-position', update)
    return () => window.removeEventListener('bookmind-reader-position', update)
  }, [])
  const position = (readerPosition.materialId === book?.material_id ? readerPosition.locator : 0) || book?.locator || 1
  const isGuiding = Boolean(book && path.startsWith(book.links.guide) && !path.endsWith('/mistakes'))
  const isPracticing = Boolean(book && (path.startsWith(book.links.practice) || path.endsWith('/mistakes')))
  const isLearning = Boolean(book && (path.startsWith(book.links.learn) || isGuiding))
  useEffect(() => {
    if (!book || !isLearning) return
    const mode = isGuiding ? 'guide' : 'learn'
    try {
      sessionStorage.setItem(`bookmind.learning-mode.${book.id}`, mode)
      sessionStorage.setItem(`bookmind.learning-route.${book.id}.${mode}`, path + (query ? `?${query}` : ''))
    } catch { /* Navigation still works when browser storage is unavailable. */ }
  }, [book?.id, isLearning, isGuiding, path, query])
  function resumeLearning(event: ReactMouseEvent<HTMLAnchorElement>, requestedMode?: 'learn' | 'guide') {
    if (!book || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    try {
      const mode = requestedMode || (sessionStorage.getItem(`bookmind.learning-mode.${book.id}`) === 'guide' ? 'guide' : 'learn')
      const saved = sessionStorage.getItem(`bookmind.learning-route.${book.id}.${mode}`)
      if (!saved) return
      const target = new URL(saved, window.location.origin)
      const base = book.links[mode]
      if (target.origin !== window.location.origin || !(target.pathname === base || target.pathname.startsWith(base + '/'))) return
      event.preventDefault()
      router.push(target.pathname + target.search)
    } catch { /* Fall back to the link's ordinary destination. */ }
  }
  const point =
    book &&
    (book.objectives.find(p => p.id === focusedId) ||
      (isGuiding ? book.objectives.find(p => p.id === book.current_objective_id) : undefined) ||
      [...book.objectives]
        .filter(p => p.start !== null && p.end !== null && p.start <= position && position <= p.end)
        .sort((a, b) => (b.start || 0) - (a.start || 0) || b.level - a.level)[0] ||
      book.objectives.find(p => p.id === book.current_objective_id))
  return (
    <div className="bm-frame">
      {book && (
        <header className="bm-context-header">
          <div className="bm-context-title">
            <BookOpen size={16} />
            <strong title={book.title}>{book.title}</strong>
            <span>
              {book.note_count} 条笔记
            </span>
          </div>
        </header>
      )}
      {book && (
        <div className="bm-workspace-toolbar">
          <nav className="bm-page-nav" aria-label="教材页面">
            {[
              { key: 'learn', label: '自主学习', icon: BookOpen },
              { key: 'guide', label: 'AI 导学', icon: GraduationCap },
              { key: 'practice', label: '练习巩固', icon: ListChecks },
              { key: 'notes', label: '学习笔记', icon: NotebookPen },
            ].map(tab => (
              <Link
                key={tab.key}
                href={
                  book.links[tab.key as keyof typeof book.links] +
                  (point
                    ? tab.key === 'learn'
                      ? `?locator=${point.start || position}`
                      : `?objective=${point.id}`
                    : '')
                }
                className={(tab.key === 'learn' ? isLearning && !isGuiding : tab.key === 'guide' ? isGuiding : tab.key === 'practice' ? isPracticing : path.startsWith(book.links.notes)) ? 'active' : ''}
                aria-current={(tab.key === 'learn' ? isLearning && !isGuiding : tab.key === 'guide' ? isGuiding : tab.key === 'practice' ? isPracticing : path.startsWith(book.links.notes)) ? 'page' : undefined}
                onClick={tab.key === 'learn' || tab.key === 'guide' ? event => resumeLearning(event, tab.key as 'learn' | 'guide') : undefined}
              >
                <tab.icon size={15} />
                {tab.label}
              </Link>
            ))}
          </nav>
          {isLearning && point && <div className="bm-learning-focus"><span title={point.name}>当前：<strong>{point.name}</strong></span>
            {isGuiding && point.start && <button type="button" onClick={() => window.dispatchEvent(new CustomEvent('bookmind-source-open', { detail: { materialId: book.material_id, locator: point.start } }))}>查看教材原文</button>}
          </div>}
        </div>
      )}
      <div className="bm-frame-content">{children}</div>
      {book && isGuiding && <TextbookSourceDrawer key={book.id} book={book} />}
    </div>
  )
}
