'use client'

import { useEffect, useRef, useState } from 'react'
import { Loader2, Sparkles, Check } from 'lucide-react'
import { apiFetch } from '@/shared/api/client'
import { useBookMind } from './BookMindProvider'

type Plan = {
  id: string
  section_name: string
  goal: string
  source_locators: number[]
  goals: { name: string; type: string; mastery_requirement: string; source_locators: number[] }[]
}
const labels: Record<string, string> = { memory: '记忆', concept: '理解', procedure: '操作与解题', design: '设计与取舍' }

export function LearningPlan({ pathId, objectiveId, onApplied }: {
  pathId: string; objectiveId: string | null; onApplied: (objectiveId: string) => Promise<void>
}) {
  const { books, refresh } = useBookMind()
  const book = books.find(row => row.path_id === pathId)
  const [section, setSection] = useState('')
  const [goal, setGoal] = useState('理解原理，并能独立完成本节的典型问题或操作')
  const [prior, setPrior] = useState('')
  const [plan, setPlan] = useState<Plan | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const request = useRef<AbortController | null>(null)
  useEffect(() => () => request.current?.abort(), [])
  const focusedSection = book?.objectives.find(point => point.id === objectiveId)?.source_section_id
  useEffect(() => {
    if (focusedSection) { setSection(focusedSection); setPlan(null); setError('') }
  }, [focusedSection])
  if (!book?.sections?.length) return null
  const selected = section || book.sections[0].knowledge_point_id
  const planned = book.objectives.filter(point => point.source_section_id === selected && point.planning_status === 'planned')
  const saved = book.learning_plans?.[selected]
  async function send(action: 'preview' | 'apply') {
    if (!book) return
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setBusy(true); setError('')
    try {
      const response = await apiFetch(`/api/bookmind/books/${book.id}/learning-plan/${action}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify(action === 'preview'
          ? { objective_id: selected, goal, prior_knowledge: prior } : { plan_id: plan?.id }),
      })
      const value = await response.json()
      if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '学习目标生成失败，请重试。')
      if (action === 'preview') setPlan(value)
      else { setPlan(null); await refresh(); await onApplied(value.objective_ids[0]) }
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '请求失败，请重试。')
    } finally { if (request.current === controller) setBusy(false) }
  }
  return <section className="bm-learning-plan" aria-label="AI 学习目标规划">
    <div className="bm-plan-heading"><Sparkles size={19} /><div><h2>这一节，需要学会什么？</h2><p>AI 结合教材原文和你的学习目的，规划具体目标与检查标准。开始导学时也会自动规划当前小节。</p></div></div>
    <fieldset disabled={busy}>
      <div className="bm-plan-fields">
        <label>教材小节<select aria-label="规划教材小节" value={selected} onChange={event => { setSection(event.target.value); setPlan(null); setError('') }}>
          {book.sections.map(row => <option key={row.knowledge_point_id} value={row.knowledge_point_id}>{row.name}</option>)}
        </select></label>
        <label>学习目的<input disabled={planned.length > 0} aria-label="学习目的" maxLength={1500} value={planned.length ? saved?.goal || goal : goal} onChange={event => { setGoal(event.target.value); setPlan(null) }} /></label>
        <label>已有基础（可选）<input disabled={planned.length > 0} aria-label="已有基础" maxLength={1000} placeholder="例如：知道树的概念，还不会写遍历算法" value={planned.length ? saved?.prior_knowledge || '' : prior} onChange={event => { setPrior(event.target.value); setPlan(null) }} /></label>
      </div>
      {!planned.length && <button type="button" className="bm-secondary" onClick={() => void send('preview')}>
        {busy ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}{busy ? '正在阅读原文并规划…' : plan ? '重新生成目标' : '生成学习目标'}
      </button>}
    </fieldset>
    {error && <p role="alert" className="bm-form-error">{error}</p>}
    {(plan || planned.length > 0) && <div className="bm-plan-preview">
      <p className="bm-plan-caption">{plan ? '目标预览 · 采用后用于导学与练习' : '本节学习目标 · 已加入导学路线'}</p>
      {(plan?.goals || planned).map((item, index) => <article key={index}>
        <span className="bm-plan-type">{labels[item.type || 'concept'] || item.type}</span>
        <div><h3>{item.name}</h3><p>{item.mastery_requirement}</p></div>
      </article>)}
      {plan && <div className="bm-plan-actions"><span>依据原文位置：{plan.source_locators.join('、')}。已有作答与笔记关联会保留。</span><button type="button" disabled={busy} className="bm-primary" onClick={() => void send('apply')}><Check size={15} />{busy ? '正在采用…' : '采用学习目标'}</button></div>}
    </div>}
  </section>
}
