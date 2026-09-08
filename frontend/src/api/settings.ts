import { API_BASE } from './config';
import { apiFetch } from './client';

/** 用户级 LLM 设置（设置页）。key 完整值永不回传，GET 只有打码形式。 */
export interface LlmSettings {
  mode: 'default' | 'custom';
  model: string;
  base_url: string;
  api_key_masked?: string;
  has_custom?: boolean;
  allow_custom?: boolean;
}

/** 保存/测试共用请求体；api_key 留空表示沿用已保存的 key。 */
export interface LlmSettingsInput {
  mode: 'default' | 'custom';
  model: string;
  base_url: string;
  api_key?: string;
}

export async function fetchLlmSettings(): Promise<LlmSettings> {
  const r = await apiFetch(`${API_BASE}/settings/me`);
  if (!r.ok) throw new Error('加载设置失败');
  return r.json();
}

export async function saveLlmSettings(body: LlmSettingsInput): Promise<LlmSettings> {
  const r = await apiFetch(`${API_BASE}/settings/me`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.detail || '保存失败，请稍后重试');
  return data;
}

export async function testLlmSettings(
  body: LlmSettingsInput,
): Promise<{ ok: boolean; sample?: string; error?: string }> {
  const r = await apiFetch(`${API_BASE}/settings/me/test`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.detail || '测试请求失败');
  return data;
}
