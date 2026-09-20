import { API_BASE } from './config';
import { apiFetch } from './client';

/** 意见反馈（docs/feedback-feature-plan.md Phase 3）。
 *
 * 后端：POST /feedback（登录即可，含体验账号）。身份、user_agent、created_at
 * 全部由服务端补齐，客户端不传也不该传。
 */

/** 分类白名单，与后端 feedback.py 的 CATEGORIES 严格一致（改这里必须同步改那边）。 */
export type FeedbackCategory = 'bug' | 'experience' | 'content' | 'other';

export const FEEDBACK_CATEGORIES: FeedbackCategory[] = ['bug', 'experience', 'content', 'other'];

export interface FeedbackInput {
  category: FeedbackCategory;
  content: string;
  contact?: string;
  /** 提交时所在界面（无 URL 路由，用 screen 状态机标识代替 path） */
  screen?: string;
  problem_id?: number | null;
  session_id?: string;
}

export interface FeedbackSubmitResult {
  ok: boolean;
  id: number;
}

/** 提交反馈。失败抛 Error，message 取后端 detail（含 429 限频文案）。 */
export async function submitFeedback(body: FeedbackInput): Promise<FeedbackSubmitResult> {
  const r = await apiFetch(`${API_BASE}/feedback`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data?.detail || '提交失败，请稍后再试');
  return data;
}
