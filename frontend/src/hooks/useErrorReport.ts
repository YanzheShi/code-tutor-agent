/**
 * 前端错误上报（docs/monitoring-alerts-design.md §14.2）。
 *
 * 全局兜底：window.onerror + unhandledrejection（采样 + 指纹去重 + 限流）；
 * 主动上报：error 屏 / SSE error / 资源加载失败等关键路径调用 reportError()。
 *
 * 防风暴三闸（后端 /client/errors 是公开端点，必须克制）：
 *  1. 同一指纹 60s 内只报 1 次（指纹 = source + message 前 120 字符）；
 *  2. 单页面生命周期上限 20 条/小时，超限静默丢弃；
 *  3. 优先 navigator.sendBeacon（页面卸载也不丢），失败回退 fetch(keepalive)。
 * 上报失败一律静默——错误上报本身绝不能再产生错误。
 */
import { API_BASE } from '../api/config';

const FINGERPRINT_WINDOW_MS = 60_000;
const MAX_PER_HOUR = 20;
const MSG_TRUNC = 120;

let installed = false;
let lastFingerprint = '';
let lastFingerprintAt = 0;
let hourWindowStart = 0;
let hourCount = 0;

function underLimit(): boolean {
  const now = Date.now();
  if (now - hourWindowStart > 3_600_000) {
    hourWindowStart = now;
    hourCount = 0;
  }
  hourCount += 1;
  return hourCount <= MAX_PER_HOUR;
}

function reportError(
  source: string,
  message: string,
  extra?: { sessionId?: string; path?: string; problemId?: number },
): void {
  try {
    const msg = (message || 'unknown').slice(0, MSG_TRUNC);
    const fingerprint = `${source}:${msg}`;
    const now = Date.now();
    if (fingerprint === lastFingerprint && now - lastFingerprintAt < FINGERPRINT_WINDOW_MS) return;
    if (!underLimit()) return;
    lastFingerprint = fingerprint;
    lastFingerprintAt = now;

    const payload = JSON.stringify({
      source: source.slice(0, 64),
      message: msg,
      session_id: (extra?.sessionId || '').slice(0, 64) || undefined,
      path: (extra?.path || window.location?.pathname || '').slice(0, 200) || undefined,
      problem_id: extra?.problemId || undefined,
    });
    const url = `${API_BASE}/client/errors`;
    if (typeof navigator !== 'undefined' && navigator.sendBeacon) {
      // sendBeacon 只能发 Blob/Form，Content-Type 用 text/plain 规避 CORS 预检
      const blob = new Blob([payload], { type: 'text/plain;charset=UTF-8' });
      if (navigator.sendBeacon(url, blob)) return;
    }
    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: payload,
      keepalive: true,
    }).catch(() => {});
  } catch {
    // 上报失败静默
  }
}

/**
 * 安装全局错误兜底（幂等：重复调用只装一次）。
 * 在 App.tsx 顶层调用一次即可。
 */
export function useErrorReport(): void {
  if (installed || typeof window === 'undefined') return;
  installed = true;

  window.addEventListener('error', (event) => {
    // 资源加载失败（img/script/css）没有 error.message，单独归类
    const target = event.target as HTMLElement | null;
    if (target && target !== (event.currentTarget as HTMLElement) &&
        (target.tagName === 'IMG' || target.tagName === 'SCRIPT' || target.tagName === 'LINK')) {
      reportError('resource', `${target.tagName} load failed: ${(target as any).src || ''}`);
      return;
    }
    reportError('window.onerror', `${event.message} @ ${event.filename}:${event.lineno}`);
  });

  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    const msg = reason instanceof Error ? `${reason.message}\n${reason.stack ?? ''}` : String(reason);
    reportError('unhandledrejection', msg.split('\n')[0] || 'unknown rejection');
  });
}

export { reportError };
