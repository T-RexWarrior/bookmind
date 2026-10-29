import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LearningPlan } from '@/components/bookmind/LearningPlan'
import { knowledgeTypeLabel } from '@/components/space/learning/format'

const { apiFetch, refresh, book } = vi.hoisted(() => ({
  apiFetch: vi.fn(), refresh: vi.fn(),
  book: { id: 'bm_demo', path_id: 'path_demo', sections: [{ knowledge_point_id: 'section_1', name: '中序遍历', start: 1, end: 3, module_id: 'm1' }],
    objectives: [{ id: 'section_1', name: '中序遍历', source_section_id: 'section_1', planning_status: 'outline', type: 'concept', mastery_requirement: '' }] },
}))
vi.mock('@/shared/api/client', () => ({ apiFetch }))
vi.mock('@/components/bookmind/BookMindProvider', () => ({ useBookMind: () => ({ books: [book], refresh }) }))
const plan = { id: 'plan_demo', section_name: '中序遍历', source_locators: [2], goals: [
  { name: '解释中序遍历的访问顺序', type: 'concept', mastery_requirement: '独立说明左、根、右的访问顺序', source_locators: [2] },
  { name: '推演新二叉树的遍历结果', type: 'procedure', mastery_requirement: '给出新二叉树时，独立写出遍历结果', source_locators: [2] },
] }
describe('textbook learning plan', () => {
  beforeEach(() => { apiFetch.mockReset(); refresh.mockReset(); book.objectives[0].planning_status = 'outline' })
  afterEach(cleanup)
  it('generates a grounded preview and adopts the stored plan without sending editable goals', async () => {
    apiFetch.mockResolvedValueOnce({ ok: true, json: async () => plan })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ objective_ids: ['objective_1', 'objective_2'] }) })
    const onApplied = vi.fn()
    render(<LearningPlan pathId="path_demo" objectiveId="section_1" onApplied={onApplied} />)
    expect(apiFetch).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('学习目的'), { target: { value: '能独立实现遍历算法' } })
    fireEvent.click(screen.getByRole('button', { name: '生成学习目标' }))
    await screen.findByText('操作与解题')
    expect(screen.getByText('给出新二叉树时，独立写出遍历结果')).toBeTruthy()
    expect(JSON.parse(apiFetch.mock.calls[0][1].body).goal).toBe('能独立实现遍历算法')
    fireEvent.click(screen.getByRole('button', { name: '采用学习目标' }))
    await waitFor(() => expect(onApplied).toHaveBeenCalledWith('objective_1'))
    expect(JSON.parse(apiFetch.mock.calls[1][1].body)).toEqual({ plan_id: 'plan_demo' })
    expect(refresh).toHaveBeenCalled()
  })
  it('shows generation failures and leaves the current curriculum intact', async () => {
    apiFetch.mockResolvedValueOnce({ ok: false, json: async () => ({ detail: '未读到可用原文，请重试' }) })
    const onApplied = vi.fn()
    render(<LearningPlan pathId="path_demo" objectiveId="section_1" onApplied={onApplied} />)
    fireEvent.click(screen.getByRole('button', { name: '生成学习目标' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('未读到可用原文，请重试')
    expect(onApplied).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: '采用学习目标' })).toBeNull()
  })
  it('aborts an in-flight preview when navigating away', async () => {
    apiFetch.mockImplementation(() => new Promise(() => {}))
    const { unmount } = render(<LearningPlan pathId="path_demo" objectiveId="section_1" onApplied={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '生成学习目标' }))
    const signal = apiFetch.mock.calls[0][1].signal
    unmount()
    expect(signal.aborted).toBe(true)
  })
  it('labels outline placeholders as awaiting planning instead of concept requirements', () => {
    expect(knowledgeTypeLabel('concept', key => key, 'outline')).toBe('待规划')
    expect(knowledgeTypeLabel('concept', key => key, 'planned')).toBe('Concept')
  })
})
