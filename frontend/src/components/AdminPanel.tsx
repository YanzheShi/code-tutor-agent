/** Admin panel for managing problems — password-protected.

Features:
- Password authentication (from .env ADMIN_PASSWORD)
- List all problems with full details
- View / edit problem fields (title, description, topic, difficulty, test cases)
- Delete problems
*/

import { useCallback, useEffect, useState } from 'react';

const BASE = 'http://localhost:8765';

// ── Types ──

interface AdminProblem {
  id: number;
  title: string;
  topic: string;
  difficulty: string;
  description: string;
  visible_test_cases_list: AdminTestCase[];
  test_cases_list: AdminTestCase[];
  brute_solution: string;
  starter_code: string;
  novelty_score: number;
  created_at: string;
}

interface AdminTestCase {
  input_args: string[];
  expected_output: string;
  explanation?: string;
  is_hidden?: boolean;
}

type AdminTab = 'list' | 'view' | 'edit';
type DiffColor = 'bg-green-900/50 text-green-400 border-green-700' | 'bg-amber-900/50 text-amber-400 border-amber-700' | 'bg-red-900/50 text-red-400 border-red-700';

export default function AdminPanel({ onClose }: { onClose: () => void }) {
  const [authenticated, setAuthenticated] = useState(false);
  const [password, setPassword] = useState('');
  const [loginError, setLoginError] = useState('');
  const [adminToken, setAdminToken] = useState<string | null>(null);

  const [problems, setProblems] = useState<AdminProblem[]>([]);
  const [loading, setLoading] = useState(false);

  // View / Edit state
  const [activeTab, setActiveTab] = useState<AdminTab>('list');
  const [selectedProblem, setSelectedProblem] = useState<AdminProblem | null>(null);
  const [editForm, setEditForm] = useState<Record<string, string | number>>({});
  const [editVisibleTestCases, setEditVisibleTestCases] = useState<string>('');
  const [editTestCases, setEditTestCases] = useState<string>('');
  const [saveMsg, setSaveMsg] = useState('');
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null);

  // ── Auth ──
  const handleLogin = useCallback(async () => {
    setLoginError('');
    try {
      const r = await fetch(BASE + '/admin/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (r.ok) {
        setAuthenticated(true);
        setAdminToken(password);
        setPassword('');
      } else {
        setLoginError('密码错误');
      }
    } catch {
      setLoginError('无法连接服务器');
    }
  }, [password]);

  // ── Fetch problems ──
  const fetchProblems = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(BASE + '/admin/problems', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: adminToken }),
      });
      if (r.ok) {
        const data = await r.json();
        setProblems(data.problems ?? []);
      }
    } catch {
      // silently fail
    } finally {
      setLoading(false);
    }
  }, [adminToken]);

  useEffect(() => {
    if (authenticated) fetchProblems();
  }, [authenticated, fetchProblems]);

  // ── View problem ──
  const handleView = useCallback((p: AdminProblem) => {
    setSelectedProblem(p);
    setActiveTab('view');
  }, []);

  // ── Edit problem ──
  const handleEdit = useCallback((p: AdminProblem) => {
    setSelectedProblem(p);
    setEditForm({
      title: p.title,
      description: p.description,
      topic: p.topic,
      difficulty: p.difficulty,
      novelty_score: p.novelty_score,
    });
    setEditVisibleTestCases(JSON.stringify(p.visible_test_cases_list, null, 2));
    setEditTestCases(JSON.stringify(p.test_cases_list, null, 2));
    setSaveMsg('');
    setActiveTab('edit');
  }, []);

  const handleSave = useCallback(async () => {
    if (!selectedProblem) return;
    try {
      const payload = {
        ...Object.fromEntries(
          Object.entries(editForm).filter(([_, v]) => v !== '' && v !== undefined)
        ),
        test_cases: JSON.parse(editTestCases),
        visible_test_cases: JSON.parse(editVisibleTestCases),
      } as Record<string, unknown>;

      const r = await fetch(BASE + `/admin/problem/${selectedProblem.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...payload, password: adminToken }),
      });
      if (r.ok) {
        setSaveMsg('保存成功 ✓');
        fetchProblems();
        setTimeout(() => setSaveMsg(''), 3000);
      } else {
        setSaveMsg('保存失败');
      }
    } catch (e) {
      setSaveMsg('JSON 格式错误: ' + (e instanceof Error ? e.message : String(e)));
    }
  }, [selectedProblem, editForm, editTestCases, editVisibleTestCases, fetchProblems]);

  // ── Delete problem ──
  const handleDelete = useCallback(async (pid: number) => {
    try {
      const r = await fetch(BASE + `/admin/problem/${pid}/delete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: adminToken }),
      });
      if (r.ok) {
        setProblems(prev => prev.filter(p => p.id !== pid));
        if (selectedProblem?.id === pid) {
          setSelectedProblem(null);
          setActiveTab('list');
        }
      }
    } catch {
      // silently fail
    }
  }, [selectedProblem]);

  // ── Reset to list ──
  const handleBackToList = useCallback(() => {
    setActiveTab('list');
    setSelectedProblem(null);
  }, []);

  // ── Logout ──
  const handleLogout = useCallback(() => {
    setAuthenticated(false);
    setProblems([]);
    setSelectedProblem(null);
    setActiveTab('list');
    setLoginError('');
    setPassword('');
    setAdminToken(null);
  }, []);

  const diffColorMap: Record<string, DiffColor> = {
    easy: 'bg-green-900/50 text-green-400 border-green-700',
    medium: 'bg-amber-900/50 text-amber-400 border-amber-700',
    hard: 'bg-red-900/50 text-red-400 border-red-700',
  };

  // ── Login Screen ──
  if (!authenticated) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
        <div className="w-full max-w-sm rounded-xl border border-ct-border bg-slate-900 p-6 shadow-2xl">
          <h2 className="mb-1 text-lg font-bold text-ct-text">管理页面</h2>
          <p className="mb-4 text-xs text-ct-muted">请输入管理员密码</p>
          <div className="flex gap-2">
            <input
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') handleLogin(); }}
              placeholder="密码…"
              className="flex-1 rounded-lg border border-ct-border bg-slate-800/50 px-3 py-2 text-sm text-ct-text placeholder-ct-muted outline-none focus:border-ct-accent"
              autoFocus
            />
            <button
              onClick={handleLogin}
              className="rounded-lg bg-ct-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90"
            >
              进入
            </button>
          </div>
          {loginError && <p className="mt-2 text-xs text-red-400">{loginError}</p>}
          <button
            onClick={onClose}
            className="mt-3 text-xs text-ct-muted hover:text-ct-text"
          >
            ← 返回
          </button>
        </div>
      </div>
    );
  }

  // ── View Mode ──
  if (activeTab === 'view' && selectedProblem) {
    const p = selectedProblem;
    return (
      <div className="h-full overflow-y-auto p-4">
        <div className="mb-4 flex items-center justify-between">
          <button onClick={handleBackToList} className="text-xs text-ct-muted hover:text-ct-text">← 返回列表</button>
          <button onClick={handleLogout} className="text-xs text-ct-muted hover:text-red-400">退出</button>
        </div>

        <h2 className="mb-1 text-xl font-bold text-ct-text">{p.title}</h2>
        <div className="mb-3 flex items-center gap-2">
          <span className={`rounded border px-2 py-0.5 text-xs ${diffColorMap[p.difficulty] || ''}`}>{p.difficulty}</span>
          <span className="text-xs text-ct-muted">{p.topic}</span>
          <span className="text-xs text-ct-muted">创建于 {p.created_at}</span>
        </div>

        {/* Description */}
        <section className="mb-4">
          <h3 className="mb-1 text-sm font-semibold text-ct-accent">题目描述</h3>
          <pre className="whitespace-pre-wrap rounded border border-ct-border bg-slate-900/30 p-3 text-xs text-ct-text">{p.description}</pre>
        </section>

        {/* Test Cases — 判题用（全量） */}
        <section className="mb-4">
          <h3 className="mb-1 text-sm font-semibold text-ct-accent">判题测试用例 ({p.test_cases_list.length})</h3>
          <p className="mb-2 text-[10px] text-ct-muted">后台判题使用的完整套件，包含隐藏用例</p>
          <div className="space-y-2">
            {p.test_cases_list.map((tc, i) => (
              <div key={i} className="rounded border border-ct-border bg-slate-900/30 p-3 text-xs">
                <div className="mb-1 flex items-center gap-2">
                  <span className="font-bold text-ct-accent">用例 #{i + 1}</span>
                  {tc.is_hidden && <span className="rounded bg-slate-700 px-1.5 py-0.5 text-[10px] text-ct-muted">隐藏</span>}
                </div>
                <div className="text-ct-muted">输入: <code className="text-ct-text">{JSON.stringify(tc.input_args)}</code></div>
                <div className="text-ct-muted">输出: <code className="text-ct-text">{tc.expected_output}</code></div>
                {tc.explanation && <div className="text-ct-muted mt-1 italic">{tc.explanation}</div>}
              </div>
            ))}
            {p.test_cases_list.length === 0 && <p className="text-xs text-ct-muted">暂无判题测试用例</p>}
          </div>
        </section>

        {/* Test Cases — 前台运行（可见） */}
        <section className="mb-4">
          <h3 className="mb-1 text-sm font-semibold text-ct-accent">前台运行用例 ({p.visible_test_cases_list.length})</h3>
          <p className="mb-2 text-[10px] text-ct-muted">用户在"运行"标签页看到的测试用例</p>
          <div className="space-y-2">
            {p.visible_test_cases_list.map((tc, i) => (
              <div key={i} className="rounded border border-ct-border bg-slate-900/30 p-3 text-xs">
                <div className="mb-1 flex items-center gap-2">
                  <span className="font-bold text-ct-accent">用例 #{i + 1}</span>
                </div>
                <div className="text-ct-muted">输入: <code className="text-ct-text">{JSON.stringify(tc.input_args)}</code></div>
                <div className="text-ct-muted">输出: <code className="text-ct-text">{tc.expected_output}</code></div>
                {tc.explanation && <div className="text-ct-muted mt-1 italic">{tc.explanation}</div>}
              </div>
            ))}
            {p.visible_test_cases_list.length === 0 && <p className="text-xs text-ct-muted">暂无前台运行用例</p>}
          </div>
        </section>

        {/* Starter Code */}
        {p.starter_code && (
          <section className="mb-4">
            <h3 className="mb-1 text-sm font-semibold text-ct-accent">模板代码</h3>
            <pre className="rounded border border-ct-border bg-slate-900/50 p-3 text-xs font-mono text-ct-text overflow-x-auto">{p.starter_code}</pre>
          </section>
        )}

        {/* Brute Solution */}
        {p.brute_solution && (
          <section className="mb-4">
            <h3 className="mb-1 text-sm font-semibold text-ct-accent">暴力解</h3>
            <pre className="rounded border border-ct-border bg-slate-900/50 p-3 text-xs font-mono text-ct-text overflow-x-auto">{p.brute_solution}</pre>
          </section>
        )}

        {/* Actions */}
        <div className="flex gap-3 pt-2">
          <button
            onClick={() => handleEdit(p)}
            className="rounded-lg border border-ct-border px-4 py-2 text-sm text-ct-text hover:bg-slate-700/30"
          >
            编辑
          </button>
          <button
            onClick={() => setDeleteConfirm(p.id)}
            className="rounded-lg border border-red-800 px-4 py-2 text-sm text-red-400 hover:bg-red-900/20"
          >
            删除
          </button>
        </div>

        {/* Delete Confirmation */}
        {deleteConfirm === p.id && (
          <div className="mt-3 rounded-lg border border-red-800 bg-red-900/20 p-3">
            <p className="text-sm text-red-400">确定要删除「{p.title}」吗？此操作不可撤销。</p>
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => { handleDelete(p.id); setDeleteConfirm(null); }}
                className="rounded bg-red-600 px-3 py-1 text-xs text-white hover:bg-red-500"
              >
                确认删除
              </button>
              <button
                onClick={() => setDeleteConfirm(null)}
                className="rounded border border-ct-border px-3 py-1 text-xs text-ct-muted hover:text-ct-text"
              >
                取消
              </button>
            </div>
          </div>
        )}
      </div>
    );
  }

  // ── Edit Mode ──
  if (activeTab === 'edit' && selectedProblem) {
    return (
      <div className="h-full overflow-y-auto p-4">
        <div className="mb-4 flex items-center justify-between">
          <button onClick={handleBackToList} className="text-xs text-ct-muted hover:text-ct-text">← 返回列表</button>
          <button onClick={handleLogout} className="text-xs text-ct-muted hover:text-red-400">退出</button>
        </div>

        <h2 className="mb-4 text-lg font-bold text-ct-text">编辑: {selectedProblem.title}</h2>

        <div className="space-y-3">
          {/* Title */}
          <div>
            <label className="mb-1 block text-xs font-medium text-ct-muted">标题</label>
            <input
              type="text"
              value={editForm.title as string || ''}
              onChange={e => setEditForm(f => ({ ...f, title: e.target.value }))}
              className="w-full rounded border border-ct-border bg-slate-800/50 px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent"
            />
          </div>

          {/* Topic & Difficulty */}
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="mb-1 block text-xs font-medium text-ct-muted">知识点</label>
              <input
                type="text"
                value={editForm.topic as string || ''}
                onChange={e => setEditForm(f => ({ ...f, topic: e.target.value }))}
                className="w-full rounded border border-ct-border bg-slate-800/50 px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent"
              />
            </div>
            <div className="w-28">
              <label className="mb-1 block text-xs font-medium text-ct-muted">难度</label>
              <select
                value={editForm.difficulty as string || 'medium'}
                onChange={e => setEditForm(f => ({ ...f, difficulty: e.target.value }))}
                className="w-full rounded border border-ct-border bg-slate-800/50 px-2 py-2 text-sm text-ct-text outline-none focus:border-ct-accent"
              >
                <option value="easy">Easy</option>
                <option value="medium">Medium</option>
                <option value="hard">Hard</option>
              </select>
            </div>
          </div>

          {/* Description */}
          <div>
            <label className="mb-1 block text-xs font-medium text-ct-muted">题目描述</label>
            <textarea
              value={editForm.description as string || ''}
              onChange={e => setEditForm(f => ({ ...f, description: e.target.value }))}
              rows={6}
              className="w-full rounded border border-ct-border bg-slate-800/50 px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent"
            />
          </div>

          {/* Test Cases — 判题用（全量） */}
          <div>
            <label className="mb-1 block text-xs font-medium text-ct-muted">
              判题测试用例 (JSON)
              <span className="ml-1 text-[10px] text-ct-muted">后台判题套件，含隐藏用例</span>
            </label>
            <textarea
              value={editTestCases}
              onChange={e => setEditTestCases(e.target.value)}
              rows={8}
              className="w-full rounded border border-ct-border bg-slate-800/50 px-3 py-2 text-xs font-mono text-ct-text outline-none focus:border-ct-accent"
            />
          </div>

          {/* Visible Test Cases — 前台运行 */}
          <div>
            <label className="mb-1 block text-xs font-medium text-ct-muted">
              前台运行用例 (JSON)
              <span className="ml-1 text-[10px] text-ct-muted">用户在"运行"标签页看到的用例</span>
            </label>
            <textarea
              value={editVisibleTestCases}
              onChange={e => setEditVisibleTestCases(e.target.value)}
              rows={6}
              className="w-full rounded border border-ct-border bg-slate-800/50 px-3 py-2 text-xs font-mono text-ct-text outline-none focus:border-ct-accent"
            />
          </div>

          {/* Save & Back */}
          <div className="flex gap-3 pt-2">
            <button
              onClick={handleSave}
              className="rounded-lg bg-ct-accent px-5 py-2 text-sm font-medium text-white hover:opacity-90"
            >
              保存修改
            </button>
            <button
              onClick={() => { setActiveTab('view'); }}
              className="rounded border border-ct-border px-4 py-2 text-sm text-ct-text hover:bg-slate-700/30"
            >
              取消
            </button>
          </div>

          {saveMsg && <p className={`text-xs ${saveMsg.includes('✓') ? 'text-green-400' : 'text-red-400'}`}>{saveMsg}</p>}
        </div>
      </div>
    );
  }

  // ── List Mode (default) ──
  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-ct-border px-4 py-3">
        <h2 className="text-sm font-bold text-ct-text">🛡️ 管理页面 ({problems.length} 题)</h2>
        <button onClick={handleLogout} className="text-xs text-ct-muted hover:text-red-400">退出</button>
      </div>

      {/* Loading */}
      {loading && (
        <div className="flex flex-1 items-center justify-center">
          <div className="flex items-center gap-2 text-sm text-ct-muted">
            <div className="h-4 w-4 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
            加载中…
          </div>
        </div>
      )}

      {/* Problem List */}
      {!loading && (
        <div className="flex-1 overflow-y-auto">
          {problems.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <p className="text-sm text-ct-muted">题库为空</p>
            </div>
          ) : (
            <div className="divide-y divide-ct-border/50">
              {problems.map(p => (
                <div key={p.id} className="group px-4 py-3 hover:bg-slate-800/20">
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
                      <button
                        onClick={() => handleView(p)}
                        className="rounded border border-ct-border px-2 py-1 text-[10px] text-ct-muted hover:border-ct-accent hover:text-ct-accent"
                        title="查看详情"
                      >
                        查看
                      </button>
                      <button
                        onClick={() => handleEdit(p)}
                        className="rounded border border-ct-border px-2 py-1 text-[10px] text-ct-muted hover:border-ct-accent hover:text-ct-accent"
                        title="编辑"
                      >
                        编辑
                      </button>
                      <button
                        onClick={() => setDeleteConfirm(p.id)}
                        className="rounded border border-ct-border px-2 py-1 text-[10px] text-ct-muted hover:border-red-600 hover:text-red-400"
                        title="删除"
                      >
                        删除
                      </button>
                    </div>
                  </div>

                  {/* Delete Confirmation per item */}
                  {deleteConfirm === p.id && (
                    <div className="mt-2 flex items-center gap-2 rounded border border-red-800 bg-red-900/20 p-2">
                      <span className="text-xs text-red-400">确认删除？</span>
                      <button
                        onClick={() => { handleDelete(p.id); setDeleteConfirm(null); }}
                        className="rounded bg-red-600 px-2 py-0.5 text-[10px] text-white hover:bg-red-500"
                      >
                        确认
                      </button>
                      <button
                        onClick={() => setDeleteConfirm(null)}
                        className="rounded border border-ct-border px-2 py-0.5 text-[10px] text-ct-muted hover:text-ct-text"
                      >
                        取消
                      </button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
