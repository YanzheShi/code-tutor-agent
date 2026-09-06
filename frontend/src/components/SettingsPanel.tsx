import { useCallback, useEffect, useState } from 'react';
import { fetchLlmSettings, saveLlmSettings, testLlmSettings, type LlmSettings } from '../api/settings';
import { useTheme } from '../hooks/useTheme';

/** 设置页 — 外观（明暗主题）+ 模型服务（自定义 API key / Base URL / 模型）。
 *
 * 视觉风格与 WelcomeScreen/AdminPanel 一致：ct-* CSS 变量 + 圆角卡片。
 * 主题即时生效（useTheme 直接切换）；模型设置保存后才生效（下一请求起用）。
 */

type LlmMode = 'default' | 'custom';
type Feedback = { kind: 'ok' | 'err' | 'info'; text: string } | null;

const FIELD_CLS =
  'w-full rounded-lg border border-ct-border bg-ct-input px-3 py-2 text-sm text-ct-text ' +
  'placeholder:text-ct-muted/60 outline-none transition focus:border-ct-accent focus:ring-2 focus:ring-ct-accent/20';

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1.5 flex items-baseline gap-2">
        <span className="text-sm font-medium text-ct-text">{label}</span>
        {hint && <span className="text-xs text-ct-muted">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

export default function SettingsPanel({ onClose }: { onClose: () => void }) {
  const { theme, setTheme } = useTheme();

  // ── 模型服务表单状态 ──
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [mode, setMode] = useState<LlmMode>('default');
  const [model, setModel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [savedMasked, setSavedMasked] = useState(''); // 已保存 key 的打码形式（placeholder 提示用）
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);

  useEffect(() => {
    let cancelled = false;
    fetchLlmSettings()
      .then((s: LlmSettings) => {
        if (cancelled) return;
        setMode(s.mode);
        setModel(s.model || '');
        setBaseUrl(s.base_url || '');
        setSavedMasked(s.api_key_masked || '');
      })
      .catch((e) => { if (!cancelled) setLoadError(String(e?.message || e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const currentInput = useCallback(
    () => ({ mode, model: model.trim(), base_url: baseUrl.trim(), api_key: apiKey.trim() }),
    [mode, model, baseUrl, apiKey],
  );

  const handleSave = async () => {
    setSaving(true); setFeedback(null);
    try {
      const saved = await saveLlmSettings(currentInput());
      setMode(saved.mode);
      setModel(saved.model || '');
      setBaseUrl(saved.base_url || '');
      setApiKey('');            // 保存成功后清空明文输入
      setSavedMasked(saved.api_key_masked || '');
      setFeedback({ kind: 'ok', text: saved.mode === 'custom' ? '已保存，下一请求起使用你的自定义模型配置' : '已切换回系统默认模型' });
    } catch (e: any) {
      setFeedback({ kind: 'err', text: String(e?.message || e) });
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    setTesting(true); setFeedback({ kind: 'info', text: '正在测试连接…' });
    try {
      const r = await testLlmSettings(currentInput());
      setFeedback(r.ok
        ? { kind: 'ok', text: `连接成功${r.sample ? `，模型回复：${r.sample}` : ''}` }
        : { kind: 'err', text: `连接失败：${r.error || '未知错误'}` });
    } catch (e: any) {
      setFeedback({ kind: 'err', text: String(e?.message || e) });
    } finally {
      setTesting(false);
    }
  };

  const feedbackCls = feedback?.kind === 'ok' ? 'bg-ct-success-bg text-ct-success'
    : feedback?.kind === 'err' ? 'bg-ct-error-bg text-ct-error'
    : 'bg-ct-info-bg text-ct-info';

  return (
    <div className="flex min-h-screen items-center justify-center bg-ct-bg p-4">
      <div className="flex h-[720px] max-h-[calc(100vh-2rem)] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-ct-border bg-ct-surface shadow-sm">
        {/* 顶栏 */}
        <div className="flex shrink-0 items-center border-b border-ct-border px-5 py-4">
          <button
            onClick={onClose}
            className="rounded-lg border border-ct-border bg-ct-panel px-3 py-1.5 text-xs font-medium text-ct-muted transition hover:border-ct-accent/50 hover:text-ct-text"
          >
            ← 返回
          </button>
          <h1 className="flex-1 text-center text-lg font-semibold text-ct-text">⚙️ 设置</h1>
          {/* 占位，保证标题严格居中 */}
          <span className="w-[68px]" />
        </div>

        <div className="flex-1 space-y-6 overflow-y-auto px-5 py-5">
          {/* ── 外观 ── */}
          <section>
            <h2 className="mb-3 text-sm font-semibold text-ct-text">外观</h2>
            <div className="grid grid-cols-2 gap-3">
              {([
                { id: 'light' as const, label: '浅色', icon: '☀️', preview: '#f6f8fa' },
                { id: 'dark' as const, label: '深色', icon: '🌙', preview: '#0d1117' },
              ]).map(opt => (
                <button
                  key={opt.id}
                  onClick={() => setTheme(opt.id)}
                  className={`flex items-center gap-3 rounded-xl border p-4 text-left transition ${
                    theme === opt.id
                      ? 'border-ct-accent bg-ct-accent/10 ring-2 ring-ct-accent/20'
                      : 'border-ct-border hover:border-ct-accent/50'
                  }`}
                >
                  <span
                    className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-ct-border text-lg"
                    style={{ backgroundColor: opt.preview }}
                  >
                    {opt.icon}
                  </span>
                  <span className="flex-1">
                    <span className="block text-sm font-medium text-ct-text">{opt.label}</span>
                    <span className="block text-xs text-ct-muted">
                      {theme === opt.id ? '当前主题' : '点击切换'}
                    </span>
                  </span>
                  {theme === opt.id && <span className="text-ct-accent">✓</span>}
                </button>
              ))}
            </div>
          </section>

          {/* ── 模型服务 ── */}
          <section>
            <h2 className="mb-1 text-sm font-semibold text-ct-text">模型服务</h2>
            <p className="mb-3 text-xs text-ct-muted">
              选择出题、判题、对话共用的模型接入方式。自定义需为 OpenAI 兼容接口。
            </p>

            {loading ? (
              <p className="text-sm text-ct-muted">加载中…</p>
            ) : loadError ? (
              <p className="rounded-lg bg-ct-error-bg px-3 py-2 text-sm text-ct-error">{loadError}</p>
            ) : (
              <>
                {/* 模式选择 */}
                <div className="mb-4 grid grid-cols-2 gap-3">
                  <button
                    onClick={() => setMode('default')}
                    className={`rounded-xl border p-4 text-left transition ${
                      mode === 'default'
                        ? 'border-ct-accent bg-ct-accent/10 ring-2 ring-ct-accent/20'
                        : 'border-ct-border hover:border-ct-accent/50'
                    }`}
                  >
                    <span className="block text-sm font-medium text-ct-text">🖥️ 系统默认</span>
                    <span className="mt-0.5 block text-xs text-ct-muted">使用服务器统一配置</span>
                  </button>
                  <button
                    onClick={() => setMode('custom')}
                    className={`rounded-xl border p-4 text-left transition ${
                      mode === 'custom'
                        ? 'border-ct-accent bg-ct-accent/10 ring-2 ring-ct-accent/20'
                        : 'border-ct-border hover:border-ct-accent/50'
                    }`}
                  >
                    <span className="block text-sm font-medium text-ct-text">🔑 自定义</span>
                    <span className="mt-0.5 block text-xs text-ct-muted">填入你自己的 API key</span>
                  </button>
                </div>

                {mode === 'custom' && (
                  <div className="space-y-4">
                    <Field label="模型名称" hint="如 deepseek-chat、gpt-4o">
                      <input className={FIELD_CLS} value={model}
                        onChange={e => setModel(e.target.value)}
                        placeholder="deepseek-chat" autoComplete="off" />
                    </Field>
                    <Field label="Base URL" hint="OpenAI 兼容接口地址">
                      <input className={FIELD_CLS} value={baseUrl}
                        onChange={e => setBaseUrl(e.target.value)}
                        placeholder="https://api.deepseek.com/v1" autoComplete="off" />
                    </Field>
                    <Field label="API Key"
                      hint={savedMasked ? `已保存：${savedMasked}，留空沿用` : undefined}>
                      <input className={FIELD_CLS} type="password" value={apiKey}
                        onChange={e => setApiKey(e.target.value)}
                        placeholder={savedMasked ? `沿用已保存的 ${savedMasked}` : 'sk-...'}
                        autoComplete="new-password" />
                    </Field>
                  </div>
                )}

                {/* 操作行 */}
                <div className="mt-4 flex items-center gap-3">
                  {mode === 'custom' && (
                    <button onClick={handleTest} disabled={testing || saving}
                      className="rounded-lg border border-ct-border px-4 py-2 text-sm text-ct-text transition hover:bg-ct-hover disabled:opacity-40">
                      {testing ? '测试中…' : '测试连接'}
                    </button>
                  )}
                  <span className="flex-1" />
                  <button onClick={handleSave} disabled={saving || testing}
                    className="rounded-lg bg-ct-accent px-5 py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-40">
                    {saving ? '保存中…' : '保存'}
                  </button>
                </div>

                {feedback && (
                  <p className={`mt-3 rounded-lg px-3 py-2 text-xs break-all ${feedbackCls}`}>
                    {feedback.text}
                  </p>
                )}
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
