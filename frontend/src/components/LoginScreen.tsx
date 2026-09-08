import { useState } from 'react';
import { forgotPassword, login, register, resetPassword } from '../api/auth';

/** 注册条款全文（「服务条款」弹窗内容；用户要求默认勾选同意）。 */
const TERMS_TEXT = `CodeTutor Agent 由独立开发者（GitHub @YanzheShi）以 Beta 提供，开源于 https://github.com/YanzheShi/code-tutor-agent 。

当前为测试阶段，功能可能不稳定；域名可能变更、服务可能下线；使用系统 API 有对话/提交配额，自定义 API Key 无系统配额但受模型方限制；因网络、节点、数据库问题可能导致数据丢失，重要数据请自行导出。

继续注册即同意《使用条款》与《隐私政策》。`;

/** 登录/注册/忘记密码页（多用户改造 P4 + 防滥用改造）。
 *
 * - 注册需要邀请码（admin 面板生成，额度内有效）
 * - 忘记密码三步流：填邮箱 → 收验证码（Brevo 未配置时提示找管理员）→ 验证码 + 新密码重置
 */
export default function LoginScreen({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [mode, setMode] = useState<'login' | 'register' | 'forgot'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [inviteCode, setInviteCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  // 注册条款：默认勾选同意；「服务条款」点开可查看全文
  const [agreed, setAgreed] = useState(true);
  const [showTerms, setShowTerms] = useState(false);
  // 忘记密码流程状态
  const [codeSent, setCodeSent] = useState(false);
  const [resetCode, setResetCode] = useState('');

  const canSubmit =
    email.includes('@') &&
    password.length >= (mode === 'register' ? 8 : 1) &&
    (mode !== 'register' || (confirmPassword.length >= 8 && confirmPassword === password)) &&
    (mode !== 'register' || inviteCode.trim().length >= 4) &&
    (mode !== 'register' || agreed) &&
    !busy;

  const switchMode = (m: typeof mode) => {
    setMode(m);
    setError('');
    setNotice('');
    setCodeSent(false);
  };

  const handleForgotRequest = async () => {
    if (!email.includes('@') || busy) return;
    setBusy(true);
    setError('');
    try {
      const r = await forgotPassword(email);
      setNotice(r.message);
      if (r.delivered === true) setCodeSent(true);
      if (r.delivered === false) setCodeSent(false);
      if (r.delivered === null) setCodeSent(true); // 后端统一措辞，按已发送处理
    } catch (err) {
      setError(err instanceof Error ? err.message : '请求失败，请重试');
    } finally {
      setBusy(false);
    }
  };

  const handleResetSubmit = async () => {
    if (!resetCode.trim() || password.length < 8 || busy) return;
    setBusy(true);
    setError('');
    try {
      await resetPassword(email, resetCode, password);
      setNotice('密码已重置，请用新密码登录');
      setTimeout(() => switchMode('login'), 1200);
    } catch (err) {
      setError(err instanceof Error ? err.message : '重置失败');
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError('');
    try {
      if (mode === 'login') {
        await login(email.trim(), password);
      } else {
        if (confirmPassword !== password) {
          setError('两次输入的密码不一致');
          return;
        }
        await register(email.trim(), password, confirmPassword, inviteCode);
      }
      onLoggedIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败，请重试');
    } finally {
      setBusy(false);
    }
  };

  const inputCls =
    'w-full rounded-lg border border-ct-border bg-ct-bg px-3 py-2 text-ct-text outline-none focus:border-ct-accent';

  return (
    <div className="flex min-h-screen items-center justify-center bg-ct-bg px-4">
      <div className="w-full max-w-sm rounded-2xl border border-ct-border bg-ct-panel p-8 shadow-sm">
        <h1 className="text-center text-xl font-medium text-ct-text">
          {mode === 'login' ? '登录 Code Tutor' : mode === 'register' ? '注册 Code Tutor' : '找回密码'}
        </h1>
        <p className="mt-2 text-center text-sm text-ct-muted">
          {mode === 'login' && '用邮箱继续你的算法练习'}
          {mode === 'register' && '注册需要邀请码，可向管理员获取'}
          {mode === 'forgot' && '输入注册邮箱，按提示重置密码'}
        </p>

        {mode !== 'forgot' && (
          <form onSubmit={handleSubmit} className="mt-6 space-y-4">
            <div>
              <label className="mb-1 block text-sm text-ct-muted">邮箱</label>
              <input
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className={inputCls}
                autoComplete="email"
              />
            </div>
            {mode === 'register' && (
              <div>
                <label className="mb-1 block text-sm text-ct-muted">邀请码</label>
                <input
                  type="text"
                  required
                  value={inviteCode}
                  onChange={(e) => setInviteCode(e.target.value)}
                  placeholder="向管理员获取"
                  className={`${inputCls} font-mono tracking-widest uppercase`}
                  autoComplete="off"
                />
              </div>
            )}
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
                className={inputCls}
                autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              />
            </div>
            {mode === 'register' && (
              <div>
                <label className="mb-1 block text-sm text-ct-muted">确认密码</label>
                <input
                  type="password"
                  required
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  placeholder="再次输入密码"
                  className={`${inputCls} ${confirmPassword && confirmPassword !== password ? 'border-red-300' : ''}`}
                  autoComplete="new-password"
                />
              </div>
            )}
            {mode === 'register' && (
              <label className="flex items-start gap-2 text-xs leading-relaxed text-ct-muted">
                <input
                  type="checkbox"
                  checked={agreed}
                  onChange={(e) => setAgreed(e.target.checked)}
                  className="mt-0.5 shrink-0"
                />
                <span>
                  我已阅读并同意
                  <button
                    type="button"
                    onClick={() => setShowTerms(true)}
                    className="mx-0.5 text-ct-accent underline underline-offset-2 hover:opacity-80"
                  >
                    服务条款
                  </button>
                  ，点击查看
                </span>
              </label>
            )}

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
        )}

        {mode === 'forgot' && (
          <div className="mt-6 space-y-4">
            <div>
              <label className="mb-1 block text-sm text-ct-muted">注册邮箱</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                className={inputCls}
                disabled={codeSent}
              />
            </div>
            {!codeSent ? (
              <button
                type="button"
                onClick={handleForgotRequest}
                disabled={!email.includes('@') || busy}
                className="w-full rounded-lg bg-ct-accent px-4 py-2 font-medium text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busy ? '发送中…' : '发送验证码'}
              </button>
            ) : (
              <>
                <div>
                  <label className="mb-1 block text-sm text-ct-muted">邮箱验证码（15 分钟内有效）</label>
                  <input
                    type="text"
                    value={resetCode}
                    onChange={(e) => setResetCode(e.target.value)}
                    placeholder="6 位数字"
                    className={`${inputCls} font-mono tracking-widest`}
                    autoComplete="one-time-code"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-sm text-ct-muted">新密码（至少 8 位）</label>
                  <input
                    type="password"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    placeholder="至少 8 位"
                    className={inputCls}
                    autoComplete="new-password"
                  />
                </div>
                <button
                  type="button"
                  onClick={handleResetSubmit}
                  disabled={!resetCode.trim() || password.length < 8 || busy}
                  className="w-full rounded-lg bg-ct-accent px-4 py-2 font-medium text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busy ? '提交中…' : '重置密码'}
                </button>
              </>
            )}
          </div>
        )}

        {notice && (
          <div className="mt-3 rounded-lg border border-blue-300 bg-blue-50 px-3 py-2 text-sm text-blue-700">
            {notice}
          </div>
        )}

        <div className="mt-4 flex w-full justify-between text-sm text-ct-muted">
          {mode === 'forgot' ? (
            <button
              type="button"
              onClick={() => switchMode('login')}
              className="hover:text-ct-text"
            >
              ← 返回登录
            </button>
          ) : (
            <button
              type="button"
              onClick={() => switchMode(mode === 'register' ? 'login' : 'register')}
              className="hover:text-ct-text"
            >
              {mode === 'register' ? '已有账号？去登录' : '没有账号？注册一个'}
            </button>
          )}
          {mode !== 'forgot' && (
            <button type="button" onClick={() => switchMode('forgot')} className="hover:text-ct-text">
              忘记密码？
            </button>
          )}
        </div>

        {/* 服务条款弹窗 */}
        {showTerms && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
            onClick={() => setShowTerms(false)}
          >
            <div
              className="max-h-[80vh] w-full max-w-md overflow-y-auto rounded-2xl border border-ct-border bg-ct-panel p-6 shadow-lg"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 className="mb-3 text-base font-medium text-ct-text">服务条款</h2>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-ct-muted">{TERMS_TEXT}</p>
              <button
                type="button"
                onClick={() => setShowTerms(false)}
                className="mt-4 w-full rounded-lg bg-ct-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90"
              >
                我知道了
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
