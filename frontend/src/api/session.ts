import type { RunCodeResponse, SessionStateResp, SubmitResponse } from '../types/session';

const BASE = 'http://localhost:8765';

export async function createSession(
  opts?: { topic?: string; difficulty?: string; mode?: string },
): Promise<{ session_id: string; status: string }> {
  const r = await fetch(`http://localhost:8765/session`, {
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
  const r = await fetch(`${BASE}/session/${sid}/submit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, language: 'python' }),
  });
  if (!r.ok) throw new Error(`submit failed: ${r.status}`);
  return r.json();
}

export async function runCode(
  sid: string,
  code: string,
): Promise<RunCodeResponse> {
  const r = await fetch(`${BASE}/session/${sid}/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, language: 'python' }),
  });
  if (!r.ok) throw new Error(`runCode failed: ${r.status}`);
  return r.json();
}

export async function getState(sid: string): Promise<SessionStateResp> {
  const r = await fetch(`${BASE}/session/${sid}/state`);
  if (!r.ok) throw new Error(`getState failed: ${r.status}`);
  return r.json();
}

export async function getReferenceCode(sid: string): Promise<{ code: string; title: string }> {
  const r = await fetch(`${BASE}/session/${sid}/reference`);
  if (!r.ok) throw new Error(`getReference failed: ${r.status}`);
  return r.json();
}