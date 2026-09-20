/**
 * 意见反馈弹窗 + 主页入口（docs/feedback-feature-plan.md Phase 3 / Phase 5）。
 *
 * 覆盖：
 * - 提交成功：请求体带上分类 / 正文 / 上下文 screen，随后展示成功态
 * - 正文不足 5 字：提交按钮禁用（前端先拦一道）
 * - 429 限频：后端 detail 行内展示、输入内容保留、不关闭弹窗
 * - Esc / 遮罩关闭；点弹窗本体不关闭；open=false 不渲染
 * - 成功后自动关闭
 * - WelcomeScreen 顶栏「反馈」按钮能拉起弹窗（入口接线回归）
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FeedbackModal from '../src/components/FeedbackModal';
import WelcomeScreen from '../src/components/WelcomeScreen';
import AdminPanel from '../src/components/AdminPanel';

/** 装一个可断言的 fetch，返回收集到的调用记录。 */
function mockFetch(status: number, body: unknown) {
  const calls: { url: string; init: any }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, init: any) => {
      calls.push({ url, init });
      return Promise.resolve(
        new Response(JSON.stringify(body), {
          status,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
    }),
  );
  return calls;
}

describe('FeedbackModal', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    cleanup();
    localStorage.clear();
  });

  it('提交成功：请求体带分类 / 正文 / screen，并展示成功态', async () => {
    const calls = mockFetch(200, { ok: true, id: 12 });
    render(<FeedbackModal open onClose={() => {}} screen="welcome" />);

    await userEvent.click(screen.getByText('📝 题目内容'));
    await userEvent.type(screen.getByLabelText('反馈内容'), '这道题的描述有个错别字');
    await userEvent.click(screen.getByTestId('feedback-submit'));

    await waitFor(() => expect(screen.getByTestId('feedback-done')).toBeInTheDocument());

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain('/feedback');
    const payload = JSON.parse(calls[0].init.body);
    expect(payload.category).toBe('content');
    expect(payload.content).toBe('这道题的描述有个错别字');
    expect(payload.screen).toBe('welcome');
  });

  it('正文不足 5 字时提交按钮禁用（含纯空白）', async () => {
    mockFetch(200, { ok: true, id: 1 });
    render(<FeedbackModal open onClose={() => {}} />);

    const btn = screen.getByTestId('feedback-submit');
    expect(btn).toBeDisabled();

    await userEvent.type(screen.getByLabelText('反馈内容'), '四个字呀');
    expect(btn).toBeDisabled();

    await userEvent.type(screen.getByLabelText('反馈内容'), '好');
    expect(btn).toBeEnabled();
  });

  it('429 限频：行内展示后端 detail、保留输入、不关闭弹窗', async () => {
    mockFetch(429, { detail: '操作过于频繁，请稍后再试' });
    const onClose = vi.fn();
    render(<FeedbackModal open onClose={onClose} />);

    await userEvent.type(screen.getByLabelText('反馈内容'), '这是一条会被限频的反馈');
    await userEvent.click(screen.getByTestId('feedback-submit'));

    await waitFor(() =>
      expect(screen.getByTestId('feedback-error')).toHaveTextContent('操作过于频繁'),
    );
    expect(screen.getByLabelText('反馈内容')).toHaveValue('这是一条会被限频的反馈');
    expect(screen.queryByTestId('feedback-done')).not.toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('Esc 关闭；点弹窗本体不关闭；open=false 不渲染', async () => {
    const onClose = vi.fn();
    const { unmount } = render(<FeedbackModal open onClose={onClose} />);

    await userEvent.click(screen.getByTestId('feedback-modal'));
    expect(onClose).not.toHaveBeenCalled();

    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);

    unmount();
    render(<FeedbackModal open={false} onClose={() => {}} />);
    expect(screen.queryByTestId('feedback-modal')).not.toBeInTheDocument();
  });

  it('成功后自动关闭', async () => {
    mockFetch(200, { ok: true, id: 3 });
    const onClose = vi.fn();
    render(<FeedbackModal open onClose={onClose} />);

    await userEvent.type(screen.getByLabelText('反馈内容'), '自动关闭验证内容');
    await userEvent.click(screen.getByTestId('feedback-submit'));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1), { timeout: 3000 });
  });
});

