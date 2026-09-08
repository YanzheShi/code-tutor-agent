import { API_BASE } from './config';
import { clearAuth, getStoredAuth, NETWORK_ERROR_MSG } from './auth';

/** 统一带 Bearer 的 fetch 包装（多用户改造 P4）。
 *
 * - 自动附 Authorization: Bearer <token>（localStorage code-tutor:auth）
 * - 业务请求收到 401 → 清掉本地凭证并刷新回登录页（token 过期/无效统一出口）
 * - 凭证类端点（登录/注册/找回密码）401 是正常业务结果，不触发跳转
 *
 * ⚠️ 2026-09-06 修复：旧实现把整个 /auth/* 都判为"凭证端点"而不带 token，
 * 导致 /auth/me/profile 这类**需要鉴权**的 me 端点永远 401 → 主页面
 * 「我的画像」tab 一直显示空。正确口径：token 有就带上（登录/注册时本地
 * 本来就没有 token，带上过期 token 对这些端点也无害）；401 跳转只对
 * 凭证端点豁免（密码错误返回 401 是业务结果，不能清凭证刷回登录页）。
 */
const CREDENTIAL_PATH_RE = /\/auth\/(login|register|forgot-password|reset-password)([/?]|$)/;

export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const isCredentialPath = CREDENTIAL_PATH_RE.test(input);
  const headers = new Headers(init?.headers || {});
  // auth.ts 存的是 JSON.stringify({token, user})，必须解析取 token（直读会把整串 JSON 当 token → 401）
  const token = getStoredAuth()?.token;
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`);
  }
  let r: Response;
  try {
    r = await fetch(input, { ...init, headers });
  } catch {
    // 断网/DNS/连接失败等：fetch 抛英文 TypeError（"Failed to fetch"），禁止透传到 UI
    throw new Error(NETWORK_ERROR_MSG);
  }
  if (r.status === 401 && !isCredentialPath) {
    // token 过期/无效：清凭证回访客主页（App 检测不到 auth 则渲染 GuestWelcome）
    clearAuth();
    if (!window.location.pathname.includes('_login')) {
      window.location.reload();
    }
  }
  return r;
}
