import { useEffect, useRef } from 'react';

/** 从进度消息里提取状态图标：✅=完成 ❌⚠️=异常 🚀⏳📝=过程（其余默认过程） */
function stepKind(msg: string): 'done' | 'error' | 'step' {
  if (msg.startsWith('✅') || msg.startsWith('♻️')) return 'done';
  if (msg.startsWith('❌') || msg.startsWith('⚠️')) return 'error';
  return 'step';
}

export default function LoadingScreen({
  progressMsgs, errorMsg, onRetry,
}: {
  progressMsgs: string[];
  errorMsg?: string;
  onRetry: () => void;
}) {
  // hooks 必须在 early return 之前调用（loading ↔ error 切换时保持调用顺序稳定）
  const listRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // jsdom（测试环境）没有实现 Element.scrollTo，用可选调用兜底
    listRef.current?.scrollTo?.({ top: listRef.current.scrollHeight, behavior: 'smooth' });
  }, [progressMsgs.length]);

  // 错误态：友好卡片 —— 明确告诉用户「不是你的问题」+ 给出下一步选项，
  // 替代旧版生硬的「出错了 / 生成超时，请重试」大字报（用户 2026-09-06 反馈）。
  if (errorMsg) {
    return (
      <div className="flex h-screen items-center justify-center p-4">
        <div className="w-full max-w-md rounded-2xl border border-ct-border bg-ct-surface px-8 py-8 text-center shadow-sm">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-full bg-ct-warn-bg text-2xl">
            🤖
          </div>
          <p className="text-base font-semibold text-ct-text">出题遇到了点小状况</p>
          <p className="mt-2 text-sm leading-relaxed text-ct-muted">{errorMsg}</p>
          <button onClick={onRetry}
            className="mt-6 rounded-lg bg-ct-accent px-6 py-2 text-sm font-medium text-white transition hover:opacity-90">
            重新出题
          </button>
          <p className="mt-4 text-[11px] text-ct-muted/70">返回主页可重新开始，或先从题库选一道题练习</p>
        </div>
      </div>
    );
  }

  // ── 出题中：滚动步骤条 ──
  // 每条后端进度消息一行：已完成的打勾淡化、最新一步高亮转圈；
  // 容器限高自动滚到底，消息多时呈「日志滚动」效果，不打扰等待。
  return (
    <div className="flex h-screen flex-col items-center justify-center gap-5 p-4">
      <div className="flex items-center gap-2">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
        <p className="text-lg text-ct-text">正在为你出题，请稍候...</p>
      </div>

      {progressMsgs.length > 0 && (
        <div
          ref={listRef}
          className="max-h-72 w-full max-w-md space-y-1.5 overflow-y-auto rounded-xl border border-ct-border bg-ct-surface px-4 py-3 shadow-sm"
        >
          {progressMsgs.map((msg, i) => {
            const kind = stepKind(msg);
            const isLast = i === progressMsgs.length - 1;
            return (
              <div key={i} className="flex items-start gap-2.5">
                {/* 状态图标：完成 ✓ / 异常 ! / 当前步转圈 / 历史过程点 */}
                <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center">
                  {kind === 'done' ? (
                    <span className="text-xs font-bold text-ct-success">✓</span>
                  ) : kind === 'error' ? (
                    <span className="text-xs font-bold text-ct-warn">!</span>
                  ) : isLast ? (
                    <span className="h-3 w-3 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
                  ) : (
                    <span className="h-1.5 w-1.5 rounded-full bg-ct-border" />
                  )}
                </span>
                <p className={'text-sm leading-relaxed ' + (isLast ? 'font-medium text-ct-text' : kind === 'done' ? 'text-ct-muted/70' : 'text-ct-muted')}>
                  {msg}
                </p>
              </div>
            );
          })}
        </div>
      )}

      <p className="text-xs text-ct-muted/60">通常 1~2 分钟内完成 · 可随时返回重新开始</p>
    </div>
  );
}
