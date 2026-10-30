import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import GoalDrafts from '@/components/bookmind/GoalDrafts'

const { list, apiFetch, library } = vi.hoisted(() => ({ list: vi.fn(), apiFetch: vi.fn(), library: { loaded: true } }))
vi.mock('@/lib/book-api', () => ({ bookApi: { list } }))
vi.mock('@/shared/api/client', () => ({ apiFetch }))
vi.mock('next/link', () => ({ default: ({ href, children, ...props }: any) => <a href={href} {...props}>{children}</a> }))
vi.mock('@/components/bookmind/BookMindProvider', () => ({ useBookMind: () => ({
  loaded: library.loaded,
  books: [{ generated_book_id: 'imported' }], removedBooks: [{ generated_book_id: 'removed-material' }],
}) }))

describe('goal generation record management', () => {
  beforeEach(() => {
    list.mockReset(); apiFetch.mockReset()
    library.loaded = true
    list.mockResolvedValue({ books: [
      { id: 'pending-1', title: '待确认方案', status: 'draft' },
      { id: 'pending-2', title: '暂停的生成', status: 'paused' },
      { id: 'imported', title: '已有教材', status: 'ready' },
      { id: 'removed-material', title: '已移除教材', status: 'ready' },
      { id: 'archived', title: '已清理记录', status: 'archived' },
    ] })
  })
  afterEach(cleanup)
  it('only lists independent generation records', async () => {
    render(<GoalDrafts />)
    await screen.findByText('待确认方案')
    expect(screen.queryByText('已有教材')).toBeNull()
    expect(screen.queryByText('已移除教材')).toBeNull()
    expect(screen.queryByText('已清理记录')).toBeNull()
  })
  it('waits for textbook bindings before exposing generation deletion', async () => {
    library.loaded = false
    const { rerender } = render(<GoalDrafts />)
    await waitFor(() => expect(list).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: '清空记录' })).toBeNull()
    library.loaded = true
    rerender(<GoalDrafts />)
    expect(await screen.findByRole('button', { name: '清空记录' })).toBeTruthy()
    expect(screen.queryByText('已有教材')).toBeNull()
  })
  it('clears only the reviewed set after confirmation', async () => {
    apiFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ removed: ['pending-1', 'pending-2'] }) })
    render(<GoalDrafts />)
    fireEvent.click(await screen.findByRole('button', { name: '清空记录' }))
    expect(screen.getByRole('alertdialog')).toHaveTextContent('已加入书架的教材、笔记和学习记录不受影响')
    expect(apiFetch).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认清理' }))
    await waitFor(() => expect(screen.queryByText('从目标开始的学习')).toBeNull())
    expect(JSON.parse(apiFetch.mock.calls[0][1].body)).toEqual({ book_ids: ['pending-1', 'pending-2'] })
  })
  it('can remove one record while retaining other drafts', async () => {
    apiFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ removed: ['pending-1'] }) })
    render(<GoalDrafts />)
    fireEvent.click(await screen.findByRole('button', { name: '删除生成记录《待确认方案》' }))
    fireEvent.click(screen.getByRole('button', { name: '确认清理' }))
    await waitFor(() => expect(screen.queryByText('待确认方案')).toBeNull())
    expect(screen.getByText('暂停的生成')).toBeTruthy()
  })
  it('keeps records visible when the server rejects a clear request', async () => {
    apiFetch.mockResolvedValueOnce({ ok: false })
    render(<GoalDrafts />)
    fireEvent.click(await screen.findByRole('button', { name: '清空记录' }))
    fireEvent.click(screen.getByRole('button', { name: '确认清理' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('清理失败')
    expect(screen.getByText('待确认方案')).toBeTruthy()
  })
})
