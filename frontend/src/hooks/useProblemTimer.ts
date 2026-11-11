import { useCallback, useEffect, useRef, useState } from 'react';
import { userKey } from '../api/auth';

/**
 * 本题计时器。
 *
 * 两类存储各管一件事：
 *  - localStorage 存「累计毫秒」：每秒落盘一次，关页/刷新都不丢。
 *  - sessionStorage 只存「本标签页是否在跑」：刷新后仍在（继续计时），
 *    关闭标签页即清空（恢复为暂停态，满足「关页停止计时」）。
 * 二者按 uid + sessionId + problemId 隔离，换题/换账号互不串。
 */
const STORE_PREFIX = 'code-tutor:timer:';
const RUN_PREFIX = 'code-tutor:timer-run:';

function timerKeys(sessionId: string, problemId: number) {
  const uid = userKey();
  return {
    storeKey: `${STORE_PREFIX}${uid}:${sessionId}:${problemId}`,
    runKey: `${RUN_PREFIX}${uid}:${sessionId}:${problemId}`,
  };
}

function readAccumulated(key: string): number | null {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return null;
    const parsed = JSON.parse(raw) as { accumulatedMs?: unknown };
    const ms = parsed?.accumulatedMs;
    return typeof ms === 'number' && Number.isFinite(ms) ? Math.max(0, ms) : 0;
  } catch {
    return null;
  }
}

function writeAccumulated(key: string, ms: number) {
  try {
    localStorage.setItem(key, JSON.stringify({ accumulatedMs: Math.max(0, Math.round(ms)) }));
  } catch {
    /* 隐私模式/配额满：计时仍可用，只是不落盘 */
  }
}

function readRunMarker(key: string): boolean {
  try {
    return sessionStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

function writeRunMarker(key: string, running: boolean) {
  try {
    sessionStorage.setItem(key, running ? '1' : '0');
  } catch {
    /* ignore */
  }
}

/** 毫秒 → `MM:SS`（≥1 小时为 `H:MM:SS`），供计时框展示。 */
export function formatElapsed(ms: number): string {
  const total = Number.isFinite(ms) ? Math.max(0, Math.floor(ms / 1000)) : 0;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export type ProblemTimer = {
  elapsedMs: number;
  running: boolean;
  /** 当前是否有题（无题时浮层不渲染）。 */
  active: boolean;
  start: () => void;
  pause: () => void;
  reset: () => void;
};

/**
 * @param sessionId 会话 id；null 表示未进入做题页
 * @param problemId 当前题 id；变化即视为新题
 * @param isAC      最近一次判题是否 AC；变 true 时自动暂停
 */
export function useProblemTimer(
  sessionId: string | null,
  problemId: number | null,
  isAC: boolean,
): ProblemTimer {
  const [elapsedMs, setElapsedMs] = useState(0);
  const [running, setRunning] = useState(false);
  // 已结算的累计时长（不含当前计时段）与当前计时段起点
  const baseRef = useRef(0);
  const startAtRef = useRef<number | null>(null);
  const keysRef = useRef<{ storeKey: string; runKey: string } | null>(null);

  const elapsedNow = useCallback(() => {
    const startedAt = startAtRef.current;
    return baseRef.current + (startedAt == null ? 0 : Date.now() - startedAt);
  }, []);

  const persistNow = useCallback(() => {
    const keys = keysRef.current;
    if (keys) writeAccumulated(keys.storeKey, elapsedNow());
  }, [elapsedNow]);

  const pause = useCallback(() => {
    if (startAtRef.current == null) return;
    const ms = elapsedNow();
    baseRef.current = ms;
    startAtRef.current = null;
    setElapsedMs(ms);
    setRunning(false);
    const keys = keysRef.current;
    if (keys) {
      writeAccumulated(keys.storeKey, ms);
      writeRunMarker(keys.runKey, false);
    }
  }, [elapsedNow]);

  const start = useCallback(() => {
    if (startAtRef.current != null || !keysRef.current) return;
    startAtRef.current = Date.now();
    setRunning(true);
    writeRunMarker(keysRef.current.runKey, true);
    writeAccumulated(keysRef.current.storeKey, baseRef.current);
  }, []);

  const reset = useCallback(() => {
    baseRef.current = 0;
    startAtRef.current = null;
    setElapsedMs(0);
    setRunning(false);
    const keys = keysRef.current;
    if (keys) {
      writeAccumulated(keys.storeKey, 0);
      writeRunMarker(keys.runKey, false);
    }
  }, []);

  // 题目标识变化：新题（无存档）自动开始；有存档则按 sessionStorage 标记决定
  // 继续跑（刷新）还是暂停（关页后重开）。
  useEffect(() => {
    if (!sessionId || problemId == null) {
      keysRef.current = null;
      baseRef.current = 0;
      startAtRef.current = null;
      setElapsedMs(0);
      setRunning(false);
      return;
    }
    const keys = timerKeys(sessionId, problemId);
    keysRef.current = keys;
    const stored = readAccumulated(keys.storeKey);
    if (stored == null) {
      baseRef.current = 0;
      startAtRef.current = Date.now();
      setElapsedMs(0);
      setRunning(true);
      writeAccumulated(keys.storeKey, 0);
      writeRunMarker(keys.runKey, true);
      return;
    }
    const continueRunning = readRunMarker(keys.runKey);
    baseRef.current = stored;
    startAtRef.current = continueRunning ? Date.now() : null;
    setElapsedMs(stored);
    setRunning(continueRunning);
  }, [sessionId, problemId]);

  // 每秒推进显示并落盘（localStorage 只存累计值，关页最多丢 1s，pagehide 再兜底补写）。
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => {
      setElapsedMs(elapsedNow());
      persistNow();
    }, 1000);
    return () => clearInterval(id);
  }, [running, elapsedNow, persistNow]);

  // 刷新/关页前把最后一段补写完整；sessionStorage 标记不在这里动，
  // 交给浏览器：刷新保留 → 续跑，关页清空 → 暂停。
  useEffect(() => {
    const flush = () => {
      if (startAtRef.current != null) persistNow();
    };
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') flush();
    };
    window.addEventListener('pagehide', flush);
    window.addEventListener('beforeunload', flush);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.removeEventListener('pagehide', flush);
      window.removeEventListener('beforeunload', flush);
      document.removeEventListener('visibilitychange', onVisibility);
      flush();
    };
  }, [persistNow]);

  // AC 即停；之后用户手动「继续」不受影响（isAC 不再变化，不会再次触发）。
  useEffect(() => {
    if (isAC) pause();
  }, [isAC, pause]);

  return {
    elapsedMs,
    running,
    active: problemId != null,
    start,
    pause,
    reset,
  };
}
