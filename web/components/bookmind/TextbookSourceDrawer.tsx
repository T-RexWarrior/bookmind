'use client'

import Link from 'next/link'
import { useCallback, useEffect, useRef, useState } from 'react'
import { BookOpen, ChevronLeft, ChevronRight, Loader2, X } from 'lucide-react'
import { getMaterial, getUnitText, rawMaterialUrl, type MaterialDetail } from '@/lib/reading-api'
import { citationTargetFromHref } from '@/lib/reading-citations'
import type { Textbook } from './BookMindProvider'

type Target = { locator: number; materialRevision?: number }
type Source = { material: MaterialDetail; text: string; locator: number }

/** A separate preview: inspecting evidence must not move the active reader. */
export default function TextbookSourceDrawer({ book }: { book: Textbook }) {
  const [target, setTarget] = useState<Target | null>(null)
  const [resolution, setResolution] = useState<{ target: Target; source?: Source; error?: string } | null>(null)
  const source = resolution?.target === target ? resolution?.source : undefined
  const error = resolution?.target === target ? resolution?.error : ''
  const loading = Boolean(target && resolution?.target !== target)
  const [view, setView] = useState<'text' | 'pdf'>('text')
  const panel = useRef<HTMLElement>(null)
  const close = useCallback(() => setTarget(null), [])

  useEffect(() => {
    const open = (event: Event) => {
      const detail = (event as CustomEvent<{ materialId?: string; locator: number }>).detail
      if (detail?.materialId !== book.material_id || !Number.isSafeInteger(detail.locator) || detail.locator < 1) return
      setTarget({ locator: detail.locator })
    }
    const followCitation = (event: MouseEvent) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
      const anchor = (event.target as HTMLElement | null)?.closest?.('a[href]')
      const citation = citationTargetFromHref(anchor?.getAttribute('href'))
      if (!citation || citation.materialId !== book.material_id) return
      event.preventDefault()
      event.stopPropagation()
      setTarget(citation)
    }
    window.addEventListener('bookmind-source-open', open)
    document.addEventListener('click', followCitation, true)
    return () => {
      window.removeEventListener('bookmind-source-open', open)
      document.removeEventListener('click', followCitation, true)
    }
  }, [book.material_id])

  useEffect(() => {
    if (!target) return
    let active = true
    void getMaterial(book.material_id).then(async material => {
      if (target.materialRevision && target.materialRevision !== material.revision) {
        throw new Error('这条引用对应教材的旧版本，请在阅读器中核对版本后查看。')
      }
      if (target.locator > material.unit_count) throw new Error('原文位置超出了教材范围。')
      const unit = await getUnitText(book.material_id, target.locator)
      if (active) setResolution({ target, source: { material, text: unit.text, locator: target.locator } })
    }).catch(reason => {
      if (active) setResolution({ target, error: reason instanceof Error ? reason.message : '原文加载失败，请重试。' })
    })
    return () => { active = false }
  }, [book.material_id, target])

  const isOpen = Boolean(target)
  useEffect(() => {
    if (!isOpen) return
    const previous = document.activeElement as HTMLElement | null
    panel.current?.querySelector<HTMLButtonElement>('button')?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); close() }
      if (event.key !== 'Tab') return
      const controls = panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], iframe, [tabindex="0"]')
      if (!controls?.length) return
      const first = controls[0], last = controls[controls.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('keydown', onKey); previous?.focus() }
  }, [isOpen, close])

  if (!target) return null
  return (
    <div className="bm-source-backdrop" onClick={close}>
      <aside ref={panel} role="dialog" aria-modal="true" aria-labelledby="bm-source-title" className="bm-source-drawer" onClick={event => event.stopPropagation()}>
        <header>
          <div><BookOpen size={17} /><div><h2 id="bm-source-title">教材原文</h2><p>{book.title}</p></div></div>
          <button type="button" onClick={close} aria-label="关闭原文"><X size={19} /></button>
        </header>
        <div className="bm-source-toolbar">
          <button type="button" aria-label="上一页原文" disabled={target.locator <= 1 || loading} onClick={() => setTarget({ ...target, locator: target.locator - 1 })}><ChevronLeft size={17} /></button>
          <span>第 {target.locator} {source?.material.unit === 'page' ? '页' : '节'}</span>
          <button type="button" aria-label="下一页原文" disabled={!source || target.locator >= source.material.unit_count || loading} onClick={() => setTarget({ ...target, locator: target.locator + 1 })}><ChevronRight size={17} /></button>
          {source?.material.render_mode === 'pdf' && <button type="button" className="bm-source-view" onClick={() => setView(view === 'text' ? 'pdf' : 'text')}>{view === 'text' ? '查看 PDF 原页' : '查看原文文本'}</button>}
        </div>
        <div className="bm-source-body" aria-busy={loading}>
          {loading ? <p className="bm-source-status"><Loader2 size={18} className="animate-spin" />正在读取教材原文…</p> : error ? <p role="alert" className="bm-error">{error}</p> : source && (
            view === 'pdf' && source.material.render_mode === 'pdf'
              ? <iframe title={`教材原页 · 第 ${source.locator} 页`} src={`${rawMaterialUrl(book.material_id)}#page=${source.locator}&view=FitH`} />
              : <div className="bm-source-text">{source.text || '这一页没有可提取的文字，可查看 PDF 原页。'}</div>
          )}
        </div>
        <footer><span>{source?.material.render_mode === 'pdf' ? 'PDF 页码按文件实际页序定位。' : '来源按教材原始章节定位。'}</span><Link href={`${book.links.learn}?locator=${target.locator}`} onClick={close}>在阅读器中打开 ↗</Link></footer>
      </aside>
    </div>
  )
}
