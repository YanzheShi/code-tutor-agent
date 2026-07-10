/** SSE 流式聊天读取 hook — 消除 App.tsx 中 3 处重复的流式读取代码。 */
import { useCallback } from 'react';

const BASE = 'http://localhost:8765';

export function useSSE() {
  const readStream = useCallback(async (
    sid: string,
    message: string,
    onToken: (token: string) => void,
    onDone?: () => void,
  ): Promise<boolean> => {
    const resp = await fetch(`${BASE}/session/${sid}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
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
          const token = event.slice(6);
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