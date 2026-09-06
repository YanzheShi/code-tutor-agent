/**
 * 前端错误上报 + 公告横幅测试（docs/monitoring-alerts-design.md §14.2/§15.3）。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import AnnouncementsBanner from '../src/components/AnnouncementsBanner';

// ── useErrorReport：全局兜底安装 + 上报行为 ──────────────────

describe('useErrorReport', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('installs global handlers idempotently and reports unhandled rejection', async () => {
    const calls: any[] = [];
    vi.stubGlobal('fetch', vi.fn((url: string, init: any) => {
      calls.push({ url, body: init?.body });
      return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    }));

    const { useErrorReport } = await import('../src/hooks/useErrorReport');
    useErrorReport();
    useErrorReport(); // 幂等：重复调用不再叠加监听器

    const before = calls.length;
    const ev = new Event('unhandledrejection') as PromiseRejectionEvent;
    (ev as any).reason = new Error('boom-for-test');
    window.dispatchEvent(ev);

    await waitFor(() => expect(calls.length).toBeGreaterThan(before));
    const last = calls[calls.length - 1];
    expect(last.url).toContain('/client/errors');
    const payload = JSON.parse(last.body);
    expect(payload.source).toBe('unhandledrejection');
    expect(payload.message).toContain('boom-for-test');
  });

  it('dedupes same fingerprint within 60s window', async () => {
    const calls: any[] = [];
    vi.stubGlobal('fetch', vi.fn((url: string, init: any) => {
      calls.push(init?.body);
      return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    }));

    const { reportError } = await import('../src/hooks/useErrorReport');
    reportError('test_src', 'same-message');
    reportError('test_src', 'same-message'); // 指纹相同 → 去重
    reportError('test_src', 'different-message');
    // 第 1 条可能走 sendBeacon（jsdom 无实现则回退 fetch），去重后 fetch 上报数 ≤2
    expect(calls.length).toBeLessThanOrEqual(2);
  });
});

// ── AnnouncementsBanner ─────────────────────────────────────

function mockAuth() {
  localStorage.setItem(
    'code-tutor:auth',
    JSON.stringify({ token: 't', user: { id: 1, email: 'a@b.c', role: 'admin' } }),
  );
}

describe('AnnouncementsBanner', () => {
  beforeEach(() => {
    localStorage.clear();
    mockAuth();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('renders nothing when no announcements', async () => {
    vi.stubGlobal('fetch', vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify({ announcements: [] }), { status: 200 })),
    ));
    const { container } = render(<AnnouncementsBanner />);
    await waitFor(() => expect(container.querySelector('[data-testid="announcements-banner"]')).toBeNull());
  });

  it('shows highest-level announcement and blocks dismissing critical monitor banner', async () => {
    vi.stubGlobal('fetch', vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify({
        announcements: [
          { id: 1, level: 'info', title: '维护通知', content: '', source: 'admin' },
          { id: 2, level: 'critical', title: '判题服务不稳定', content: '请稍后重试', source: 'monitor' },
        ],
      }), { status: 200 })),
    ));
    render(<AnnouncementsBanner />);
    await waitFor(() => expect(screen.getByText('判题服务不稳定')).toBeInTheDocument());
    // 只显示最高级别的一条（critical），info 被折叠
    expect(screen.queryByText('维护通知')).toBeNull();
    // monitor + critical 不可关闭
    expect(screen.queryByLabelText('关闭公告')).toBeNull();
  });

  it('non-critical banner is dismissible', async () => {
    vi.stubGlobal('fetch', vi.fn(() =>
      Promise.resolve(new Response(JSON.stringify({
        announcements: [
          { id: 3, level: 'warning', title: '磁盘空间不足', content: '', source: 'monitor' },
        ],
      }), { status: 200 })),
    ));
    render(<AnnouncementsBanner />);
    await waitFor(() => expect(screen.getByText('磁盘空间不足')).toBeInTheDocument());
    expect(screen.getByLabelText('关闭公告')).toBeInTheDocument();
  });
});
