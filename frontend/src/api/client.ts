import { API_BASE } from './config';
import { clearAuth, getStoredAuth } from './auth';

/** 统一带 Bearer 的 fetch 包装（多用户改造 P4）。
 *
 * - 自动附 Authorization: Bearer <token>（localStorage code-tutor:auth）
 * - 业务请求收到 401 → 清掉本地凭证并刷新回登录页（token 过期/无效统一出口）
 * - /auth/* 请求不附加 token，也不做 401 跳转（登录/注册本身可能 401）
 */
export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const isAuthPath = /[/?]auth\//.test(input) || input.includes(`${API_BASE}/auth`);
  const headers = new Headers(init?.headers || {});
  // auth.ts 存的是 JSON.stringify({token, user})，必须解析取 token（直读会把整串 JSON 当 token → 401）
  const token = getStoredAuth()?.token;
  if (token && !isAuthPath && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  const r = await fetch(input, { ...init, headers });
  if (r.status === 401 && !isAuthPath) {
    // token 过期/无效：清凭证回登录页（App 检测不到 auth 则渲染 LoginScreen）
    clearAuth();
    if (!window.location.pathname.includes('_login')) {
      window.location.reload();
    }
  }
  return r;
}
