// 网络层异常兜底回归（2026-09-07：用户登录页看到英文裸报错 "Failed to fetch"）
// 断网/DNS 失败/连接拒绝时 fetch 抛 TypeError，auth.ts / client.ts 必须翻译成友好中文，
// 不得把英文原始异常透传到 UI。HTTP 业务错误（401 等）文案不受影响。
import { describe, it, expect, vi, afterEach } from 'vitest';
import { login } from '../src/api/auth';
import { apiFetch } from '../src/api/client';

function mockNetworkFailure() {
  return vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('Failed to fetch'));
}

describe('网络异常兜底（Failed to fetch → 友好中文）', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it('login: 网络失败抛友好中文，不透传英文 TypeError', async () => {
    mockNetworkFailure();
    await expect(login('a@b.com', 'password1')).rejects.toThrow('网络连接失败，请检查网络后重试');
    await expect(login('a@b.com', 'password1')).rejects.not.toThrow(/Failed to fetch/);
  });

  it('apiFetch: 网络失败抛友好中文', async () => {
    mockNetworkFailure();
    await expect(apiFetch('/session/1/problem')).rejects.toThrow('网络连接失败，请检查网络后重试');
  });

  it('register: 网络失败同样兜底', async () => {
    mockNetworkFailure();
    await expect(registerWrapper()).rejects.toThrow('网络连接失败，请检查网络后重试');
  });

  async function registerWrapper() {
    const { register } = await import('../src/api/auth');
    return register('a@b.com', 'password1', 'CODE1234');
  }

  it('HTTP 401 业务错误文案不受影响（仍是后端 detail）', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: '邮箱或密码错误' }),
    } as Response);
    await expect(login('a@b.com', 'wrongpass')).rejects.toThrow('邮箱或密码错误');
  });

  it('apiFetch: 401 凭证端点原样返回、不触发清凭证跳转', async () => {
    const authModule = await import('../src/api/auth');
    const clearSpy = vi.spyOn(authModule, 'clearAuth');
    vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: false,
      status: 401,
      json: async () => ({ detail: '邮箱或密码错误' }),
    } as Response);
    // apiFetch 只做透传，401 是否抛业务错误由上层 login() 决定
    const r = await apiFetch('/auth/login', { method: 'POST' });
    expect(r.status).toBe(401);
    expect(clearSpy).not.toHaveBeenCalled();
  });
});
