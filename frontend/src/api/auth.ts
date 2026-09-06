import { API_BASE } from './config';

export interface AuthUser {
  id: number;
  email: string;
  role: string;
}

const AUTH_KEY = 'code-tutor:auth';

interface StoredAuth {
  token: string;
  user: AuthUser;
}

export function getStoredAuth(): StoredAuth | null {
  try {
    const raw = localStorage.getItem(AUTH_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as StoredAuth;
    return parsed?.token && parsed?.user ? parsed : null;
  } catch {
    return null;
  }
}

export function setAuth(token: string, user: AuthUser): void {
  localStorage.setItem(AUTH_KEY, JSON.stringify({ token, user } satisfies StoredAuth));
}

export function clearAuth(): void {
  localStorage.removeItem(AUTH_KEY);
}

export function isAdmin(): boolean {
  return getStoredAuth()?.user.role === 'admin';
}

/** localStorage 按用户隔离用的 uid key（旧数据兼容：无 auth 时退化为 'default'）。 */
export function userKey(): string {
  const u = getStoredAuth()?.user.id;
  return u != null ? String(u) : 'default';
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const r = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.detail || '登录失败，请检查邮箱与密码');
  setAuth(data.token, data.user);
  return data.user;
}

export async function register(email: string, password: string, inviteCode: string): Promise<AuthUser> {
  const r = await fetch(`${API_BASE}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, invite_code: inviteCode.trim().toUpperCase() }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.detail || '注册失败');
  setAuth(data.token, data.user);
  return data.user;
}

/** 自助改密（验证旧密码）。 */
export async function changePassword(oldPassword: string, newPassword: string): Promise<void> {
  const auth = getStoredAuth();
  if (!auth) throw new Error('未登录');
  const r = await fetch(`${API_BASE}/auth/me/password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${auth.token}` },
    body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
  });
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    throw new Error(data?.detail || '修改失败');
  }
}

/** 忘记密码：请求验证码邮件（delivered=false 表示邮件服务未配置，走 admin 重置）。 */
export async function forgotPassword(email: string): Promise<{ delivered: boolean | null; message: string }> {
  const r = await fetch(`${API_BASE}/auth/forgot-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: email.trim() }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.detail || '请求失败，请稍后再试');
  return { delivered: data.delivered ?? null, message: data.message || '' };
}

/** 用邮箱验证码重置密码。 */
export async function resetPassword(email: string, code: string, newPassword: string): Promise<void> {
  const r = await fetch(`${API_BASE}/auth/reset-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: email.trim(), code: code.trim(), new_password: newPassword }),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.detail || '重置失败');
}

/** 启动时校验 token 是否仍有效（后端回库查用户；失效返回 null）。 */
export async function fetchMe(): Promise<AuthUser | null> {
  const auth = getStoredAuth();
  if (!auth) return null;
  try {
    const r = await fetch(`${API_BASE}/auth/me`, {
      headers: { Authorization: `Bearer ${auth.token}` },
    });
    if (!r.ok) {
      clearAuth();
      return null;
    }
    const data = await r.json();
    // 回写（role 等以后端为准）
    setAuth(auth.token, data.user);
    return data.user;
  } catch {
    // 网络异常：保留本地凭证，让用户进界面后按具体请求报错
    return auth.user;
  }
}
