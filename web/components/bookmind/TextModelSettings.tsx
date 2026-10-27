'use client'
import { useEffect, useState } from 'react'
import { apiFetch } from '@/shared/api/client'
import { Loader2, Settings } from 'lucide-react'

export default function TextModelSettings() {
  const [form, setForm] = useState({ base_url: 'https://api.deepseek.com', model: '', api_key: '' })
  const [savedKey, setSavedKey] = useState(false),
    [busy, setBusy] = useState(false),
    [notice, setNotice] = useState('')
  useEffect(() => {
    void apiFetch('/api/bookmind/settings')
      .then(async response => {
        if (!response.ok) throw new Error('API 配置加载失败')
        const result = await response.json()
        setForm({ base_url: result.base_url, model: result.model, api_key: '' })
        setSavedKey(result.has_api_key)
      })
      .catch(reason => setNotice(String(reason)))
  }, [])
  async function save(test: boolean) {
    setBusy(true)
    setNotice('')
    try {
      const response = await apiFetch('/api/bookmind/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(form),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || '保存失败')
      setSavedKey(result.has_api_key)
      setForm(previous => ({ ...previous, api_key: '' }))
      if (test) {
        const tested = await apiFetch('/api/bookmind/settings/test', { method: 'POST' })
        const reply = await tested.json()
        if (!tested.ok) throw new Error(reply.detail || '连接失败')
        setNotice(`配置已保存。模型回复：${reply.reply}`)
      } else setNotice('配置已保存，阅读助教与 Mastery Path 共用这一个文字模型。')
    } catch (reason) {
      setNotice(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }
  return (
    <main className="bm-settings">
      <Settings size={25} />
      <h1>文字模型 API</h1>
      <p>阅读问答、图解、出题与笔记整理共用同一配置。密钥只保存在本机。</p>
      <form
        onSubmit={event => {
          event.preventDefault()
          void save(false)
        }}
      >
        <label>
          API 地址
          <input
            required
            type="url"
            value={form.base_url}
            onChange={e => setForm({ ...form, base_url: e.target.value })}
          />
        </label>
        <label>
          模型名称
          <input
            required
            value={form.model}
            onChange={e => setForm({ ...form, model: e.target.value })}
          />
        </label>
        <label>
          API Key
          <input
            type="password"
            autoComplete="new-password"
            value={form.api_key}
            placeholder={savedKey ? '已保存，留空保持原密钥' : '输入 API Key'}
            onChange={e => setForm({ ...form, api_key: e.target.value })}
          />
        </label>
        <div className="bm-settings-actions">
          <button className="bm-primary" type="submit" disabled={busy}>
            {busy && <Loader2 size={16} className="animate-spin" />}保存配置
          </button>
          <button type="button" onClick={() => void save(true)} disabled={busy}>
            保存并测试连接
          </button>
        </div>
        <small>测试会发送一次简短的模型请求。</small>
        {notice && (
          <p role="status" className="bm-notice">
            {notice}
          </p>
        )}
      </form>
    </main>
  )
}
