import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Bookshelf from '@/components/bookmind/Bookshelf'

const { apiFetch, refresh, library } = vi.hoisted(() => ({
  apiFetch: vi.fn(), refresh: vi.fn(),
  library: { books: [{ id: 'bm_demo', title: '测试教材', objectives: [], note_count: 2,
    links: { learn: '/read', practice: '/practice', notes: '/notes' } }],
    removedBooks: [{ id: 'bm_removed', title: '已移除示例', removed_at: 1 }], error: '' },
}))
vi.mock('@/shared/api/client', () => ({ apiFetch }))
vi.mock('@/lib/reading-api', () => ({ uploadMaterial: vi.fn() }))
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('next/link', () => ({ default: ({ href, children, ...props }: any) => <a href={href} {...props}>{children}</a> }))
vi.mock('@/components/bookmind/BookMindProvider', () => ({ useBookMind: () => ({ ...library, refresh }) }))
vi.mock('@/components/bookmind/GoalDrafts', () => ({ default: () => null }))

describe('textbook shelf removal and restore', () => {
  beforeEach(() => { apiFetch.mockReset(); refresh.mockReset() })
  afterEach(cleanup)
  it('requires an explicit confirmation and explains preserved records', async () => {
    render(<Bookshelf />)
    fireEvent.click(screen.getByRole('button', { name: '删除教材《测试教材》' }))
    expect(screen.getByRole('alertdialog')).toHaveTextContent('笔记、对话和练习记录会保留')
    expect(apiFetch).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(apiFetch).not.toHaveBeenCalled()
  })
  it('removes only the selected book, then refreshes the library', async () => {
    apiFetch.mockResolvedValueOnce({ ok: true })
    render(<Bookshelf />)
    fireEvent.click(screen.getByRole('button', { name: '删除教材《测试教材》' }))
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce())
    expect(apiFetch).toHaveBeenCalledWith('/api/bookmind/books/bm_demo', { method: 'DELETE' })
  })
  it('keeps the confirmation open on server failure', async () => {
    apiFetch.mockResolvedValueOnce({ ok: false })
    render(<Bookshelf />)
    fireEvent.click(screen.getByRole('button', { name: '删除教材《测试教材》' }))
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('删除教材失败')
    expect(screen.getByRole('alertdialog')).toBeTruthy()
    expect(refresh).not.toHaveBeenCalled()
  })
  it('restores the original textbook identity', async () => {
    apiFetch.mockResolvedValueOnce({ ok: true })
    render(<Bookshelf />)
    fireEvent.click(screen.getByText('已删除的教材（1）'))
    fireEvent.click(screen.getByRole('button', { name: '恢复教材' }))
    await waitFor(() => expect(refresh).toHaveBeenCalledOnce())
    expect(apiFetch).toHaveBeenCalledWith('/api/bookmind/books/bm_removed/restore', { method: 'POST' })
  })
})
