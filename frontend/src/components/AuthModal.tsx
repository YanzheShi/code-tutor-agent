import { useEffect, useRef, useState } from 'react';
import {
  claimAccount, forgotPassword, login, register, resetPassword,
  fetchPublicInvite, sendRegisterCode,
} from '../api/auth';

/** 注册条款全文（「服务条款」弹窗内容；用户要求默认勾选同意）。 */
const TERMS_TEXT = `CodeTutor Agent 由独立开发者（GitHub @YanzheShi）以 Beta 提供，开源于 https://github.com/YanzheShi/code-tutor-agent 。

欢迎使用！ 当前为测试阶段，功能可能不稳定，有可能在做题过程中重启；域名可能变更、服务可能下线；使用系统 API 有对话/提交配额，如果使用频繁，可以自部署使用自己的apikey；因网络、节点、数据库问题可能导致数据丢失，重要数据请自行导出。

继续注册即同意《服务条款》。`;

/** 登录/注册/忘记密码弹窗（访客主页改造：从 LoginScreen 抽取的表单逻辑 + 弹窗壳）。
 *
 * - 访客在主页点「开始使用 / 登录」时弹出，不整页跳转
 * - 注册 = 邀请码 +（邮件通道可用时）邮箱验证码双确认
 * - claimMode：体验账号（测试用户）转正注册——提交走 /auth/claim 原地升级，
 *   user_id 不变、做题记录全保留（2026-09-10 测试用户体系）
 * - 忘记密码三步流：填邮箱 → 收验证码（邮件未配置时提示找管理员）→ 验证码 + 新密码重置
 * - 遮罩点击 / Esc / 右上角 × 均可关闭；登录或注册成功回调 onLoggedIn（由调用方决定后续，如整页刷新）
 */
