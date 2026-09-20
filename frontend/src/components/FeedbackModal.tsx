import { useCallback, useEffect, useRef, useState } from 'react';
import { submitFeedback, type FeedbackCategory } from '../api/feedback';

/** 意见反馈弹窗（docs/feedback-feature-plan.md Phase 3）。
 *
 * - 入口：WelcomeScreen 顶栏「💬 反馈」（与 ⚙️ 设置 / 退出登录 并排）
 * - 分类四选一（白名单与后端 feedback.py CATEGORIES 一致）
 * - 正文 5–2000 字：前端先拦一道给即时反馈，后端复校（纯空白串后端也会拒）
 * - 上下文（screen / problem_id / session_id）由 props 传入，不暴露给用户
 * - 提交成功就地提示 + 自动关闭：不跳转、不刷新、不清场
 * - 提交失败保留输入内容（用户不用重打一遍）；429 限频文案直接展示后端 detail
 * - 遮罩点击 / Esc / 右上角 × 均可关闭；**提交中禁止关闭**，防半途丢弃
 */

const MIN_LEN = 5;
const MAX_LEN = 2000;
const DONE_CLOSE_MS = 1500;

const CATEGORY_OPTIONS: { id: FeedbackCategory; label: string; hint: string }[] = [
  { id: 'bug', label: '🐞 问题反馈', hint: '报错、卡住、点了没反应' },
  { id: 'experience', label: '💡 体验建议', hint: '希望增加或改进的功能' },
  { id: 'content', label: '📝 题目内容', hint: '题面、用例、参考解有误' },
  { id: 'other', label: '💬 其他', hint: '想说什么都行' },
];

export default function FeedbackModal({
  open,
  onClose,
  screen = 'welcome',
  problemId = null,
  sessionId,
}: {
  open: boolean;
  onClose: () => void;
  /** 提交时所在界面标识（本项目无 URL 路由，用 screen 状态机值代替 path） */
  screen?: string;
  problemId?: number | null;
  sessionId?: string;
}) {
  const [category, setCategory] = useState<FeedbackCategory>('bug');
  const [content, setContent] = useState('');
  const [contact, setContact] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 每次打开重置：避免上次的残留正文 / 成功态串场
  useEffect(() => {
    if (!open) return;
    setCategory('bug');
    setContent('');
    setContact('');
    setBusy(false);
    setError('');
    setDone(false);
  }, [open]);

  // 卸载时清掉自动关闭定时器，避免对已卸载组件 setState
  useEffect(() => () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
  }, []);

  const requestClose = useCallback(() => {
    if (busy) return; // 提交中 / 成功提示中不许关
    if (closeTimer.current) clearTimeout(closeTimer.current);
    onClose();
  }, [busy, onClose]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') requestClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, requestClose]);

  if (!open) return null;

  const trimmed = content.trim();
  const tooShort = trimmed.length < MIN_LEN;

  const handleSubmit = async () => {
    if (tooShort || busy) return;
    setBusy(true);
    setError('');
    try {
      await submitFeedback({
        category,
        content: trimmed,
        contact: contact.trim() || undefined,
        screen,
        problem_id: problemId ?? undefined,
        session_id: sessionId || undefined,
      });
      setDone(true);
      // 保持 busy=true 直到自动关闭：成功提示期间不接受再次提交 / 手动关闭
      closeTimer.current = setTimeout(() => {
        setBusy(false);
        onClose();
      }, DONE_CLOSE_MS);
    } catch (e) {
      setError(e instanceof Error ? e.message : '提交失败，请稍后再试');
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={requestClose}
    >
      <div
        className="relative w-full max-w-md rounded-2xl border border-ct-border bg-ct-panel p-6 shadow-lg"
        onClick={(e) => e.stopPropagation()}
        data-testid="feedback-modal"
      >
        <button
          type="button"
          aria-label="关闭"
          onClick={requestClose}
          className="absolute right-3 top-3 rounded px-1.5 text-ct-muted transition hover:text-ct-text"
        >
          ✕
        </button>

        <h2 className="text-base font-medium text-ct-text">💬 意见反馈</h2>
        <p className="mt-1 text-xs text-ct-muted">
          遇到问题、想加功能、发现题目有误，都可以在这里说。
        </p>

        {done ? (
          <div className="mt-5 rounded-lg border border-ct-success/40 bg-ct-success-bg px-4 py-6 text-center">
            <p className="text-sm font-medium text-ct-success" data-testid="feedback-done">
              已收到，感谢你的反馈！
            </p>
          </div>
        ) : (
          <>
            <div className="mt-4 grid grid-cols-2 gap-2">
              {CATEGORY_OPTIONS.map((o) => (
                <button
                  key={o.id}
                  type="button"
                  onClick={() => setCategory(o.id)}
                  aria-pressed={category === o.id}
                  className={`rounded-lg border px-3 py-2 text-left transition ${
                    category === o.id
                      ? 'border-ct-accent bg-ct-accent/10'
                      : 'border-ct-border hover:border-ct-accent/50'
                  }`}
                >
                  <span className="block text-xs font-medium text-ct-text">{o.label}</span>
                  <span className="mt-0.5 block text-[10px] text-ct-muted">{o.hint}</span>
                </button>
              ))}
            </div>

            <div className="mt-4">
              <label className="block text-xs text-ct-muted" htmlFor="feedback-content">
                反馈内容
              </label>
              <textarea
                id="feedback-content"
                value={content}
                maxLength={MAX_LEN}
                rows={5}
                onChange={(e) => setContent(e.target.value)}
                placeholder="尽量说清楚：你做了什么、期望什么、实际发生了什么（至少 5 个字）"
                className="mt-1 w-full resize-y rounded-lg border border-ct-border bg-ct-input px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent"
              />
              <span className="mt-1 block text-right text-[10px] text-ct-muted">
                {content.length}/{MAX_LEN}
              </span>
            </div>

            <div className="mt-1">
              <label className="block text-xs text-ct-muted" htmlFor="feedback-contact">
                联系方式（可选）
              </label>
              <input
                id="feedback-contact"
                type="text"
                value={contact}
                maxLength={120}
                onChange={(e) => setContact(e.target.value)}
                placeholder="留个邮箱 / 微信，方便我们回复你"
                className="mt-1 w-full rounded-lg border border-ct-border bg-ct-input px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent"
              />
            </div>

            {error && (
              <p
                className="mt-3 rounded-lg border border-ct-error/40 bg-ct-error-bg px-3 py-2 text-xs text-ct-error"
                data-testid="feedback-error"
              >
                {error}
              </p>
            )}

            <div className="mt-4 flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={requestClose}
                disabled={busy}
                className="rounded-lg border border-ct-border px-4 py-1.5 text-xs text-ct-muted transition hover:text-ct-text disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="button"
                onClick={handleSubmit}
                disabled={busy || tooShort}
                data-testid="feedback-submit"
                className="rounded-lg bg-ct-accent px-4 py-1.5 text-xs font-medium text-white transition hover:opacity-90 disabled:opacity-50"
              >
                {busy ? '提交中…' : '提交反馈'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
