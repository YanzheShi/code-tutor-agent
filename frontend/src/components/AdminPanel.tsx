/** Admin panel — password-protected management.
 *
 * Three sections:
 *   题库管理 — CRUD problems
 *   提交管理 — browse all submissions
 *   查看画像 — user proficiency profile
 */

import { useCallback, useEffect, useState } from 'react';

const BASE = 'http://localhost:8765';

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

interface AdminSubmission {
  id: number; problem_id: number; problem_title: string;
  verdict: string; code: string; created_at: string;
}

type AdminSection = 'questions' | 'submissions' | 'profile';
type AdminTab = 'list' | 'view' | 'edit';

const diffColorMap: Record<string, string> = {
  easy: 'bg-ct-success-bg text-ct-success',
  medium: 'bg-ct-warn-bg text-ct-warn',
  hard: 'bg-ct-error-bg text-ct-error',
};

const VERDICT_COLORS: Record<string, string> = {
  AC: 'text-ct-success', WA: 'text-ct-warn',
  TLE: 'text-ct-error', RE: 'text-ct-error', CE: 'text-ct-error',
};

// ── Profile sub-component ──

function AdminProfileView() {
  const [profile, setProfile] = useState<Record<string, unknown> | null>(null);
  const [profileV2, setProfileV2] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      fetch(BASE + '/admin/profile').then(r => r.ok ? r.json() : null),
      fetch(BASE + '/admin/profile/v2').then(r => r.ok ? r.json() : null),
    ]).then(([p1, p2]) => { setProfile(p1); setProfileV2(p2); setLoading(false); })
      .catch(() => { setProfile(null); setProfileV2(null); setLoading(false); });
  }, []);

  if (loading) return <div className="flex flex-1 items-center justify-center"><span className="text-sm text-ct-muted">加载画像中…</span></div>;
  if (!profile) return <div className="flex flex-1 items-center justify-center"><span className="text-sm text-ct-muted">暂无画像数据</span></div>;

  const bar = (val: number, color: string) => (
    <div className="h-2 w-full rounded-full bg-ct-hover">
      <div className={'h-2 rounded-full ' + color} style={{ width: Math.max(0, Math.round(val * 100)) + '%' }} />
    </div>
  );
  const v2Prof = profileV2?.prof as Record<string, number> | undefined;
  const tagNames = (profileV2?.tag_names ?? {}) as Record<string, string>;
  const tagEntries = v2Prof ? Object.entries(v2Prof).sort((a, b) => b[1] - a[1]) : [];

  return (
    <div className="flex-1 overflow-y-auto p-6 space-y-6">
      <section className="rounded-xl border border-ct-border bg-ct-surface p-5 space-y-4">
        <h3 className="text-sm font-semibold text-ct-text">总体指标</h3>
        <div className="grid grid-cols-2 gap-6">
          <div>
            <div className="flex justify-between mb-1 text-sm">
              <span className="text-ct-muted">综合熟练度</span>
              <span className="text-ct-text font-mono">{((profile.proficiency as number) * 100).toFixed(0)}%</span>
            </div>
            {bar(profile.proficiency as number, 'bg-ct-accent')}
          </div>
          <div>
            <div className="flex justify-between mb-1 text-sm">
              <span className="text-ct-muted">稳定性</span>
              <span className="text-ct-text font-mono">{((profile.stability as number) * 100).toFixed(0)}%</span>
            </div>
            {bar(profile.stability as number, 'bg-ct-success')}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-4 pt-1">
          <div><span className="text-ct-muted text-xs">做题数</span><p className="text-ct-text font-mono text-lg">{profile.attempts as number}</p></div>
          <div><span className="text-ct-muted text-xs">距离上次</span><p className="text-ct-text font-mono text-lg">{profile.forget_days as number} 天</p></div>
          <div><span className="text-ct-muted text-xs">AC率</span><p className="text-ct-text font-mono text-lg">{((profile.ac_rate as number ?? 0) * 100).toFixed(0)}%</p></div>
        </div>
        {((profile.common_errors as string[])?.length ?? 0) > 0 && (
          <div><span className="text-ct-muted text-xs block mb-1">常见错误</span>
            <div className="flex flex-wrap gap-1">{(profile.common_errors as string[]).map((e, i) => (
              <span key={i} className="rounded bg-ct-error-bg px-2 py-0.5 text-xs text-ct-error">{e}</span>
            ))}</div>
          </div>
        )}
      </section>

      {tagEntries.length > 0 && (
        <section className="rounded-xl border border-ct-border bg-ct-surface p-5 space-y-3">
          <h3 className="text-sm font-semibold text-ct-text">各知识点熟练度（{tagEntries.length} 个）</h3>
          {tagEntries.map(([tag, prof]) => (
            <div key={tag}>
              <div className="flex justify-between text-xs mb-0.5">
                <span className="text-ct-muted">{tagNames[tag] ?? tag}</span>
                <span className="text-ct-text font-mono">{(prof * 100).toFixed(0)}%</span>
              </div>
              {bar(prof, 'bg-ct-accent')}
            </div>
          ))}
        </section>
      )}
    </div>
  );
}

// ── Main component ──

