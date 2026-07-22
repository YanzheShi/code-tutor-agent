import { useCallback, useRef } from 'react';

const BASE = 'http://localhost:8765';

type ProgressHandlers = {
  onProgress?: (message: string) => void;
  onDone?: (state: any) => void;
  onError?: (message: string) => void;
};

/**
 * 订阅后端的 SSE 出题进度端点 /session/{sid}/progress/stream，
 * 替代前端原先的 setInterval 轮询 /state。
 *
 * - progress 事件：追加一条进度消息
 * - done 事件：推送最终 serialize_state，调用 onDone
 * - error 事件：连接异常关闭（含超时），调用 onError
 *
 * 每次 subscribe 会先关闭上一次的 EventSource，避免重复连接。
 */
export function useProgressSSE() {
  const esRef = useRef<EventSource | null>(null);

  const subscribe = useCallback((sid: string, handlers?: ProgressHandlers) => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
    const es = new EventSource(`${BASE}/session/${sid}/progress/stream`);
    esRef.current = es;
    let finished = false;

    const finish = () => {
      finished = true;
      es.close();
      esRef.current = null;
    };

    es.addEventListener('progress', (e) => {
      try {
        const data = JSON.parse((e as MessageEvent).data);
        handlers?.onProgress?.(data.message);
      } catch {
        /* ignore malformed */
      }
    });

    es.addEventListener('done', (e) => {
      try {
        const state = JSON.parse((e as MessageEvent).data);
        handlers?.onDone?.(state);
      } catch {
        /* ignore malformed */
      }
      finish();
    });

    es.addEventListener('error', () => {
      if (finished) return;
      handlers?.onError?.('生成失败，请重试');
      finish();
    });

    return es;
  }, []);

  const close = useCallback(() => {
    if (esRef.current) {
      esRef.current.close();
      esRef.current = null;
    }
  }, []);

  return { subscribe, close };
}
