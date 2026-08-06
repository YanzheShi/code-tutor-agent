import { useCallback, useEffect, useState } from 'react';
import { API_BASE } from '../api/config';

const BASE = API_BASE;

type Tab = 'existing' | 'agent' | 'profile' | 'admin';
type ProblemBrief = { id: number; title: string; topic: string; difficulty: string; verdict?: string };

/* ── ProfileView 子组件 ── */

function ProfileView() {
  const [profile, setProfile] = useState<Record<string, unknown> | null>(null);
  const [profileV2, setProfileV2] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [showAllTags, setShowAllTags] = useState(false);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      fetch(BASE + '/admin/profile').then(r => r.ok ? r.json() : null),
      fetch(BASE + '/admin/profile/v2').then(r => r.ok ? r.json() : null),
    ]).then(([p1, p2]) => {
      setProfile(p1);
      setProfileV2(p2);
      setLoading(false);
    }).catch(() => {
      setProfile(null);
      setProfileV2(null);
      setLoading(false);
    });
  }, []);

  if (loading) return <p className="text-sm text-ct-muted text-center py-8">加载画像中...</p>;
  if (!profile) return <p className="text-sm text-ct-muted text-center py-8">暂无画像数据，做几道题后再来看看</p>;

  const bar = (val: number, color: string) => (
    <div className="h-2 w-full rounded-full bg-ct-hover">
      <div className={'h-2 rounded-full ' + color} style={{ width: Math.round(val * 100) + '%' }} />
    </div>
  );

  // 解析 per-tag 数据
  const v2Prof = profileV2?.prof as Record<string, number> | undefined;
  const v2Stab = profileV2?.stab as Record<string, { variance: number }> | undefined;
  const tagNames = (profileV2?.tag_names ?? {}) as Record<string, string>;
  const tagEntries = v2Prof ? Object.entries(v2Prof).sort((a, b) => b[1] - a[1]) : [];

  const MAX_VISIBLE = 5;
  const displayedTags = showAllTags ? tagEntries : tagEntries.slice(0, MAX_VISIBLE);
  const hiddenCount = tagEntries.length - MAX_VISIBLE;

  return (
    <section className="space-y-4">
      <h2 className="text-sm font-semibold text-ct-text">📊 我的画像</h2>

      {/* 总体指标 */}
      <div className="rounded-lg border border-ct-border bg-ct-surface p-4 space-y-3 text-sm">
        <div className="flex justify-between mb-1">
          <span className="text-ct-muted">综合熟练度</span>
          <span className="text-ct-text font-mono">{((profile.proficiency as number) * 100).toFixed(0)}%</span>
        </div>
        {bar(profile.proficiency as number, 'bg-ct-accent')}
        <div className="flex justify-between mb-1">
          <span className="text-ct-muted">稳定性</span>
          <span className="text-ct-text font-mono">{((profile.stability as number) * 100).toFixed(0)}%</span>
        </div>
        {bar(profile.stability as number, 'bg-ct-success')}
        <div className="grid grid-cols-2 gap-4 pt-1">
          <div>
            <span className="text-ct-muted text-xs">做题数</span>
            <p className="text-ct-text font-mono text-lg">{profile.attempts as number}</p>
          </div>
          <div>
            <span className="text-ct-muted text-xs">距离上次</span>
            <p className="text-ct-text font-mono text-lg">{profile.forget_days as number} 天</p>
          </div>
        </div>
        {(profile.common_errors as string[]).length > 0 && (
          <div>
            <span className="text-ct-muted text-xs block mb-1">常见错误</span>
            <div className="flex flex-wrap gap-1">
              {(profile.common_errors as string[]).map((e, i) => (
                <span key={i} className="rounded bg-ct-error-bg px-2 py-0.5 text-xs text-ct-error">{e}</span>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Per-tag 画像 */}
      {tagEntries.length > 0 && (
        <div className="rounded-lg border border-ct-border bg-ct-surface p-4 space-y-2 text-sm">
          <h3 className="text-xs font-semibold text-ct-muted mb-2">
            各知识点熟练度（{tagEntries.length} 个）
          </h3>
          {displayedTags.map(([tag, prof]) => (
            <div key={tag}>
              <div className="flex justify-between text-xs mb-0.5">
                <span className="text-ct-muted">{tagNames[tag] ?? tag}</span>
                <span className="text-ct-text font-mono">{(prof * 100).toFixed(0)}%</span>
              </div>
              {bar(prof, 'bg-ct-accent')}
            </div>
          ))}
          {!showAllTags && hiddenCount > 0 && (
            <button
              onClick={() => setShowAllTags(true)}
              className="w-full text-xs text-ct-accent hover:text-ct-accent/80 pt-1 transition"
            >
              展开全部（共 {tagEntries.length} 个知识点）
            </button>
          )}
          {showAllTags && (
            <button
              onClick={() => setShowAllTags(false)}
              className="w-full text-xs text-ct-muted hover:text-ct-text pt-1 transition"
            >
              收起
            </button>
          )}
        </div>
      )}

      <p className="text-xs text-ct-muted text-center">
        画像在每次判题后自动更新，根据熟练度规划下一题难度
      </p>
    </section>
  );
}

/* ── 主组件 ── */

export default function WelcomeScreen({
  onStart,
  onStartExisting,
  onOpenAdmin,
}: {
  onStart: (topic: string, difficulty: string, mode: string) => void;
  onStartExisting?: (problemId: number) => void;
  onOpenAdmin?: () => void;
}) {
  const [tab, setTab] = useState<Tab>('agent');
  const [problems, setProblems] = useState<ProblemBrief[]>([]);
  const [problemsLoading, setProblemsLoading] = useState(false);
  const [selectedPid, setSelectedPid] = useState<number | null>(null);

  useEffect(() => {
    if (tab === 'existing') {
      setProblemsLoading(true);
      fetch(BASE + '/problems')
        .then(r => r.json())
        .then(data => setProblems(data.problems ?? []))
        .catch(() => setProblems([]))
        .finally(() => setProblemsLoading(false));
    }
  }, [tab]);

  return (
    <div className="flex h-screen items-center justify-center bg-ct-bg">
      <div className="w-full max-w-2xl space-y-6 px-6">
        <div className="text-center">
          <h1 className="text-3xl font-bold text-ct-text">🤖 CodeTutor Agent</h1>
          <p className="mt-2 text-ct-muted">AI 编程私教 · 自主出题 · 对抗判题 · 渐进辅导</p>
        </div>

        {/* 标签切换 */}
        <div className="flex gap-1 rounded-lg bg-ct-input p-1">
          {[
            { id: 'agent' as Tab, label: '🤖 Agent 导师' },
            { id: 'existing' as Tab, label: '从题库选' },
            { id: 'profile' as Tab, label: '📊 我的画像' },
            ...(onOpenAdmin ? [{ id: 'admin' as Tab, label: '🛡️ 管理' }] : []),
          ].map(t => (
            t.id === 'admin' ? (
              <button key="admin" onClick={() => onOpenAdmin?.()}
                className={`flex-1 rounded-md py-2 text-sm font-medium transition ${tab === 'admin' ? 'bg-ct-accent text-white' : 'text-ct-muted hover:text-ct-text'}`}>
                {t.label}
              </button>
            ) : (
              <button key={t.id} onClick={() => setTab(t.id)}
                className={`flex-1 rounded-md py-2 text-sm font-medium transition ${tab === t.id ? 'bg-ct-accent text-white' : 'text-ct-muted hover:text-ct-text'}`}>
                {t.label}
              </button>
            )
          ))}
        </div>

        {/* ── 从题库选 ── */}
        {tab === 'existing' && (
          <section>
            <h2 className="mb-3 text-sm font-semibold text-ct-text">已有题目</h2>
            {problemsLoading ? (
              <p className="text-sm text-ct-muted">加载中…</p>
            ) : problems.length === 0 ? (
              <p className="text-sm text-ct-muted">题库为空，先用 AI 出几道题吧</p>
            ) : (
              <div className="max-h-96 space-y-1 overflow-y-auto">
                {problems.map(p => {
                  const verdictIcon = p.verdict === 'AC' ? '✅' : p.verdict ? '⏸' : '';
                  const verdictTitle = p.verdict === 'AC' ? '已通过' : p.verdict ? '已提交' : '';
                  return (
                  <button key={p.id} onClick={() => setSelectedPid(p.id)}
                    className={`w-full rounded-lg border px-4 py-2 text-left text-sm transition ${selectedPid === p.id ? 'border-ct-accent bg-ct-accent/10 text-ct-accent' : 'border-ct-border text-ct-muted hover:border-ct-accent/50'}`}>
                    <span className="mr-1 inline-block w-5 text-center" title={verdictTitle}>{verdictIcon}</span>
                    <span className="font-medium text-ct-text">{p.id}. {p.title}</span>
                    <span className="ml-2 text-xs">{p.topic}</span>
                    <span className={`ml-2 rounded px-1.5 py-0.5 text-xs ${p.difficulty === 'easy' ? 'bg-ct-success-bg text-ct-success' : p.difficulty === 'medium' ? 'bg-ct-warn-bg text-ct-warn' : 'bg-ct-error-bg text-ct-error'}`}>{p.difficulty}</span>
                  </button>
                  );
                })}
              </div>
            )}
            <button onClick={() => selectedPid && onStartExisting?.(selectedPid)} disabled={!selectedPid}
              className="mt-4 w-full rounded-lg bg-ct-accent py-3 text-base font-semibold text-white transition hover:opacity-90 disabled:opacity-40">
              开始练习
            </button>
          </section>
        )}

        {/* ── Agent 导师模式 ── */}
        {tab === 'agent' && (
          <section className="text-center">
            <div className="mb-4 rounded-lg border border-ct-border bg-ct-surface p-6">
              <p className="text-lg font-medium text-ct-text">🧑‍🏫 Agent 导师模式</p>
              <p className="mt-2 text-sm text-ct-muted">与 AI 导师直接对话，告诉 TA 你想练什么类型、难度、具体方向的题目。</p>
              <ul className="mt-3 space-y-1 text-left text-xs text-ct-muted">
                <li>💬 自然对话，告诉 AI 你想练什么</li>
                <li>🎯 AI 会追问细节，确保题目贴合你的需求</li>
                <li>🧠 提交后 AI 判题，给出温暖反馈和修复建议</li>
                <li>🔄 未通过可以多次修改，AI 持续辅导直到 AC</li>
              </ul>
            </div>
            <button onClick={() => onStart('', '', 'agent')}
              className="w-full rounded-lg bg-ct-accent py-3 text-base font-semibold text-white transition hover:opacity-90">
              开始对话
            </button>
          </section>
        )}

        {/* ── 我的画像 ── */}
        {tab === 'profile' && <ProfileView />}
      </div>
    </div>
  );
}