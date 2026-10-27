import { useEffect, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  CircleHelp,
  Eye,
  EyeOff,
  KeyRound,
  Save,
  Settings as SettingsIcon,
  ShieldCheck,
  Unplug,
  Zap,
} from "lucide-react";
import { useApp } from "./App";
import { post, put } from "./api";
import { Spinner } from "./components";
import type { Settings } from "./types";

export default function SettingsPage() {
  const { settings, refreshSettings, toast } = useApp();
  const [form, setForm] = useState<Settings>(),
    [busy, setBusy] = useState(""),
    [visible, setVisible] = useState(false),
    [result, setResult] = useState<{ ok: boolean; text: string }>();
  useEffect(() => {
    if (settings) setForm({ ...settings, api_key: "" });
  }, [settings]);
  if (!form)
    return (
      <div className="page-loading">
        <Spinner label="正在加载设置" />
      </div>
    );
  const update = (values: Partial<Settings>) => {
    setForm({ ...form, ...values });
    setResult(undefined);
  };
  const save = async () => {
    setBusy("save");
    try {
      await put("/settings", form);
      await refreshSettings();
      setResult(undefined);
      toast("API 设置已保存。");
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusy("");
    }
  };
  const test = async () => {
    setBusy("test");
    setResult(undefined);
    try {
      const value = await post<{ model: string; reply: string }>(
        "/settings/test",
        form,
      );
      setResult({
        ok: true,
        text: `${value.model} 已返回实际回复：“${value.reply}”`,
      });
    } catch (e) {
      setResult({ ok: false, text: (e as Error).message });
    } finally {
      setBusy("");
    }
  };
  return (
    <div className="settings-page">
      <header className="home-header">
        <span className="breadcrumb">
          <SettingsIcon size={15} />
          API 设置
        </span>
        <span className="header-label">配置一次，在所有学习空间使用</span>
      </header>
      <main className="settings-content">
        <div className="eyebrow">CONNECT YOUR INTELLIGENCE</div>
        <h1>连接你的 AI 助教。</h1>
        <p className="settings-lead">
          使用一个文字模型，完成讲解、画图、编写手册与出题。
        </p>
        <div className="settings-layout">
          <section className="white-panel settings-form">
            <div className="settings-panel-heading">
              <span className="card-icon green">
                <Zap size={22} />
              </span>
              <div>
                <h2>模型服务</h2>
                <p>支持 OpenAI 格式的 Chat Completions 接口</p>
              </div>
              <span
                className={"configured-tag " + (form.has_api_key ? "yes" : "")}
              >
                {form.has_api_key ? "已保存密钥" : "待配置"}
              </span>
            </div>
            <label className="field">
              API 地址（Base URL）
              <input
                aria-label="API 地址"
                type="url"
                value={form.base_url}
                onChange={(e) => update({ base_url: e.target.value })}
                placeholder="https://api.deepseek.com"
              />
              <small>
                填写服务根地址；如果服务要求 /v1，请包含它。不要添加
                /chat/completions。
              </small>
            </label>
            <label className="field">
              API Key
              <div className="password-input">
                <input
                  aria-label="API Key"
                  type={visible ? "text" : "password"}
                  autoComplete="new-password"
                  value={form.api_key}
                  onChange={(e) => update({ api_key: e.target.value })}
                  placeholder={
                    form.has_api_key
                      ? "已配置，留空可保留当前密钥"
                      : "输入你的 API 密钥"
                  }
                />
                <button
                  type="button"
                  aria-label={visible ? "隐藏密钥" : "显示密钥"}
                  onClick={() => setVisible((v) => !v)}
                >
                  {visible ? <EyeOff size={17} /> : <Eye size={17} />}
                </button>
              </div>
              <small>
                <ShieldCheck size={12} />
                密钥保存在本机后端，不会返回到页面。
              </small>
            </label>
            <label className="field">
              模型名称
              <input
                aria-label="模型名称"
                value={form.model}
                onChange={(e) => update({ model: e.target.value })}
                placeholder="填写你的服务商提供的模型 ID"
              />
              <small>需要支持中文、流式输出与结构化内容生成。</small>
            </label>
            <details className="advanced-settings">
              <summary>
                高级设置 <ChevronDown size={15} />
              </summary>
              <div className="advanced-grid">
                <label className="field">
                  温度
                  <input
                    type="number"
                    value={form.temperature}
                    min={0}
                    max={1.5}
                    step={0.1}
                    onChange={(e) => update({ temperature: +e.target.value })}
                  />
                </label>
                <label className="field">
                  最大输出 Tokens
                  <input
                    type="number"
                    value={form.max_tokens}
                    min={1024}
                    max={16000}
                    step={1000}
                    onChange={(e) => update({ max_tokens: +e.target.value })}
                  />
                </label>
                <label className="field">
                  超时（秒）
                  <input
                    type="number"
                    value={form.timeout}
                    min={30}
                    max={600}
                    onChange={(e) => update({ timeout: +e.target.value })}
                  />
                </label>
              </div>
            </details>
            {result && (
              <div
                role="status"
                className={"connection-result " + (result.ok ? "ok" : "fail")}
              >
                {result.ok ? <CheckCircle2 size={18} /> : <Unplug size={18} />}
                <div>
                  <strong>{result.ok ? "连接测试通过" : "连接未成功"}</strong>
                  <p>{result.text}</p>
                  <small>{result.ok ? "点击“保存设置”使修改生效。" : ""}</small>
                </div>
              </div>
            )}
            <div className="settings-actions">
              <button
                className="btn"
                disabled={!!busy}
                onClick={() => void test()}
              >
                {busy === "test" ? (
                  <Spinner label="测试实际模型回复" />
                ) : (
                  <>
                    <Unplug size={16} />
                    测试连接
                  </>
                )}
              </button>
              <button
                className="btn primary"
                disabled={!!busy || !form.base_url || !form.model}
                onClick={() => void save()}
              >
                {busy === "save" ? (
                  <Spinner label="保存中" />
                ) : (
                  <>
                    <Save size={16} />
                    保存设置
                  </>
                )}
              </button>
            </div>
          </section>
          <aside className="settings-help">
            <div className="help-symbol">
              <CircleHelp size={25} />
            </div>
            <h3>这一版，需要配置什么？</h3>
            <p>一个可用的文字模型 API 就够了。</p>
            <div className="help-item">
              <CheckCircle2 size={16} />
              <div>
                <strong>讲解与提问</strong>
                <span>结合资料原文回答</span>
              </div>
            </div>
            <div className="help-item">
              <CheckCircle2 size={16} />
              <div>
                <strong>教学图解</strong>
                <span>生成关系图、SVG 和分步演示</span>
              </div>
            </div>
            <div className="help-item">
              <CheckCircle2 size={16} />
              <div>
                <strong>学习手册与练习</strong>
                <span>围绕目标生成，依据原文出题</span>
              </div>
            </div>
            <div className="notice">
              第一版使用本地文字检索，不需要额外的 Embedding
              API。语音和图像模型后续再接入。
            </div>
            <p className="settings-privacy">
              <KeyRound size={15} />
              本机配置文件位于项目的 data 目录，请勿连同密钥一起分享项目。
            </p>
          </aside>
        </div>
      </main>
    </div>
  );
}
