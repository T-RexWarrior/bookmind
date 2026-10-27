'use client'

import Link from 'next/link'
import { usePathname, useSearchParams } from 'next/navigation'
import { Suspense, useEffect, useState } from 'react'
import { BookOpen, GraduationCap, Library, NotebookPen, Settings, Sprout } from 'lucide-react'
import { useBookMind } from './BookMindProvider'
import { useSidebarDrawer } from '@/components/layout/AppShell'

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
  const focusedId = useSearchParams().get('objective')
  const [locator, setLocator] = useState(0)
  useEffect(() => {
    const update = (event: Event) =>
      setLocator((event as CustomEvent<{ locator: number }>).detail.locator)
    window.addEventListener('bookmind-reader-position', update)
    return () => window.removeEventListener('bookmind-reader-position', update)
  }, [])
  const position = locator || book?.locator || 1
  const isPracticing = Boolean(book && path.startsWith(book.links.practice))
  const point =
    book &&
    (book.objectives.find(p => p.id === focusedId) ||
      (isPracticing ? book.objectives.find(p => p.id === book.current_objective_id) : undefined) ||
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
              {book.objectives.length} 个学习目标 · {book.note_count} 条笔记
            </span>
          </div>
          <nav aria-label="教材学习方式">
            {[
              { key: 'learn', label: '学习', icon: BookOpen },
              { key: 'practice', label: '练习 · Mastery Path', icon: GraduationCap },
              { key: 'notes', label: '笔记', icon: NotebookPen },
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
                className={
                  path.startsWith(book.links[tab.key as keyof typeof book.links]) ? 'active' : ''
                }
              >
                <tab.icon size={15} />
                {tab.label}
              </Link>
            ))}
          </nav>
        </header>
      )}
      {book && point && (
        <div className="bm-knowledge-strip">
          <span>
            共同学习目标：<strong>{point.name}</strong>
          </span>
          <span>
            {point.evidence_count} 条作答证据
            {point.next_review
              ? ` · 下次复习 ${new Date(point.next_review * 1000).toLocaleDateString('zh-CN')}`
              : ' · 尚无复习安排'}
          </span>
          {point.start && (
            <Link href={`${book.links.learn}?locator=${point.start}`}>对应原文 ↗</Link>
          )}
        </div>
      )}
      {book && path.startsWith(book.links.practice) && (
        <nav className="bm-practice-tabs" aria-label="练习内容">
          <Link
            href={book.links.practice + (point ? `?objective=${point.id}` : '')}
            className={!path.endsWith('/mistakes') ? 'active' : ''}
          >
            学习路径与到期复习
          </Link>
          <Link
            href={`${book.links.practice}/mistakes`}
            className={path.endsWith('/mistakes') ? 'active' : ''}
          >
            本书错题
          </Link>
        </nav>
      )}
      <div className="bm-frame-content">{children}</div>
    </div>
  )
}