describe('WelcomeScreen 反馈入口', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    cleanup();
    localStorage.clear();
  });

  it('顶栏「反馈」可拉起弹窗（即使父级没传 onOpenSettings/onLogout）', async () => {
    render(<WelcomeScreen onStart={() => {}} />);

    // 用 role+name 而非 getByText：按钮文案会随 UI 调整（曾带 emoji），role 查询更稳
    const entry = screen.getByRole('button', { name: /^反馈$/ });
    expect(entry).toBeInTheDocument();
    expect(screen.queryByTestId('feedback-modal')).not.toBeInTheDocument();

    await userEvent.click(entry);
    expect(screen.getByTestId('feedback-modal')).toBeInTheDocument();
  });
});

describe('AdminPanel 用户反馈只读区', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });
  afterEach(() => {
    vi.restoreAllMocks();
    cleanup();
    localStorage.clear();
  });

  function stubAdminFetch() {
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        urls.push(url);
        const body = url.includes('/admin/feedback')
          ? {
              items: [
                {
                  id: 2, user_id: 9, user_email: 'b@x.com', category: 'bug',
                  content: '提交按钮点了没反应', contact: null, screen: 'welcome',
                  problem_id: null, session_id: null, user_agent: 'UA/1.0',
                  created_at: '2026-09-20 09:40:00.123456',
                },
                {
                  id: 1, user_id: 8, user_email: 'a@x.com', category: 'experience',
                  content: '希望支持 JS', contact: 'wx: abc', screen: null,
                  problem_id: 42, session_id: 's1', user_agent: 'UA/1.0',
                  created_at: '2026-09-20 09:30:00',
                },
              ],
              total: 2,
              counts: { bug: 1, experience: 1 },
            }
          : { problems: [] };
        return Promise.resolve(
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }),
        );
      }),
    );
    return urls;
  }

  it('展示列表 / 分类计数 / 展开详情，且没有任何编辑删除入口', async () => {
    const urls = stubAdminFetch();
    render(<AdminPanel onClose={() => {}} />);

    await userEvent.click(screen.getByText('📥 用户反馈'));
    await waitFor(() => expect(screen.getByTestId('admin-feedback')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText('提交按钮点了没反应')).toBeInTheDocument());

    expect(urls.some((u) => u.includes('/admin/feedback'))).toBe(true);
    expect(screen.getByText('共 2 条')).toBeInTheDocument();

    // 计数角标是「全量口径」：切到某分类也能看到其他分类各有多少
    expect(screen.getByText('全部 2')).toBeInTheDocument();
    expect(screen.getByText('问题 1')).toBeInTheDocument();
    expect(screen.getByText('体验 1')).toBeInTheDocument();
    expect(screen.getByText('题目 0')).toBeInTheDocument();

    // 时间取到分钟（后端带微秒，slice(0,16) 后应为 'YYYY-MM-DD HH:MM'）
    expect(screen.getByText('2026-09-20 09:40')).toBeInTheDocument();

    // 点行展开完整正文 + 联系方式 / 会话 / UA
    await userEvent.click(screen.getByText('希望支持 JS'));
    await waitFor(() => expect(screen.getByText('wx: abc')).toBeInTheDocument());
    expect(screen.getByText('s1')).toBeInTheDocument();

    // 只读口径：不应出现任何变更类入口
    expect(screen.queryByText('编辑')).not.toBeInTheDocument();
    expect(screen.queryByText('删除')).not.toBeInTheDocument();
  });

  it('切分类筛选会把 offset 拉回第一页并带上 category 参数', async () => {
    const urls = stubAdminFetch();
    render(<AdminPanel onClose={() => {}} />);

    await userEvent.click(screen.getByText('📥 用户反馈'));
    await waitFor(() => expect(screen.getByTestId('admin-feedback')).toBeInTheDocument());

    await userEvent.click(screen.getByText('问题 1'));
    await waitFor(() =>
      expect(urls.some((u) => u.includes('/admin/feedback') && u.includes('category=bug'))).toBe(
        true,
      ),
    );
    const last = urls.filter((u) => u.includes('/admin/feedback')).pop()!;
    expect(last).toContain('offset=0');
  });
});
