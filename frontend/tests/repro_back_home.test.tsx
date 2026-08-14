import { describe, it, expect, vi, beforeEach, beforeAll, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import App from '../src/App';
import { ThemeProvider } from '../src/hooks/useTheme';

vi.mock('@monaco-editor/react', () => ({
  default: () => <div data-testid="monaco-stub" />,
}));

class EventSourceStub {
  url: string;
  listeners: Record<string, (e: any) => void> = {};
  constructor(url: string) { this.url = url; }
  addEventListener(type: string, cb: (e: any) => void) { this.listeners[type] = cb; }
  close() {}
}

beforeAll(() => {
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
});

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
    if (u.includes('/session/') && u.includes('/state')) {
      return { ok: true, json: async () => DIALOG_STATE } as Response;
    }
    if (u === '/__edit_trace') {
      return { ok: true, status: 204 } as Response;
    }
    throw new Error('unexpected fetch: ' + u);
  }));
}

beforeEach(() => {
  vi.stubGlobal('EventSource', EventSourceStub as any);
  localStorage.clear();
  mockFetch();
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__ct_editor;
  vi.unstubAllGlobals();
});

describe('back to home', () => {
  it('renders WelcomeScreen after clicking 回到主页', async () => {
    localStorage.setItem('code-tutor:session', JSON.stringify({ screen: 'main', sessionId: 's1', mode: 'agent' }));
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

    localStorage.setItem('code-tutor:session', JSON.stringify({ screen: 'main', sessionId: 's1', mode: 'agent' }));
    render(
      <ThemeProvider><App /></ThemeProvider>,
    );

    await waitFor(() => expect(screen.getByTitle('回到主页')).toBeInTheDocument(), { timeout: 3000 });

    fireEvent.click(screen.getByTitle('回到主页'));

    await waitFor(() => expect(screen.getByText('开始对话')).toBeInTheDocument(), { timeout: 3000 });
  });
});