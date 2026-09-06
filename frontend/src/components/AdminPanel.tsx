import { apiFetch } from '../api/client';
/** Admin panel — password-protected management.
 *
 * Three sections:
 *   题库管理 — CRUD problems
 *   成本中心 — token 用量 / 成本 / 缓存命中统计（只含内置 key 消耗）
 */

import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from 'react';
import { API_BASE } from '../api/config';

// 成本中心含 ECharts(~300KB),懒加载独立 chunk,仅在打开该 Tab 时下载
const CostCenter = lazy(() => import('./CostCenter'));

const BASE = API_BASE;

// ── Types ──

interface AdminProblem {
  id: number; title: string; topic: string; difficulty: string;
  description: string; visible_test_cases_list: AdminTestCase[];
  test_cases_list: AdminTestCase[];
  brute_solution: string; optimal_solution: string; starter_code: string;
  function_signature: string; time_complexity: string; space_complexity: string;
  source: string; source_url: string;
  constraints: string[]; alternative_solutions: string[]; novelty_score: number;
  created_at: string;
}

interface AdminTestCase {
  input_args: string[]; expected_output: string;
  explanation?: string; is_hidden?: boolean;
}

type AdminSection = 'questions' | 'cost' | 'users';
type AdminTab = 'list' | 'view' | 'edit';

const diffColorMap: Record<string, string> = {
  easy: 'bg-ct-success-bg text-ct-success',
  medium: 'bg-ct-warn-bg text-ct-warn',
  hard: 'bg-ct-error-bg text-ct-error',
};

// ── Main component ──

