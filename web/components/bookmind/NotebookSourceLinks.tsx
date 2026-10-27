'use client'
import Link from 'next/link'
import { useBookMind } from './BookMindProvider'

export default function NotebookSourceLinks({ metadata }: { metadata?: Record<string, unknown> }) {
  const { books } = useBookMind()
  const source = metadata?.bookmind as
    { book_id?: string; knowledge_point_id?: string; locator?: number } | undefined
  const book = books.find(item => item.id === source?.book_id)
  if (!book) return null
  const point = book.objectives.find(item => item.id === source?.knowledge_point_id)
  const locator = source?.locator ?? point?.start
  return (
    <div className="bm-note-source">
      <span>关联知识点：{point?.name || '教材级笔记'}</span>
      <Link href={book.links.learn + (locator ? `?locator=${locator}` : '')}>
        返回教材{locator ? ` · 第 ${locator} 页/节` : ''}
      </Link>
      <Link href={book.links.practice}>查看学习与复习记录</Link>
    </div>
  )
}
