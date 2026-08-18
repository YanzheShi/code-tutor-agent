/** SSE 流式聊天读取 hook — 消除 App.tsx 中 3 处重复的流式读取代码。 */
import { useCallback } from 'react';
import { API_BASE } from '../api/config';

const BASE = API_BASE;

/** 解析 SSE data 载荷：后端用 JSON { "t": text } 序列化 token（保留换行），
 *  兼容旧的裸文本格式。解析失败返回 null（跳过该事件，不破坏当前流）。 */
function decodePayload(raw: string): string | null {
  if (raw === '__DONE__') return '__DONE__';
  const trimmed = raw.trim();
  if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
    try {
      const parsed = JSON.parse(trimmed);
      if (parsed && typeof parsed.t === 'string') return parsed.t;
    } catch {
      /* fall through to legacy raw-text */
    }
  }
  return raw;
}

export function useSSE() {
  const readStream = useCallback(async (
    sid: string,
    message: string,
    onToken: (token: string) => void,
    onDone?: () => void,
    code?: string,
  ): Promise<boolean> => {
    const resp = await fetch(`${BASE}/session/${sid}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, ...(code != null && code.trim() ? { code } : {}) }),
    });
    if (!resp.ok || !resp.body) return false;

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const events = buffer.split('\n\n');
      buffer = events.pop() || '';
      for (const event of events) {
        if (event.startsWith('data: ')) {
          const raw = event.slice(6);
          const token = decodePayload(raw);
          if (token == null) continue;
          if (token === '__DONE__') continue;
          onToken(token);
        }
      }
    }
    onDone?.();
    return true;
  }, []);

  return { readStream };
}