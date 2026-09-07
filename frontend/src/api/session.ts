import { apiFetch } from './client';
import type { RunCodeResponse, SessionStateResp, SubmitResponse } from '../types/session';
import { API_BASE } from './config';

const BASE = API_BASE;

export async function createSession(
  opts?: { topic?: string; difficulty?: string; mode?: string },
): Promise<{ session_id: string; status: string }> {
  const r = await apiFetch(`${BASE}/session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: opts ? JSON.stringify(opts) : undefined,
  });
  if (!r.ok) {
    const errBody = await r.text().catch(() => '');
    throw new Error(`创建会话失败 (${r.status}): ${errBody || '请确认后端服务已启动'}`);
  }
  return r.json();
}

export async function submitCode(
  sid: string,
  code: string,
): Promise<SubmitResponse> {
  // 加硬超时，避免后端判题异常卡死时「判题中」永远不解除
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 180000);
  try {
    const r = await apiFetch(`${BASE}/session/${sid}/submit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, language: 'python' }),
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error(`submit failed: ${r.status}`);
    return r.json();
  } finally {
    clearTimeout(timer);
  }
}

export async function runCode(
  sid: string,
  code: string,
): Promise<RunCodeResponse> {
  // 加硬超时，避免后端异常卡死时「运行中」永远不解除
  // todo 调大到 300s，避免断点调试时停留过久被超时掐断链路
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 300000);
  try {
    const r = await apiFetch(`${BASE}/session/${sid}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, language: 'python' }),
      signal: ctrl.signal,
    });
    if (!r.ok) throw new Error(`runCode failed: ${r.status}`);
    return r.json();
  } finally {
    clearTimeout(timer);
  }
}

export async function getState(sid: string): Promise<SessionStateResp> {
  const r = await apiFetch(`${BASE}/session/${sid}/state`);
  if (!r.ok) throw new Error(`getState failed: ${r.status}`);
  return r.json();
}

export async function getReferenceCode(sid: string): Promise<{ code: string; title: string }> {
  const r = await apiFetch(`${BASE}/session/${sid}/reference`);
  if (!r.ok) throw new Error(`getReference failed: ${r.status}`);
  return r.json();
}

export async function analyzeTrace(sid: string, problemId = 'default', message?: string): Promise<any> {
  const r = await apiFetch(`${BASE}/session/${sid}/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ problem_id: problemId, message }),
  });
  if (!r.ok) throw new Error(`analyzeTrace failed: ${r.status}`);
  const data = await r.json();
  // 首轮：data.analysis；多轮追问：data.reply
  return message ? data.reply : data.analysis;
}

export async function fetchTraceAnalysis(sid: string, problemId = 'default'): Promise<any> {
  const r = await apiFetch(`${BASE}/session/${sid}/analysis?problem_id=${encodeURIComponent(problemId)}`);
  if (!r.ok) throw new Error(`fetchTraceAnalysis failed: ${r.status}`);
  return r.json();
}

/** 轨迹分析多轮追问的流式版本：POST /session/{sid}/analyze/stream，SSE 逐 token 回调。 */
export async function analyzeTraceStream(
  sid: string,
  problemId = 'default',
  message?: string,
  onToken?: (token: string) => void,
): Promise<boolean> {
  const r = await apiFetch(`${BASE}/session/${sid}/analyze/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ problem_id: problemId, message }),
  });
  if (!r.ok || !r.body) return false;

  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split('\n\n');
    buffer = events.pop() || '';
    for (const ev of events) {
      if (!ev.startsWith('data: ')) continue;
      const raw = ev.slice(6);
      if (raw.trim() === '__DONE__') continue;
      let token = raw;
      const trimmed = raw.trim();
      if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
        try {
          const parsed = JSON.parse(trimmed);
          if (parsed && typeof parsed.t === 'string') token = parsed.t;
        } catch {
          /* 保留原始文本 */
        }
      }
      onToken?.(token);
    }
  }
  return true;
}

export async function summarizeTrace(
  sid: string,
  problemId = 'default',
  transitionAction = 'continue',
): Promise<any> {
  const r = await apiFetch(`${BASE}/session/${sid}/analyze/summarize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ problem_id: problemId, transition_action: transitionAction }),
  });
  if (!r.ok) throw new Error(`summarizeTrace failed: ${r.status}`);
  const data = await r.json();
  return data.summary;
}