import { describe, it, expect, vi, beforeEach, beforeAll, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import App from '../src/App';
import { ThemeProvider } from '../src/hooks/useTheme';

// 多租户改造后 App 有登录门禁，且 useSession 的 RESTORE_KEY 在模块导入时
// 就按 userKey() 计算（code-tutor:session:<uid>）——因此 auth 必须在导入前就位。
// vi.hoisted 在所有 import 之前执行，此时 jsdom 环境已就绪。
vi.hoisted(() => {
  localStorage.setItem(
    'code-tutor:auth',
    JSON.stringify({ token: 'test-token', user: { id: 1, email: 't@test.com', role: 'user' } }),
  );
  localStorage.setItem(
    'code-tutor:session:1',
    JSON.stringify({ screen: 'main', sessionId: 's1', mode: 'agent' }),
  );
});

vi.mock('@monaco-editor/react', () => ({
  default: () => <div data-testid="monaco-stub" />,
}));

const DIALOG_STATE = {
  session_id: 's1',
  status: 'dialog',
  mode: 'agent',
  problem: null,
  submissions: [],
  tutor_messages: [],
  hint_level: 0,
  last_verdict: null,
  last_review_payload: null,
  error_message: '',
  progress_messages: [],
};

function mockFetch() {
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    if (u.includes('/auth/me')) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ user: { id: 1, email: 't@test.com', role: 'user' } }),
      } as unknown as Response;
    }
    if (u.includes('/session/') && u.includes('/state')) {
      return { ok: true, status: 200, json: async () => DIALOG_STATE } as unknown as Response;
    }
    if (u.includes('/progress/stream')) {
      // SSE fetch 流：永久挂起不关闭（关闭会被判为「连接已断开」→ error 屏；
      // 组件卸载时 abort 主动退出，不触发 onError）
      return {
        ok: true,
        status: 200,
        body: new ReadableStream<Uint8Array>({ start() { /* pending */ } }),
      } as unknown as Response;
    }
    if (u === '/__edit_trace') {
      return { ok: true, status: 204 } as unknown as Response;
    }
    throw new Error('unexpected fetch: ' + u);
  }));
}

beforeAll(() => {
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
});

beforeEach(() => {
  mockFetch();
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__ct_editor;
  vi.unstubAllGlobals();
});

describe('back to home', () => {
  it('renders WelcomeScreen after clicking 回到主页', async () => {
    render(
      <ThemeProvider><App /></ThemeProvider>,
    );

    await waitFor(() => expect(screen.getByTitle('回到主页')).toBeInTheDocument(), { timeout: 3000 });

    fireEvent.click(screen.getByTitle('回到主页'));

    await waitFor(() => expect(screen.getByText('开始对话')).toBeInTheDocument(), { timeout: 3000 });
  });

  it('does not crash when edit-trace detaches a Monaco-style editor (unbound dispose regression)', async () => {
    // Monaco 的 onDidChangeModelContent 返回的 disposable，dispose 是原型方法、依赖 this。
    // 曾因 `return sub.dispose`（解绑）在回主页的 effect cleanup 中直接调用，
    // 导致 this === undefined → TypeError → React 卸载整棵树 → 白屏。
    const disposable = {
      _isDisposed: false,
      dispose: function () {
        if (this._isDisposed) return; // this undefined 时这里会抛 TypeError
        this._isDisposed = true;
      },
    };
    (window as unknown as Record<string, unknown>).__ct_editor = {
      getValue: () => 'print(1)',
      getPosition: () => ({ lineNumber: 1, column: 1 }),
      onDidChangeModelContent: () => disposable,
    };

    render(
      <ThemeProvider><App /></ThemeProvider>,
    );

    await waitFor(() => expect(screen.getByTitle('回到主页')).toBeInTheDocument(), { timeout: 3000 });

    fireEvent.click(screen.getByTitle('回到主页'));

    await waitFor(() => expect(screen.getByText('开始对话')).toBeInTheDocument(), { timeout: 3000 });
  });
});
