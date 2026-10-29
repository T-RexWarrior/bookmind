'use client'

import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { usePathname } from 'next/navigation'
import { apiFetch } from '@/shared/api/client'

export type Objective = {
  id: string
  name: string
  module_id: string
  module_name: string
  start: number | null
  end: number | null
  evidence_count: number
  attempt_count: number
  mastery: number
  next_review: number | null
  level: number
  type?: string
  planning_status?: string
  mastery_requirement?: string
  source_section_id?: string
}
export type Textbook = {
  id: string
  material_id: string
  workspace_id: string
  path_id: string
  notebook_id: string
  title: string
  generated_book_id?: string
  learning_goal?: string
  note_count: number
  objectives: Objective[]
  sections?: { knowledge_point_id: string; name: string; start: number; end: number; module_id: string }[]
  learning_plans?: Record<string, { goal: string; prior_knowledge?: string }>
  current_objective_id: string
  locator: number
  unit: string
  unit_count: number
  links: { learn: string; guide: string; practice: string; notes: string }
}
const Context = createContext<{
  books: Textbook[]
  activeBook?: Textbook
  refresh: () => Promise<void>
  error: string
}>({ books: [], refresh: async () => {}, error: '' })

export function BookMindProvider({ children }: { children: React.ReactNode }) {
  const path = usePathname()
  const [books, setBooks] = useState<Textbook[]>([])
  const [error, setError] = useState('')
  const refresh = useCallback(async () => {
    try {
      const response = await apiFetch('/api/bookmind/books', { cache: 'no-store' })
      if (!response.ok) throw new Error('教材列表加载失败，请检查本机服务。')
      setBooks(await response.json())
      setError('')
    } catch (reason) {
      setError(String(reason))
    }
  }, [])
  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => {
      if (!document.hidden) void refresh()
    }, 10000)
    return () => window.clearInterval(timer)
  }, [refresh, path])
  const activeBook = books.find(book =>
    [book.workspace_id, book.path_id, book.notebook_id].some(id => path.split('/').includes(id))
  )
  return (
    <Context.Provider value={{ books, activeBook, refresh, error }}>{children}</Context.Provider>
  )
}

export const useBookMind = () => useContext(Context)

export const textbookUnitLabel = (unit: string) => ({ page: '页', chapter: '章', section: '节', slide: '页', segment: '段' }[unit] || '节')
