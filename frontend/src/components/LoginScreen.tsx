import { useState } from 'react';
import { login, register } from '../api/auth';

/** 登录/注册页（多用户改造 P4）。开放注册；邮箱 + 密码（≥8 位）。 */
export default function LoginScreen({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const canSubmit = email.includes('@') && password.length >= (mode === 'register' ? 8 : 1) && !busy;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError('');
    try {
      if (mode === 'login') {
        await login(email.trim(), password);
      } else {
        await register(email.trim(), password);
      }
      onLoggedIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败，请重试');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-ct-bg px-4">
      <div className="w-full max-w-sm rounded-2xl border border-ct-border bg-ct-panel p-8 shadow-sm">
        <h1 className="text-center text-xl font-medium text-ct-text">
          {mode === 'login' ? '登录 Code Tutor' : '注册 Code Tutor'}
        </h1>
        <p className="mt-2 text-center text-sm text-ct-muted">
          {mode === 'login' ? '用邮箱继续你的算法练习' : '注册后即可开始练习'}
        </p>

        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          <div>
            <label className="mb-1 block text-sm text-ct-muted">邮箱</label>
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
              className="w-full rounded-lg border border-ct-border bg-ct-bg px-3 py-2 text-ct-text outline-none focus:border-ct-accent"
              autoComplete="email"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm text-ct-muted">
              密码{mode === 'register' && '（至少 8 位）'}
            </label>
            <input
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder={mode === 'register' ? '至少 8 位' : '密码'}
              className="w-full rounded-lg border border-ct-border bg-ct-bg px-3 py-2 text-ct-text outline-none focus:border-ct-accent"
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            />
          </div>

          {error && (
            <div className="rounded-lg border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-600">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={!canSubmit}
            className="w-full rounded-lg bg-ct-accent px-4 py-2 font-medium text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {busy ? '请稍候…' : mode === 'login' ? '登录' : '注册并登录'}
          </button>
        </form>

        <button
          type="button"
          onClick={() => {
            setMode(mode === 'login' ? 'register' : 'login');
            setError('');
          }}
          className="mt-4 w-full text-center text-sm text-ct-muted hover:text-ct-text"
        >
          {mode === 'login' ? '没有账号？注册一个' : '已有账号？去登录'}
        </button>
      </div>
    </div>
  );
}
