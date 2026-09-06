import { changePassword, isAdmin } from '../api/auth';
import { apiFetch } from '../api/client';
import { useEffect, useMemo, useState } from 'react';
import { API_BASE } from '../api/config';

const BASE = API_BASE;

type Tab = 'existing' | 'agent' | 'profile' | 'subs' | 'admin';
type ProblemBrief = { id: number; title: string; topic: string; difficulty: string; verdict?: string };

type MySubmission = { id: number; problem_id: number; problem_title: string; verdict: string; code: string; created_at: string };

const VERDICT_COLORS: Record<string, string> = {
  AC: 'text-ct-success', WA: 'text-ct-warn',
  TLE: 'text-ct-error', RE: 'text-ct-error', CE: 'text-ct-error',
};

/* ── 我的提交（个人中心：所有登录用户可看自己的记录）── */
function MySubmissionsView() {
  const [subs, setSubs] = useState<MySubmission[] | null>(null);
  const [expandedCode, setExpandedCode] = useState<number | null>(null);

  useEffect(() => {
    apiFetch(BASE + '/auth/me/submissions')
      .then(r => r.ok ? r.json() : null)
      .then(d => setSubs((d?.submissions ?? []) as MySubmission[]))
      .catch(() => setSubs([]));
  }, []);

  if (subs === null) return <p className="text-sm text-ct-muted text-center py-8">加载提交记录中...</p>;
  if (subs.length === 0) return <p className="text-sm text-ct-muted text-center py-8">还没有提交记录，去做几道题吧</p>;

  return (
    <div className="space-y-1.5">
      <div className="divide-y divide-ct-border/50 rounded-xl border border-ct-border bg-ct-surface">
        {subs.map(sub => (
          <div key={sub.id} className="px-3 py-2.5">
            <div className="flex items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-2 text-sm">
                <span className="truncate font-medium text-ct-text">#{sub.problem_id} {sub.problem_title}</span>
                <span className={`shrink-0 font-bold text-xs ${VERDICT_COLORS[sub.verdict] || 'text-ct-muted'}`}>{sub.verdict || 'PENDING'}</span>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <span className="text-[11px] text-ct-muted">{sub.created_at}</span>
                <button onClick={() => setExpandedCode(expandedCode === sub.id ? null : sub.id)}
                  className="text-xs text-ct-accent hover:underline">
                  {expandedCode === sub.id ? '收起' : '查看代码'}
                </button>
              </div>
            </div>
            {expandedCode === sub.id && (
              <pre className="mt-2 max-h-48 overflow-x-auto rounded border border-ct-border bg-ct-surface-secondary p-3 text-xs font-mono text-ct-text">{sub.code}</pre>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}


/* ── 错误模式 6 维定义（与后端 weakness.py DIM_KEYS / DIM_DISPLAY 同步）── */
const ERROR_MODE_DIMS = [
  { key: 'correctness', label: '正确性 & 边界', icon: '🎯',
    desc: '边界条件、空值/None 处理、索引越界、类型转换等正确性陷阱', color: '#cf222e' },
  { key: 'datastruct', label: '数据结构操作', icon: '🗂️',
    desc: '数组/链表/树/图/哈希的增删改查、遍历顺序与指针操作', color: '#0969da' },
  { key: 'perf', label: '复杂度 & 性能', icon: '⚡',
    desc: '时间/空间复杂度分析、TLE/OOM 风险与优化策略选择', color: '#9a6700' },
  { key: 'algo', label: '算法思维', icon: '🧠',
    desc: '问题建模、状态转移、贪心/DP/图论等算法范式运用', color: '#1a7f37' },
  { key: 'impl', label: '实现质量 & 鲁棒性', icon: '🔧',
    desc: '代码可读性、命名规范、异常处理、输入校验与防御编程', color: '#8957e5' },
  { key: 'debug', label: '自测 & 调试', icon: '🔍',
    desc: '自测用例构造、边界场景覆盖、调试效率与错误定位能力', color: '#bf5700' },
] as const;

type ErrorModeInfo = { count: number; severity: number; last_seen?: string; evidence?: string };
type ErrorModes = Record<string, Record<string, ErrorModeInfo>>;

/** 无数据维度的默认分：中性「未知」，不奖励也不惩罚。
 *  做题暴露弱项 → 扣分（severity 1 扣到 3）；持续干净练习 → severity 衰减回升。
 *  取 5 而非更低，是因为可得分区间为 [3, 10]，默认分低于 3 会低于可达下限。 */
const NO_DATA_SCORE = 5.0;

/** 从 error_modes 计算每个维度的"能力分"(0~10)。无数据=5(中性未知), 有弱项则扣分。 */
function dimScores(modes: ErrorModes): number[] {
  return ERROR_MODE_DIMS.map(d => {
    const tags = modes[d.key];
    if (!tags || Object.keys(tags).length === 0) return NO_DATA_SCORE;
    // 取该维度下最严重的 tag 的 severity，映射到扣分：severity 1→扣到 3 分，severity 0→不扣
    const maxSev = Math.max(...Object.values(tags).map(t => t.severity));
    return Math.round((1 - maxSev * 0.7) * 10 * 10) / 10; // 保留一位小数
  });
}

/* ── 纯 SVG 六维雷达图 ──
   viewBox 加宽加高（600×480），把六个轴标签完整容纳在内，
   避免长标签（如"数据结构操作"）被裁切或贴到数值上。
   标签对齐按数据点的水平方向判断：点在中心右侧→start，左侧→end，正中→middle。 */

function RadarChart({ scores }: { scores: number[] }) {
  const W = 600;
  const H = 480;
  const cx = W / 2;
  const cy = H / 2;
  const r = 150;
  const labelDist = r + 44; // 标签距圆心，留出标签与数值间的空隙
  const levels = 5;
  const angleStep = (Math.PI * 2) / 6;

  // 网格多边形顶点 (level 0..levels)
  const gridPolys = Array.from({ length: levels }, (_, li) => {
    const lr = r * ((li + 1) / levels);
    return Array.from({ length: 6 }, (_, i) => {
      const a = -Math.PI / 2 + i * angleStep;
      return [cx + lr * Math.cos(a), cy + lr * Math.sin(a)];
    });
  });

  // 数据多边形
  const dataPoints = scores.map((s, i) => {
    const a = -Math.PI / 2 + i * angleStep;
    const sr = r * (Math.max(0, Math.min(s, 10)) / 10);
    return [cx + sr * Math.cos(a), cy + sr * Math.sin(a)];
  });

  // 轴标签：按数据点水平方向决定对齐，保证文字始终落在 viewBox 内、远离数值
  const labels = ERROR_MODE_DIMS.map((d, i) => {
    const a = -Math.PI / 2 + i * angleStep;
    const lx = cx + labelDist * Math.cos(a);
    const ly = cy + labelDist * Math.sin(a);
    const align = lx > cx + 0.5 ? 'start' : lx < cx - 0.5 ? 'end' : 'middle' as const;
    return { x: lx, y: ly, align, label: d.label.replace(/ &.*/, '') };
  });

  const avg = scores.reduce((a, b) => a + b, 0) / scores.length;

  const polyToS = (pts: number[][]) => pts.map(p => p.join(',')).join(' ');

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto"
      role="img" aria-label="六维能力雷达图">
      {/* grid rings */}
      {gridPolys.map((gp, i) => (
        <polygon key={i} points={polyToS(gp)}
          fill="none" stroke="var(--ct-border)" strokeWidth={0.7} />
      ))}
      {/* axes */}
      {Array.from({ length: 6 }, (_, i) => {
        const a = -Math.PI / 2 + i * angleStep;
        return (
          <line key={i} x1={cx} y1={cy}
            x2={cx + r * Math.cos(a)} y2={cy + r * Math.sin(a)}
            stroke="var(--ct-border)" strokeWidth={0.7} />
        );
      })}
      {/* data area */}
      <polygon points={polyToS(dataPoints)}
        fill="var(--ct-accent)" fillOpacity={0.14}
        stroke="var(--ct-accent)" strokeWidth={1.8} strokeLinejoin="round" />
      {/* dots + values */}
      {dataPoints.map((p, i) => (
        <g key={i}>
          <circle cx={p[0]} cy={p[1]} r={5}
            fill="var(--ct-accent)" stroke="#fff" strokeWidth={1.2} />
          <text x={p[0]} y={p[1] - 15} textAnchor="middle"
            fontSize={13} fontWeight={600} fill="var(--ct-text)">
            {scores[i].toFixed(1)}
          </text>
        </g>
      ))}
      {/* axis labels */}
      {labels.map((l, i) => (
        <text key={i} x={l.x} y={l.y} textAnchor={l.align as 'start' | 'end' | 'middle'}
          dominantBaseline="central"
          fontSize={12} fontWeight={500} fill="var(--ct-muted)">
          {l.label}
        </text>
      ))}
      {/* center score */}
      <text x={cx} y={cy - 2} textAnchor="middle" fontSize={26} fontWeight={700}
        fill="var(--ct-accent)">
        {avg.toFixed(1)}
      </text>
      <text x={cx} y={cy + 16} textAnchor="middle" fontSize={11}
        fill="var(--ct-muted)">
        均分
      </text>
    </svg>
  );
}

/* ── 维度卡片 ── */

function DimCard({ dim, tags }: { dim: typeof ERROR_MODE_DIMS[number]; tags?: Record<string, ErrorModeInfo> }) {
  const topTags = tags ? Object.entries(tags)
    .sort((a, b) => b[1].count * b[1].severity - a[1].count * a[1].severity)
    .slice(0, 3) : [];

  return (
    <div className="rounded-lg border border-ct-border bg-ct-surface p-3 space-y-1.5 transition hover:border-ct-accent/40">
      <div className="flex items-center gap-1.5">
        <span className="text-sm">{dim.icon}</span>
        <span className="text-xs font-semibold text-ct-text">{dim.label}</span>
      </div>
      <p className="text-[11px] leading-relaxed text-ct-muted">{dim.desc}</p>
      {topTags.length > 0 && (
        <div className="flex flex-wrap gap-1 pt-0.5">
          {topTags.map(([tag, info]) => (
            <span key={tag}
              className="inline-flex items-center gap-0.5 rounded-full bg-ct-warn-bg px-2 py-0.5 text-[10px] font-medium text-ct-warn"
              title={`命中 ${info.count} 次 · 严重度 ${(info.severity * 100).toFixed(0)}%`}>
              {tag}
              <span className="opacity-70">{(info.severity * 100).toFixed(0)}</span>
            </span>
          ))}
        </div>
      )}
      {(!tags || Object.keys(tags).length === 0) && (
        <span className="inline-block text-[10px] text-ct-success font-medium">暂无弱项</span>
      )}
    </div>
  );
}

/* ── ProfileView 子组件 ── */

function ProfileView() {
  const [profile, setProfile] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    apiFetch(BASE + '/auth/me/profile')
      .then(r => r.ok ? r.json() : null)
      .then(setProfile)
      .catch(() => setProfile(null))
      .finally(() => setLoading(false));
  }, []);

  // hooks 必须在 early return 之前调用，保持调用顺序稳定
  const modes = (profile?.error_modes ?? {}) as ErrorModes;
  const scores = useMemo(() => dimScores(modes), [modes]);

  if (loading) return <p className="text-sm text-ct-muted text-center py-8">加载画像中...</p>;
  if (!profile) return <p className="text-sm text-ct-muted text-center py-8">暂无画像数据，做几道题后再来看看</p>;

  return (
    <section className="space-y-5">
      {/* 标题行 */}
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-ct-text">📊 能力画像</h2>
        <span className="text-[11px] text-ct-muted">基于 {(profile.attempts as number) ?? 0} 次提交 · 更新于最近一次判题</span>
      </div>

      {/* 雷达图：撑满卡片可用宽度，首屏主导展示 */}
      <div className="rounded-xl border border-ct-border bg-ct-surface p-4">
        <RadarChart scores={scores} />
      </div>

      {/* 6 维度卡片（滚动到此处才展示） */}
      <div className="grid grid-cols-2 gap-2.5">
        {ERROR_MODE_DIMS.map(d => (
          <DimCard key={d.key} dim={d} tags={modes[d.key]} />
        ))}
      </div>

      {/* 底部提示 */}
      <p className="text-[11px] text-ct-muted text-center pt-1">
        分数越低表示该维度越需关注 · 点击各维度标签查看具体弱项 · 画像在每次提交后自动更新
      </p>
    </section>
  );
}

/* ── 主组件 ── */

export default function WelcomeScreen({
  onStart,
  onStartExisting,
  onOpenAdmin,
  onOpenSettings,
  onLogout,
  user,
}: {
  onStart: (topic: string, difficulty: string, mode: string) => void;
  onStartExisting?: (problemId: number) => void;
  onOpenAdmin?: () => void;
  onOpenSettings?: () => void;
  onLogout?: () => void;
  user?: { id: number; email: string; role: string } | null;
}) {
  const [tab, setTab] = useState<Tab>('agent');
  const [problems, setProblems] = useState<ProblemBrief[]>([]);
  const [problemsLoading, setProblemsLoading] = useState(false);
  const [selectedPid, setSelectedPid] = useState<number | null>(null);

  useEffect(() => {
    if (tab === 'existing') {
      setProblemsLoading(true);
      apiFetch(BASE + '/problems')
        .then(r => r.json())
        .then(data => setProblems(data.problems ?? []))
        .catch(() => setProblems([]))
        .finally(() => setProblemsLoading(false));
    }
  }, [tab]);

  const tabs = [
    { id: 'agent' as Tab, label: '🤖 Agent 导师' },
    { id: 'existing' as Tab, label: '从题库选' },
    { id: 'profile' as Tab, label: '📊 我的画像' },
    { id: 'subs' as Tab, label: '📋 我的提交' },
    // 管理入口仅 admin 角色可见（多用户改造 P4）
    ...(onOpenAdmin && isAdmin() ? [{ id: 'admin' as Tab, label: '🛡️ 管理' }] : []),
  ];

  return (
    <div className="flex min-h-screen items-center justify-center bg-ct-bg p-4">
      {/* 固定高度卡片：标题+标签栏+内容整体打包，所有 tab 共享同一卡片高度 → 切换零跳动 */}
      <div className="flex w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-ct-border bg-ct-surface px-6 py-6 shadow-sm max-h-[calc(100vh-2rem)] min-h-[520px] h-[760px]">
        {/* 标题（钉在卡片顶部，不随内容移动） */}
        <div className="relative shrink-0 text-center">
          <h1 className="text-3xl font-bold text-ct-text">🤖 CodeTutor Agent</h1>
          <p className="mt-2 text-ct-muted">AI 编程私教 · 自主出题 · 对抗判题 · 渐进辅导</p>
          {(onOpenSettings || onLogout) && (
            <div className="absolute right-0 top-0 flex gap-2">
              {onOpenSettings && (
                <button
                  type="button"
                  onClick={onOpenSettings}
                  title="设置（主题 / 模型服务）"
                  className="rounded-lg border border-ct-border bg-ct-panel px-3 py-1.5 text-xs font-medium text-ct-muted transition hover:border-ct-accent/50 hover:text-ct-text"
                >
                  ⚙️ 设置
                </button>
              )}
              {onLogout && (
                <button
                  type="button"
                  onClick={onLogout}
                  title={user?.email ? `退出登录 (${user.email})` : '退出登录'}
                  className="rounded-lg border border-ct-border bg-ct-panel px-3 py-1.5 text-xs font-medium text-ct-muted transition hover:border-ct-accent/50 hover:text-ct-text"
                >
                  退出登录
                </button>
              )}
            </div>
          )}
        </div>

        {/* 标签切换（固定，切换 tab 不漂移） */}
        <div className="mt-5 flex shrink-0 gap-1 rounded-lg bg-ct-input p-1">
          {tabs.map(t => (
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

        {/* 内容区：固定卡片高度内滚动，矮内容居中、高内容滚动 */}
        <div className="mt-5 flex-1 overflow-y-auto">
          {/* ── 从题库选 ── */}
          {tab === 'existing' && (
            <section className="flex h-full flex-col">
              <h2 className="mb-3 shrink-0 text-sm font-semibold text-ct-text">已有题目</h2>
              {problemsLoading ? (
                <p className="text-sm text-ct-muted">加载中…</p>
              ) : problems.length === 0 ? (
                <p className="text-sm text-ct-muted">题库为空，先用 AI 出几道题吧</p>
              ) : (
                <div className="flex-1 overflow-y-auto space-y-1 pr-1">
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
                className="mt-3 w-full shrink-0 rounded-lg border-t border-ct-border bg-ct-accent py-3 text-base font-semibold text-white transition hover:opacity-90 disabled:opacity-40">
                开始练习
              </button>
            </section>
          )}

          {/* ── Agent 导师模式（矮内容，居中留白） ── */}
          {tab === 'agent' && (
            <section className="flex min-h-full flex-col justify-center text-center">
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
          {tab === 'profile' && <ChangePasswordCard />}

          {/* ── 我的提交 ── */}
          {tab === 'subs' && <MySubmissionsView />}
        </div>
      </div>
    </div>
  );
}


/* ── 自助改密卡片（防滥用改造，2026-09-06）── */

function ChangePasswordCard() {
  const [open, setOpen] = useState(false);
  const [oldPw, setOldPw] = useState('');
  const [newPw, setNewPw] = useState('');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState('');
  const [err, setErr] = useState('');

  const submit = async () => {
    if (newPw.length < 8 || busy) return;
    setBusy(true); setMsg(''); setErr('');
    try {
      await changePassword(oldPw, newPw);
      setMsg('密码已更新');
      setOldPw(''); setNewPw('');
    } catch (e) {
      setErr(e instanceof Error ? e.message : '修改失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-6 rounded-xl border border-ct-border bg-ct-panel p-4">
      <button
        type="button"
        onClick={() => { setOpen(!open); setMsg(''); setErr(''); }}
        className="text-sm font-medium text-ct-muted hover:text-ct-text">
        🔑 修改密码 {open ? '▲' : '▼'}
      </button>
      {open && (
        <div className="mt-3 space-y-2">
          <input type="password" value={oldPw} onChange={e => setOldPw(e.target.value)}
            placeholder="当前密码" autoComplete="current-password"
            className="w-full rounded-lg border border-ct-border bg-ct-bg px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent" />
          <input type="password" value={newPw} onChange={e => setNewPw(e.target.value)}
            placeholder="新密码（至少 8 位）" autoComplete="new-password"
            className="w-full rounded-lg border border-ct-border bg-ct-bg px-3 py-2 text-sm text-ct-text outline-none focus:border-ct-accent" />
          <button type="button" onClick={submit}
            disabled={oldPw.length < 1 || newPw.length < 8 || busy}
            className="rounded-lg bg-ct-accent px-4 py-1.5 text-sm font-medium text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40">
            {busy ? '提交中…' : '确认修改'}
          </button>
          {msg && <p className="text-xs text-ct-success">{msg}</p>}
          {err && <p className="text-xs text-red-500">{err}</p>}
          <p className="text-[11px] text-ct-muted">忘记当前密码？请联系管理员重置（或走登录页「忘记密码」流程）。</p>
        </div>
      )}
    </div>
  );
}