export default function AdminPanel({ onClose }: { onClose: () => void }) {
  const [authenticated, setAuthenticated] = useState(false);
  const [password, setPassword] = useState('');
  const [loginError, setLoginError] = useState('');
  const [adminToken, setAdminToken] = useState<string | null>(null);

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

  // Submissions
  const [submissions, setSubmissions] = useState<AdminSubmission[]>([]);
  const [subsLoading, setSubsLoading] = useState(false);
  const [expandedCode, setExpandedCode] = useState<number | null>(null);

  // ── Auth ──
  const handleLogin = useCallback(async () => {
    setLoginError('');
    try {
      const r = await fetch(BASE + '/admin/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (r.ok) { setAuthenticated(true); setAdminToken(password); setPassword(''); }
      else setLoginError('密码错误');
    } catch { setLoginError('无法连接服务器'); }
  }, [password]);

  // ── Fetch problems ──
  const fetchProblems = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(BASE + '/admin/problems', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: adminToken }),
      });
      if (r.ok) setProblems((await r.json()).problems ?? []);
    } catch { /* ignore */ }
    finally { setLoading(false); }
  }, [adminToken]);

  // ── Fetch submissions ──
  const fetchSubmissions = useCallback(async () => {
    setSubsLoading(true);
    try {
      const r = await fetch(BASE + '/admin/submissions', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: adminToken }),
      });
      if (r.ok) setSubmissions((await r.json()).submissions ?? []);
    } catch { /* ignore */ }
    finally { setSubsLoading(false); }
  }, [adminToken]);

  useEffect(() => {
    if (authenticated) fetchProblems();
  }, [authenticated, fetchProblems]);

  useEffect(() => {
    if (authenticated && section === 'submissions') fetchSubmissions();
  }, [authenticated, section, fetchSubmissions]);

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
      const r = await fetch(BASE + `/admin/problem/${selectedProblem.id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...payload, password: adminToken }),
      });
      if (r.ok) { setSaveMsg('保存成功 ✓'); fetchProblems(); setTimeout(() => setSaveMsg(''), 3000); }
      else setSaveMsg('保存失败');
    } catch (e) { setSaveMsg('JSON 格式错误: ' + (e instanceof Error ? e.message : String(e))); }
  }, [selectedProblem, editForm, editTestCases, editVisibleTestCases, fetchProblems]);

  const handleDelete = useCallback(async (pid: number) => {
    try {
      const r = await fetch(BASE + `/admin/problem/${pid}/delete`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: adminToken }),
      });
      if (r.ok) { setProblems(prev => prev.filter(p => p.id !== pid)); if (selectedProblem?.id === pid) { setSelectedProblem(null); setActiveTab('list'); } }
    } catch { /* ignore */ }
  }, [selectedProblem]);

  const handleLogout = useCallback(() => {
    setAuthenticated(false); setProblems([]); setSelectedProblem(null);
    setActiveTab('list'); setSection('questions'); setLoginError(''); setPassword(''); setAdminToken(null);
  }, []);

  // ── Login screen ──
  if (!authenticated) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-ct-overlay backdrop-blur-sm">
        <div className="w-full max-w-sm rounded-xl border border-ct-border bg-ct-surface-secondary p-6 shadow-2xl">
          <h2 className="mb-1 text-lg font-bold text-ct-text">管理页面</h2>
          <p className="mb-4 text-xs text-ct-muted">请输入管理员密码</p>
          <div className="flex gap-2">
            <input type="password" value={password} onChange={e => setPassword(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleLogin(); }}
              placeholder="密码…" autoFocus
              className="flex-1 rounded-lg border border-ct-border bg-ct-input px-3 py-2 text-sm text-ct-text placeholder-ct-muted outline-none focus:border-ct-accent" />
            <button onClick={handleLogin} className="rounded-lg bg-ct-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90">进入</button>
          </div>
          {loginError && <p className="mt-2 text-xs text-ct-error">{loginError}</p>}
          <button onClick={onClose} className="mt-3 text-xs text-ct-muted hover:text-ct-text">← 返回</button>
        </div>
      </div>
    );
  }

  // ── Top bar ──
  const sectionItems: { id: AdminSection; label: string; icon: string }[] = [
    { id: 'questions', label: '题库管理', icon: '📚' },
    { id: 'submissions', label: '提交管理', icon: '📝' },
    { id: 'profile', label: '查看画像', icon: '📊' },
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
          <button onClick={handleLogout} className="text-xs text-ct-muted hover:text-ct-error">退出</button>
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
        <button onClick={handleLogout} className="text-xs text-ct-muted hover:text-ct-error">退出</button>
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

      {/* Submissions */}
      {section === 'submissions' && (
        <div className="flex-1 overflow-y-auto">
          {subsLoading ? (
            <div className="flex h-full items-center justify-center"><div className="flex items-center gap-2 text-sm text-ct-muted"><div className="h-4 w-4 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />加载中…</div></div>
          ) : submissions.length === 0 ? (
            <div className="flex h-full items-center justify-center"><p className="text-sm text-ct-muted">暂无提交记录</p></div>
          ) : (
            <div className="divide-y divide-ct-border/50">
              {submissions.map(sub => (
                <div key={sub.id} className="px-4 py-3 hover:bg-ct-surface">
                  <div className="flex items-start justify-between">
                    <div className="flex-1">
                      <div className="flex items-center gap-2 text-sm">
                        <span className="font-medium text-ct-text">#{sub.problem_id} {sub.problem_title}</span>
                        <span className={`font-bold text-xs ${VERDICT_COLORS[sub.verdict] || 'text-ct-muted'}`}>{sub.verdict || 'PENDING'}</span>
                        <span className="text-xs text-ct-muted">{sub.created_at}</span>
                      </div>
                    </div>
                    <button onClick={() => setExpandedCode(expandedCode === sub.id ? null : sub.id)}
                      className="text-xs text-ct-accent hover:underline ml-2 shrink-0">
                      {expandedCode === sub.id ? '收起' : '查看代码'}
                    </button>
                  </div>
                  {expandedCode === sub.id && (
                    <pre className="mt-2 rounded border border-ct-border bg-ct-surface-secondary p-3 text-xs font-mono text-ct-text overflow-x-auto max-h-48">{sub.code}</pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Profile */}
      {section === 'profile' && <AdminProfileView />}
    </div>
  );
}
