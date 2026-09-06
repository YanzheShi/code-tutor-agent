import { useCallback, useRef } from 'react';
import { apiFetch } from '../api/client';
import { API_BASE } from '../api/config';

const BASE = API_BASE;

type ProgressHandlers = {
  onProgress?: (message: string) => void;
  onDone?: (state: any) => void;
  onError?: (message: string) => void;
};

/**
 * 订阅后端的 SSE 出题进度端点 /session/{sid}/progress/stream。
 *
 * 多用户改造（P4）：原 EventSource 无法携带 Authorization header，
 * 改用 fetch 流式读取并手写 SSE 事件解析（与 useSSE 的 chat/stream 同模式）。
 *
 * - progress 事件：追加一条进度消息
 * - done 事件：推送最终 serialize_state，调用 onDone
 * - error 事件：连接异常关闭（含超时），调用 onError
 */
export function useProgressSSE() {
  const abortRef = useRef<AbortController | null>(null);

  const close = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
  }, []);

  const subscribe = useCallback((sid: string, handlers?: ProgressHandlers) => {
    close();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let finished = false;

    const finish = () => {
      finished = true;
      ctrl.abort();
      abortRef.current = null;
    };

    (async () => {
      try {
        const resp = await apiFetch(`${BASE}/session/${sid}/progress/stream`, {
          signal: ctrl.signal,
          headers: { Accept: 'text/event-stream' },
        });
        if (!resp.ok || !resp.body) {
          handlers?.onError?.(`进度连接没建立成功（${resp.status}），点「再试一次」通常就能恢复～`);
          finish();
          return;
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        // 手写 SSE 解析：按空行切事件，事件内 data: 行拼接
        const dispatch = (rawEvent: string) => {
          const lines = rawEvent.split('\n');
          let event = 'message';
          const dataLines: string[] = [];
          for (const line of lines) {
            if (line.startsWith('event:')) event = line.slice(6).trim();
            else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
          }
          if (!dataLines.length) return;
          const dataText = dataLines.join('\n');
          try {
            const data = JSON.parse(dataText);
            if (event === 'progress') handlers?.onProgress?.(data.message);
            else if (event === 'done') {
              handlers?.onDone?.(data);
              finish();
            } else if (event === 'error') {
              handlers?.onError?.(data?.message || '出题没有成功，点击重试再试一次，或先回主页从题库选题练习～');
              finish();
            }
          } catch {
            /* ignore malformed */
          }
        };

        while (true) {
          const { done, value } = await reader.read();
          if (done || finished) break;
          buffer += decoder.decode(value, { stream: true });
          let idx: number;
          while ((idx = buffer.indexOf('\n\n')) >= 0) {
            const rawEvent = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            if (rawEvent.trim()) dispatch(rawEvent);
          }
        }
        if (!finished) {
          // 连接正常结束但未收到 done/error：让上层走 /state 轮询兜底
          handlers?.onError?.('进度连接中断了，点「再试一次」重新开始出题～');
          finish();
        }
      } catch (e: any) {
        if (finished || e?.name === 'AbortError') return; // 主动关闭不算错误
        handlers?.onError?.('进度连接中断了，点「再试一次」重新开始出题～');
        finish();
      }
    })();

    return ctrl;
  }, [close]);

  return { subscribe, close };
}
