import { StrictMode, type ReactNode } from 'react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useProblemTimer, formatElapsed } from '../src/hooks/useProblemTimer';

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useProblemTimer', () => {
  it('新题自动开始，并按秒推进', () => {
    const { result } = renderHook(() => useProblemTimer('s1', 101, false));
    expect(result.current.active).toBe(true);
    expect(result.current.running).toBe(true);
    expect(result.current.elapsedMs).toBe(0);

    advance(3000);
    expect(result.current.elapsedMs).toBe(3000);
  });

  it('暂停后冻结，继续后接着累计', () => {
    const { result } = renderHook(() => useProblemTimer('s1', 101, false));
    advance(2500);
    act(() => result.current.pause());

    expect(result.current.running).toBe(false);
    expect(result.current.elapsedMs).toBe(2500);

    advance(5000);
    expect(result.current.elapsedMs).toBe(2500);

    act(() => result.current.start());
    advance(1000);
    expect(result.current.running).toBe(true);
    expect(result.current.elapsedMs).toBe(3500);
  });

  it('重置归零并暂停', () => {
    const { result } = renderHook(() => useProblemTimer('s1', 101, false));
    advance(3000);
    act(() => result.current.reset());

    expect(result.current.elapsedMs).toBe(0);
    expect(result.current.running).toBe(false);

    advance(3000);
    expect(result.current.elapsedMs).toBe(0);
  });

  it('AC 自动暂停，手动继续仍可用', () => {
    const { result, rerender } = renderHook(
      ({ ac }: { ac: boolean }) => useProblemTimer('s1', 101, ac),
      { initialProps: { ac: false } },
    );
    advance(2000);
    rerender({ ac: true });

    expect(result.current.running).toBe(false);
    expect(result.current.elapsedMs).toBe(2000);

    advance(3000);
    expect(result.current.elapsedMs).toBe(2000);

    act(() => result.current.start());
    advance(1000);
    expect(result.current.running).toBe(true);
    expect(result.current.elapsedMs).toBe(3000);
  });

  it('刷新（sessionStorage 保留）后继续计时', () => {
    const first = renderHook(() => useProblemTimer('s1', 101, false));
    advance(3000);
    first.unmount();

    const second = renderHook(() => useProblemTimer('s1', 101, false));
    expect(second.result.current.running).toBe(true);
    expect(second.result.current.elapsedMs).toBe(3000);

    advance(1000);
    expect(second.result.current.elapsedMs).toBe(4000);
  });

  it('关页（sessionStorage 清空）后恢复为暂停态，累计时长保留', () => {
    const first = renderHook(() => useProblemTimer('s1', 101, false));
    advance(3000);
    first.unmount();

    sessionStorage.clear(); // 关闭标签页
    const second = renderHook(() => useProblemTimer('s1', 101, false));
    expect(second.result.current.running).toBe(false);
    expect(second.result.current.elapsedMs).toBe(3000);

    advance(3000);
    expect(second.result.current.elapsedMs).toBe(3000);
  });

  it('换题后从 00:00 自动开始，旧题累计不串', () => {
    const { result, rerender } = renderHook(
      ({ pid }: { pid: number }) => useProblemTimer('s1', pid, false),
      { initialProps: { pid: 101 } },
    );
    advance(3000);
    rerender({ pid: 102 });

    expect(result.current.running).toBe(true);
    expect(result.current.elapsedMs).toBe(0);

    advance(1000);
    expect(result.current.elapsedMs).toBe(1000);
  });

  it('StrictMode 双执行不会把计时清零', () => {
    const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;
    const { result } = renderHook(() => useProblemTimer('strict-s', 301, false), { wrapper });

    expect(result.current.running).toBe(true);
    advance(3000);
    expect(result.current.elapsedMs).toBe(3000);
  });

  it('无题时不启动，控制按钮为空操作', () => {
    const { result } = renderHook(() => useProblemTimer(null, null, false));
    expect(result.current.active).toBe(false);
    expect(result.current.running).toBe(false);

    act(() => result.current.start());
    expect(result.current.running).toBe(false);
  });
});

describe('formatElapsed', () => {
  it('按 MM:SS 展示，超过 1 小时带小时位', () => {
    expect(formatElapsed(0)).toBe('00:00');
    expect(formatElapsed(65_000)).toBe('01:05');
    expect(formatElapsed(3_661_000)).toBe('1:01:01');
  });

  it('负数/非法值兜底为 00:00', () => {
    expect(formatElapsed(-1)).toBe('00:00');
    expect(formatElapsed(Number.NaN)).toBe('00:00');
  });
});
