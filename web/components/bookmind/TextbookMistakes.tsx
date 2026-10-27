'use client'
import { useState } from 'react'
import { useParams } from 'next/navigation'
import QuestionCard from '@/components/space/question-bank/QuestionCard'
import { useQuestionBank } from '@/components/space/question-bank/useQuestionBank'
import { PracticeSession } from '@/components/learning/practice/PracticeSession'
import { useBookMind } from './BookMindProvider'

export default function TextbookMistakes() {
  const pathId = String(useParams<{ pathId: string }>().pathId)
  const bank = useQuestionBank({ masteryPathId: pathId, mistakesOnly: true })
  const [session, setSession] = useState<number[] | null>(null)
  const { activeBook: book } = useBookMind()
  if (session)
    return (
      <div className="h-full overflow-y-auto p-6">
        <PracticeSession
          ids={session}
          onClose={() => {
            setSession(null)
            void bank.refresh()
          }}
          backLabel="返回本书错题"
        />
      </div>
    )
  return (
    <main className="h-full overflow-y-auto p-6">
      <div className="mb-5 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">本书错题</h1>
          <p className="mt-2 text-xs text-[var(--muted-foreground)]">
            随堂小测与 Mastery Path 的错题共用这份记录 · {bank.total} 道
          </p>
        </div>
        <button
          className="bm-primary"
          disabled={!bank.items.length || bank.loading}
          onClick={() =>
            setSession(
              bank.selectedIds.size ? [...bank.selectedIds] : bank.items.map(item => item.id)
            )
          }
        >
          练习当前页错题
        </button>
      </div>
      {bank.error && (
        <p role="alert" className="bm-error">
          {bank.error}
        </p>
      )}
      {bank.loading ? (
        <p>正在读取作答记录…</p>
      ) : !bank.items.length ? (
        <div className="bm-empty">
          本书还没有错题。阅读时的小测和学习路径里的作答会自动归入这里。
        </div>
      ) : (
        <div className="grid gap-4">
          {bank.items.map(entry => (
            <div key={entry.id}>
              {book && entry.section_id && (
                <div className="mb-1 text-xs">
                  <a
                    className="text-emerald-700"
                    href={`${book.links.learn}?locator=${entry.section_id}`}
                  >
                    回到这道题的教材原文 ↗
                  </a>
                </div>
              )}
              <QuestionCard
                entry={entry}
                mistakesOnly
                categories={bank.categories}
                selected={bank.selectedIds.has(entry.id)}
                disabled={bank.pendingIds.has(entry.id)}
                onToggleSelected={() => bank.toggleSelected(entry.id)}
                onToggleBookmark={() => void bank.toggleBookmark(entry)}
                onToggleResolved={() => void bank.toggleResolved(entry)}
                onDelete={() => void bank.removeEntry(entry)}
                onFile={category => bank.fileEntries([entry.id], category)}
                onUnfile={category => bank.unfileEntries([entry.id], category)}
                onCreateAndFile={name => bank.fileIntoNewCategory([entry.id], name)}
              />
            </div>
          ))}
        </div>
      )}
      <div className="mt-5 flex items-center gap-4 text-xs">
        <button disabled={bank.page === 0} onClick={() => bank.setPage(bank.page - 1)}>
          上一页
        </button>
        <span>第 {bank.page + 1} 页</span>
        <button
          disabled={(bank.page + 1) * 60 >= bank.total}
          onClick={() => bank.setPage(bank.page + 1)}
        >
          下一页
        </button>
      </div>
    </main>
  )
}