export default function AuthModal({ open, onClose, onLoggedIn, claimMode = false }: {
  open: boolean;
  onClose: () => void;
  onLoggedIn: () => void;
  /** 体验账号转正模式：弹窗直接进注册表单，提交走 claim 接口。 */
  claimMode?: boolean;
}) {
  const [mode, setMode] = useState<'login' | 'register' | 'forgot'>(claimMode ? 'register' : 'login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [inviteCode, setInviteCode] = useState('');
  // 公开邀请码：admin 在面板标记后，注册页自动拉取并预填，用户免手填
  const [publicInvite, setPublicInvite] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  // 注册条款：默认勾选同意；「服务条款」点开可查看全文
  const [agreed, setAgreed] = useState(true);
  const [showTerms, setShowTerms] = useState(false);
  // 注册邮箱验证码（2026-09-10）：邮件通道可用时注册需「邀请码 + 邮箱码」双确认
  const [emailVerification, setEmailVerification] = useState(false);
  const [emailCode, setEmailCode] = useState('');
  const [sendCooldown, setSendCooldown] = useState(0); // 剩余秒数（60s 倒计时，与服务端 per-IP 1/min 对齐）
  const cooldownTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  // 忘记密码流程状态
  const [codeSent, setCodeSent] = useState(false);
  const [resetCode, setResetCode] = useState('');

  // Esc 关闭（条款弹窗打开时不响应，避免误关两层）
  useEffect(() => {
    if (!open || showTerms) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, showTerms, onClose]);

  // 60s 发送倒计时（纯 UX；真正的限频锚点在服务端 per-IP 1/min）
  useEffect(() => {
    if (sendCooldown <= 0) return;
    cooldownTimer.current = setInterval(() => {
      setSendCooldown((s) => (s <= 1 ? 0 : s - 1));
    }, 1000);
    return () => {
      if (cooldownTimer.current) clearInterval(cooldownTimer.current);
      cooldownTimer.current = null;
    };
  }, [sendCooldown > 0]);

  // claim 模式：弹窗打开即进注册表单（体验账号转正）
  useEffect(() => {
    if (open && claimMode) setMode('register');
  }, [open, claimMode]);

  // 进入注册模式即拉取公开邀请码并预填（admin 在面板标记的码免手填）+ 邮箱验证开关
  useEffect(() => {
    if (!open || mode !== 'register') return;
    let cancelled = false;
    setPublicInvite(null);
    setEmailCode('');
    setSendCooldown(0);
    fetchPublicInvite().then((r) => {
      if (cancelled) return;
      if (r.enabled && r.invite_code) {
        setPublicInvite(r.invite_code);
        setInviteCode(r.invite_code);
      }
      setEmailVerification(r.email_verification);
    });
    return () => { cancelled = true; };
  }, [open, mode]);

  const canSubmit =
    email.includes('@') &&
    password.length >= (mode === 'register' ? 8 : 1) &&
    (mode !== 'register' || (confirmPassword.length >= 8 && confirmPassword === password)) &&
    (mode !== 'register' || inviteCode.trim().length >= 4) &&
    (mode !== 'register' || !emailVerification || emailCode.trim().length === 6) &&
    (mode !== 'register' || agreed) &&
    !busy;

  const switchMode = (m: typeof mode) => {
    setMode(m);
    setError('');
    setNotice('');
    setCodeSent(false);
  };

  // 注册验证码下发：前端 60s 倒计时是 UX，服务端 per-IP 1/min 才是硬限频
  const handleSendRegisterCode = async () => {
    if (!email.includes('@') || sendCooldown > 0 || busy) return;
    setBusy(true);
    setError('');
    try {
      const r = await sendRegisterCode(email.trim());
      if (!r.email_verification) {
        // 邮件通道未配置：注册无需验证码，藏起发码 UI
        setEmailVerification(false);
        setNotice(r.message || '邮件服务未配置，注册无需邮箱验证码。');
        return;
      }
      setNotice(r.message || '验证码已发送，请查收（含垃圾箱）。');
      setSendCooldown(60);
    } catch (err) {
      setError(err instanceof Error ? err.message : '验证码发送失败，请重试');
    } finally {
      setBusy(false);
    }
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
      } else if (claimMode) {
        // 体验账号转正：原地升级（user_id 不变），成功后新 token 覆盖本地凭证
        await claimAccount(email.trim(), password, confirmPassword, inviteCode, emailCode);
      } else {
        if (confirmPassword !== password) {
          setError('两次输入的密码不一致');
          return;
        }
        await register(email.trim(), password, confirmPassword, inviteCode, emailCode);
      }
      onLoggedIn();
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败，请重试');
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  const inputCls =
    'w-full rounded-lg border border-ct-border bg-ct-bg px-3 py-2 text-ct-text outline-none focus:border-ct-accent';

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onClose}
    >
      <div
        className="relative max-h-[90vh] w-full max-w-sm overflow-y-auto rounded-2xl border border-ct-border bg-ct-panel p-8 shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭"
          className="absolute right-4 top-3 text-xl leading-none text-ct-muted hover:text-ct-text"
        >
          ×
        </button>

        <h2 className="text-center text-xl font-medium text-ct-text">
          {mode === 'login' ? '登录 Code Tutor'
            : mode === 'register' ? (claimMode ? '注册正式账号' : '注册 Code Tutor')
            : '找回密码'}
        </h2>
        <p className="mt-2 text-center text-sm text-ct-muted">
          {mode === 'login' && '用邮箱继续你的算法练习'}
          {mode === 'register' && (claimMode
            ? '注册后体验额度重新开启，你的做题记录会完整保留'
            : '注册需要邀请码，可向管理员获取')}
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
            {mode === 'register' && emailVerification && (
              <div>
                <label className="mb-1 block text-sm text-ct-muted">邮箱验证码</label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    required
                    value={emailCode}
                    onChange={(e) => setEmailCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                    placeholder="6 位数字"
                    className={`${inputCls} flex-1 font-mono tracking-widest`}
                    autoComplete="one-time-code"
                  />
                  <button
                    type="button"
                    onClick={handleSendRegisterCode}
                    disabled={!email.includes('@') || sendCooldown > 0 || busy}
                    className="shrink-0 rounded-lg border border-ct-border bg-ct-surface px-3 py-2 text-sm text-ct-text transition hover:bg-ct-hover disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    {sendCooldown > 0 ? `${sendCooldown}s 后重发` : '发送验证码'}
                  </button>
                </div>
                <p className="mt-1 text-xs text-ct-muted">验证码 15 分钟内有效，请查收邮箱（含垃圾箱）</p>
              </div>
            )}
            {mode === 'register' && (
              <div>
                <label className="mb-1 block text-sm text-ct-muted">邀请码</label>
                {publicInvite ? (
                  <>
                    <input
                      type="text"
                      required
                      disabled
                      value={inviteCode}
                      placeholder="向管理员获取"
                      className={`${inputCls} font-mono tracking-widest uppercase cursor-not-allowed opacity-70`}
                      autoComplete="off"
                    />
                    <p className="mt-1 text-xs text-ct-muted">
                      注册码已自动填入，直接点击「注册并登录」即可
                    </p>
                  </>
                ) : (
                  <input
                    type="text"
                    required
                    value={inviteCode}
                    onChange={(e) => setInviteCode(e.target.value)}
                    placeholder="向管理员获取"
                    className={`${inputCls} font-mono tracking-widest uppercase`}
                    autoComplete="off"
                  />
                )}
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

        {/* 服务条款弹窗（嵌套，z 更高；点击遮罩只关条款不关登录弹窗） */}
        {showTerms && (
          <div
            className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 px-4"
            onClick={(e) => { e.stopPropagation(); setShowTerms(false); }}
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