export default function AdminPanel({ onClose }: { onClose: () => void }) {
  // 鉴权说明（2026-09-06）：管理后台不再有独立密码——入口由 WelcomeScreen 的
  // isAdmin() 门禁控制，后端由 /admin 路由级 require_admin（JWT + role）把关。

  // Top-level section
  const [section, setSection] = useState<AdminSection>('questions');

  // Questions
  const [problems, setProblems] = useState<AdminProblem[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<AdminTab>('list');
  const [selectedProblem, setSelectedProblem] = useState<AdminProblem | null>(null);
  const [editForm, setEditForm] = useState<Record<string, string | number>>({});
  const [editVisibleTestCases, setEditVisibleTestCases] = useState('');
  const [editTestCases, setEditTestCases] = useState('');
  const [saveMsg, setSaveMsg] = useState('');
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null);

  // ── Fetch problems ──
  const fetchProblems = useCallback(async () => {
    setLoading(true);
    try {
      const r = await apiFetch(BASE + '/admin/problems', { method: 'POST' });
      if (r.ok) setProblems((await r.json()).problems ?? []);
    } catch { /* ignore */ }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    fetchProblems();
  }, [fetchProblems]);

  // ── Problem CRUD ──
  const handleView = (p: AdminProblem) => { setSelectedProblem(p); setActiveTab('view'); };
  const handleEdit = (p: AdminProblem) => {
    setSelectedProblem(p);
    setEditForm({ title: p.title, description: p.description, topic: p.topic, difficulty: p.difficulty, novelty_score: p.novelty_score, function_signature: p.function_signature, time_complexity: p.time_complexity, space_complexity: p.space_complexity, source: p.source, source_url: p.source_url });
    setEditVisibleTestCases(JSON.stringify(p.visible_test_cases_list, null, 2));
    setEditTestCases(JSON.stringify(p.test_cases_list, null, 2));
    setSaveMsg(''); setActiveTab('edit');
  };
  const handleBackToList = () => { setActiveTab('list'); setSelectedProblem(null); };

  const handleSave = useCallback(async () => {
    if (!selectedProblem) return;
    try {
      const payload = { ...Object.fromEntries(Object.entries(editForm).filter(([_, v]) => v !== '' && v !== undefined)), test_cases: JSON.parse(editTestCases), visible_test_cases: JSON.parse(editVisibleTestCases) } as Record<string, unknown>;
      const r = await apiFetch(BASE + `/admin/problem/${selectedProblem.id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (r.ok) { setSaveMsg('保存成功 ✓'); fetchProblems(); setTimeout(() => setSaveMsg(''), 3000); }
      else setSaveMsg('保存失败');
    } catch (e) { setSaveMsg('JSON 格式错误: ' + (e instanceof Error ? e.message : String(e))); }
  }, [selectedProblem, editForm, editTestCases, editVisibleTestCases, fetchProblems]);

  const handleDelete = useCallback(async (pid: number) => {
    try {
      const r = await apiFetch(BASE + `/admin/problem/${pid}/delete`, { method: 'POST' });
      if (r.ok) { setProblems(prev => prev.filter(p => p.id !== pid)); if (selectedProblem?.id === pid) { setSelectedProblem(null); setActiveTab('list'); } }
    } catch { /* ignore */ }
  }, [selectedProblem]);

  // ── Top bar ──
  const sectionItems: { id: AdminSection; label: string; icon: string }[] = [
    { id: 'questions', label: '题库管理', icon: '📚' },
    { id: 'cost', label: '成本中心', icon: '💸' },
    { id: 'users', label: '用户与邀请码', icon: '👥' },
  ];

  // ── Questions view (non-list) ──
  if (section === 'questions' && activeTab !== 'list' && selectedProblem) {
    const p = selectedProblem;
    return (
      <div className="flex h-full flex-col">
        {/* Top bar */}
        <div className="flex items-center justify-between border-b border-ct-border px-4 py-3">
          <div className="flex items-center gap-4">
            <button onClick={handleBackToList} className="text-xs text-ct-muted hover:text-ct-text">← 返回列表</button>
          </div>
          <button onClick={onClose} className="text-xs text-ct-muted hover:text-ct-error">退出</button>
        </div>

        {/* View mode */}
        {activeTab === 'view' && (
          <div className="flex-1 overflow-y-auto p-4">
            <h2 className="mb-1 text-xl font-bold text-ct-text">{p.title}</h2>
            <div className="mb-3 flex items-center gap-2">
              <span className={`rounded border px-2 py-0.5 text-xs ${diffColorMap[p.difficulty] || ''}`}>{p.difficulty}</span>
              <span className="text-xs text-ct-muted">{p.topic}</span>
              <span className="text-xs text-ct-muted">创建于 {p.created_at}</span>
            </div>
            <section className="mb-4"><h3 className="mb-1 text-sm font-semibold text-ct-accent">题目描述</h3><pre className="whitespace-pre-wrap rounded border border-ct-border bg-ct-surface-secondary p-3 text-xs text-ct-text">{p.description}</pre></section>
            <section className="mb-4"><h3 className="mb-1 text-sm font-semibold text-ct-accent">判题测试用例 ({p.test_cases_list.length})</h3>
              <div className="space-y-2">{p.test_cases_list.map((tc, i) => (
                <div key={i} className="rounded border border-ct-border bg-ct-surface-secondary p-3 text-xs">
                  <div className="mb-1 flex items-center gap-2"><span className="font-bold text-ct-accent">用例 #{i + 1}</span>{tc.is_hidden && <span className="rounded bg-ct-hover px-1.5 py-0.5 text-[10px] text-ct-muted">隐藏</span>}</div>
                  <div className="text-ct-muted">输入: <code className="text-ct-text">{JSON.stringify(tc.input_args)}</code></div>
                  <div className="text-ct-muted">输出: <code className="text-ct-text">{tc.expected_output}</code></div>
                </div>
              ))}</div></section>
            <section className="mb-4"><h3 className="mb-1 text-sm font-semibold text-ct-accent">前台运行用例 ({p.visible_test_cases_list.length})</h3>
              <div className="space-y-2">{p.visible_test_cases_list.map((tc, i) => (
                <div key={i} className="rounded border border-ct-border bg-ct-surface-secondary p-3 text-xs">
                  <div className="mb-1 font-bold text-ct-accent">用例 #{i + 1}</div>
                  <div className="text-ct-muted">输入: <code className="text-ct-text">{JSON.stringify(tc.input_args)}</code></div>
                  <div className="text-ct-muted">输出: <code className="text-ct-text">{tc.expected_output}</code></div>
                </div>
              ))}</div></section>
            {p.starter_code && <section className="mb-4"><h3 className="mb-1 text-sm font-semibold text-ct-accent">模板代码</h3><pre className="rounded border border-ct-border bg-ct-surface-secondary p-3 text-xs font-mono text-ct-text overflow-x-auto">{p.starter_code}</pre></section>}
            <div className="flex gap-3 pt-2">
              <button onClick={() => handleEdit(p)} className="rounded-lg border border-ct-border px-4 py-2 text-sm text-ct-text hover:bg-ct-hover/30">编辑</button>
              <button onClick={() => setDeleteConfirm(p.id)} className="rounded-lg border border-ct-error/40 px-4 py-2 text-sm text-ct-error hover:bg-ct-error-bg">删除</button>
            </div>
            {deleteConfirm === p.id && (
              <div className="mt-3 rounded-lg border border-ct-error/40 bg-ct-error-bg p-3">
                <p className="text-sm text-ct-error">确定要删除「{p.title}」吗？此操作不可撤销。</p>
                <div className="mt-2 flex gap-2">
                  <button onClick={() => { handleDelete(p.id); setDeleteConfirm(null); }} className="rounded bg-ct-error px-3 py-1 text-xs text-white hover:bg-ct-error/80">确认删除</button>
                  <button onClick={() => setDeleteConfirm(null)} className="rounded border border-ct-border px-3 py-1 text-xs text-ct-muted hover:text-ct-text">取消</button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Edit mode */}
        {activeTab === 'edit' && (
          <div className="flex-1 overflow-y-auto p-4">
            <h2 className="mb-4 text-lg font-bold text-ct-text">编辑: {selectedProblem.title}</h2>
            <div className="space-y-3">
              <div><label className="mb-1 block text-xs font-medium text-ct-muted">标题</label><input type="text" value={editForm.title as string || ''} onChange={e => setEditForm(f => ({ ...f, title: e.target.value }))} className="w-full rounded border border-ct-border bg-ct-input px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent" /></div>
              <div className="flex gap-3">
                <div className="flex-1"><label className="mb-1 block text-xs font-medium text-ct-muted">知识点</label><input type="text" value={editForm.topic as string || ''} onChange={e => setEditForm(f => ({ ...f, topic: e.target.value }))} className="w-full rounded border border-ct-border bg-ct-input px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent" /></div>
                <div className="w-28"><label className="mb-1 block text-xs font-medium text-ct-muted">难度</label><select value={editForm.difficulty as string || 'medium'} onChange={e => setEditForm(f => ({ ...f, difficulty: e.target.value }))} className="w-full rounded border border-ct-border bg-ct-input px-2 py-2 text-sm text-ct-text outline-none focus:border-ct-accent"><option value="easy">Easy</option><option value="medium">Medium</option><option value="hard">Hard</option></select></div>
              </div>
              <div><label className="mb-1 block text-xs font-medium text-ct-muted">题目描述</label><textarea value={editForm.description as string || ''} onChange={e => setEditForm(f => ({ ...f, description: e.target.value }))} rows={6} className="w-full rounded border border-ct-border bg-ct-input px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent" /></div>
              <div><label className="mb-1 block text-xs font-medium text-ct-muted">判题测试用例 (JSON)</label><textarea value={editTestCases} onChange={e => setEditTestCases(e.target.value)} rows={8} className="w-full rounded border border-ct-border bg-ct-input px-3 py-2 text-xs font-mono text-ct-text outline-none focus:border-ct-accent" /></div>
              <div><label className="mb-1 block text-xs font-medium text-ct-muted">前台运行用例 (JSON)</label><textarea value={editVisibleTestCases} onChange={e => setEditVisibleTestCases(e.target.value)} rows={6} className="w-full rounded border border-ct-border bg-ct-input px-3 py-2 text-xs font-mono text-ct-text outline-none focus:border-ct-accent" /></div>
              <div className="flex gap-3 pt-2">
                <button onClick={handleSave} className="rounded-lg bg-ct-accent px-5 py-2 text-sm font-medium text-white hover:opacity-90">保存修改</button>
                <button onClick={() => setActiveTab('view')} className="rounded border border-ct-border px-4 py-2 text-sm text-ct-text hover:bg-ct-hover/30">取消</button>
              </div>
              {saveMsg && <p className={`text-xs ${saveMsg.includes('✓') ? 'text-ct-success' : 'text-ct-error'}`}>{saveMsg}</p>}
            </div>
          </div>
        )}
      </div>
    );
  }

  // ── Main view (questions list + submissions + profile) ──
  return (
    <div className="flex h-full flex-col">
      {/* Top bar */}
      <div className="flex items-center justify-between border-b border-ct-border px-4 py-3">
        <div className="flex items-center gap-4">
          <h2 className="text-sm font-bold text-ct-text">🛡️ 管理页面</h2>
          <div className="flex gap-1 rounded bg-ct-input p-0.5">
            {sectionItems.map(it => (
              <button key={it.id} onClick={() => setSection(it.id)}
                className={`px-3 py-1 text-xs font-medium rounded transition ${section === it.id ? 'bg-ct-accent text-white' : 'text-ct-muted hover:text-ct-text'}`}>
                {it.icon} {it.label}
              </button>
            ))}
          </div>
        </div>
        <button onClick={onClose} className="text-xs text-ct-muted hover:text-ct-error">退出</button>
      </div>

      {/* Section content */}
      {/* Questions list */}
      {section === 'questions' && (
        <>
          {loading ? (
            <div className="flex flex-1 items-center justify-center"><div className="flex items-center gap-2 text-sm text-ct-muted"><div className="h-4 w-4 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />加载中…</div></div>
          ) : problems.length === 0 ? (
            <div className="flex flex-1 items-center justify-center"><p className="text-sm text-ct-muted">题库为空</p></div>
          ) : (
            <div className="flex-1 overflow-y-auto">
              <div className="divide-y divide-ct-border/50">
                {problems.map(p => (
                  <div key={p.id} className="group px-4 py-3 hover:bg-ct-surface">
                    <div className="flex items-start justify-between">
                      <div className="flex-1">
                        <div className="flex items-center gap-2">
                          <span className="font-medium text-ct-text text-sm">{p.id}. {p.title}</span>
                          <span className={`rounded border px-1.5 py-0.5 text-[10px] ${diffColorMap[p.difficulty] || ''}`}>{p.difficulty}</span>
                          <span className="text-xs text-ct-muted">{p.topic}</span>
                          <span className="text-xs text-ct-muted">{p.test_cases_list.length} 判题 / {p.visible_test_cases_list.length} 前台</span>
                        </div>
                        <p className="mt-1 line-clamp-2 text-xs text-ct-muted">{p.description.slice(0, 120)}…</p>
                      </div>
                      <div className="flex gap-1.5 ml-2 shrink-0">
                        <button onClick={() => handleView(p)} className="rounded border border-ct-border px-2 py-1 text-[10px] text-ct-muted hover:border-ct-accent hover:text-ct-accent">查看</button>
                        <button onClick={() => handleEdit(p)} className="rounded border border-ct-border px-2 py-1 text-[10px] text-ct-muted hover:border-ct-accent hover:text-ct-accent">编辑</button>
                        <button onClick={() => setDeleteConfirm(p.id)} className="rounded border border-ct-border px-2 py-1 text-[10px] text-ct-muted hover:border-ct-error hover:text-ct-error">删除</button>
                      </div>
                    </div>
                    {deleteConfirm === p.id && (
                      <div className="mt-2 flex items-center gap-2 rounded border border-ct-error/40 bg-ct-error-bg p-2">
                        <span className="text-xs text-ct-error">确认删除？</span>
                        <button onClick={() => { handleDelete(p.id); setDeleteConfirm(null); }} className="rounded bg-ct-error px-2 py-0.5 text-[10px] text-white hover:bg-ct-error/80">确认</button>
                        <button onClick={() => setDeleteConfirm(null)} className="rounded border border-ct-border px-2 py-0.5 text-[10px] text-ct-muted hover:text-ct-text">取消</button>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {/* Cost center */}
      {section === 'cost' && (
        <Suspense fallback={<div className="p-4 text-sm text-ct-muted">加载成本中心…</div>}>
          <CostCenter />
        </Suspense>
      )}

      {/* 用户与邀请码管理 */}
      {section === 'users' && <AdminUsersView />}
    </div>
  );
}

/* ── 用户与邀请码管理（防滥用改造，2026-09-06）── */

interface AdminUserRow {
  id: number;
  email: string;
  role: string;
  created_at: string;
}

interface InviteRow {
  code: string;
  max_uses: number;
  used_count: number;
  expires_at: string | null;
  active: number;
  note: string;
  created_at: string;
}

function AdminUsersView() {
  const [users, setUsers] = useState<AdminUserRow[]>([]);
  const [invites, setInvites] = useState<InviteRow[]>([]);
  const [msg, setMsg] = useState('');
  const [tempPw, setTempPw] = useState<{ email: string; pw: string } | null>(null);
  const [maxUses, setMaxUses] = useState(100);
  const [expiresDays, setExpiresDays] = useState(1);
  const [note, setNote] = useState('');

  const load = useCallback(async () => {
    try {
      const [u, i] = await Promise.all([
        apiFetch(API_BASE + '/admin/users'),
        apiFetch(API_BASE + '/admin/invites'),
      ]);
      if (u.ok) setUsers((await u.json()).users ?? []);
      if (i.ok) setInvites((await i.json()).invites ?? []);
    } catch { /* ignore */ }
  }, []);
  useEffect(() => { load(); }, [load]);

  const resetPassword = async (userId: number, email: string) => {
    setMsg('');
    try {
      const r = await apiFetch(`${API_BASE}/admin/users/${userId}/reset-password`, { method: 'POST' });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.temp_password) {
        setTempPw({ email, pw: data.temp_password });
      } else {
        setMsg(data?.detail || '重置失败');
      }
    } catch { setMsg('网络错误'); }
  };

  const createInvite = async () => {
    setMsg('');
    try {
      const r = await apiFetch(API_BASE + '/admin/invites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_uses: maxUses, expires_days: expiresDays, note }),
      });
      const data = await r.json().catch(() => ({}));
      if (r.ok && data.code) {
        setMsg(`已生成邀请码：${data.code}（额度 ${data.max_uses}${data.expires_at ? `，有效期至 ${data.expires_at}` : '，永久'}）`);
        setNote('');
        load();
      } else {
        setMsg(data?.detail || '生成失败');
      }
    } catch { setMsg('网络错误'); }
  };

  const disableInvite = async (code: string) => {
    setMsg('');
    try {
      const r = await apiFetch(`${API_BASE}/admin/invites/${code}/disable`, { method: 'POST' });
      if (r.ok) load(); else setMsg('停用失败');
    } catch { setMsg('网络错误'); }
  };

  const th = 'px-2 py-1 text-left text-xs font-medium text-ct-muted';
  const td = 'px-2 py-1 text-xs text-ct-text';

  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-6">
      <section>
        <h3 className="mb-2 text-sm font-medium text-ct-text">用户（{users.length}）</h3>
        <div className="overflow-x-auto rounded-lg border border-ct-border">
          <table className="w-full">
            <thead className="bg-ct-bg"><tr><th className={th}>ID</th><th className={th}>邮箱</th><th className={th}>角色</th><th className={th}>注册时间</th><th className={th}>操作</th></tr></thead>
            <tbody>
              {users.map(u => (
                <tr key={u.id} className="border-t border-ct-border">
                  <td className={td}>{u.id}</td>
                  <td className={td}>{u.email}</td>
                  <td className={td}>{u.role === 'admin' ? '管理员' : '用户'}</td>
                  <td className={td}>{(u.created_at || '').slice(0, 16)}</td>
                  <td className={td}>
                    <button
                      onClick={() => resetPassword(u.id, u.email)}
                      className="rounded border border-ct-border px-2 py-0.5 text-xs text-ct-muted hover:text-ct-text">
                      重置密码
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-medium text-ct-text">生成邀请码</h3>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-xs text-ct-muted">额度
            <input type="number" min={1} max={10000} value={maxUses}
              onChange={e => setMaxUses(Math.max(1, Number(e.target.value) || 1))}
              className="ml-1 w-20 rounded border border-ct-border bg-ct-bg px-2 py-1 text-xs text-ct-text" />
          </label>
          <label className="text-xs text-ct-muted">有效天数（0=永久）
            <input type="number" min={0} max={365} value={expiresDays}
              onChange={e => setExpiresDays(Math.max(0, Number(e.target.value) || 0))}
              className="ml-1 w-20 rounded border border-ct-border bg-ct-bg px-2 py-1 text-xs text-ct-text" />
          </label>
          <input type="text" value={note} onChange={e => setNote(e.target.value)} placeholder="备注（可选）"
            className="w-40 rounded border border-ct-border bg-ct-bg px-2 py-1 text-xs text-ct-text" />
          <button onClick={createInvite}
            className="rounded bg-ct-accent px-3 py-1 text-xs font-medium text-white hover:opacity-90">
            生成
          </button>
        </div>
      </section>

      <section>
        <h3 className="mb-2 text-sm font-medium text-ct-text">邀请码（{invites.length}）</h3>
        <div className="overflow-x-auto rounded-lg border border-ct-border">
          <table className="w-full">
            <thead className="bg-ct-bg"><tr><th className={th}>码</th><th className={th}>已用/额度</th><th className={th}>过期时间</th><th className={th}>状态</th><th className={th}>备注</th><th className={th}>操作</th></tr></thead>
            <tbody>
              {invites.map(v => (
                <tr key={v.code} className="border-t border-ct-border">
                  <td className={`${td} font-mono`}>{v.code}</td>
                  <td className={td}>{v.used_count}/{v.max_uses}</td>
                  <td className={td}>{v.expires_at ? v.expires_at.slice(0, 16) : '永久'}</td>
                  <td className={td}>{v.active ? '有效' : '已停用'}</td>
                  <td className={td}>{v.note || '-'}</td>
                  <td className={td}>
                    {v.active === 1 && (
                      <button onClick={() => disableInvite(v.code)}
                        className="rounded border border-ct-border px-2 py-0.5 text-xs text-red-500 hover:text-red-600">
                        停用
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {invites.length === 0 && (
                <tr><td className={td} colSpan={6}>还没有邀请码，用上面的表单生成一个</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      {msg && <div className="rounded-lg border border-ct-border bg-ct-bg px-3 py-2 text-sm text-ct-text">{msg}</div>}
      {tempPw && (
        <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          <b>{tempPw.email}</b> 的临时密码：<span className="font-mono font-bold">{tempPw.pw}</span>
          （仅显示这一次，请立即发给用户；用户登录后建议自行修改）
          <button onClick={() => setTempPw(null)} className="ml-2 text-xs underline">知道了</button>
        </div>
      )}
    </div>
  );
}