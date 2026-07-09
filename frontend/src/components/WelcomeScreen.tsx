import { useCallback, useEffect, useState } from 'react';

const TOPICS = [
  { id: '数组', label: '数组', desc: '遍历、查找、排序基础' },
  { id: '数组+哈希表', label: '数组+哈希表', desc: '空间换时间，O(n) 查找' },
  { id: '双指针', label: '双指针', desc: '滑动窗口、相向指针' },
  { id: '链表', label: '链表', desc: '反转、环检测、合并' },
  { id: '动态规划', label: '动态规划', desc: 'DP 状态定义与转移' },
  { id: '二分查找', label: '二分查找', desc: '有序数组中的搜索' },
  { id: '递归', label: '递归', desc: '递归思维与回溯' },
];

const DIFFICULTIES = [
  { id: 'easy', label: 'Easy', color: 'bg-green-900/50 text-green-400 border-green-700' },
  { id: 'medium', label: 'Medium', color: 'bg-amber-900/50 text-amber-400 border-amber-700' },
  { id: 'hard', label: 'Hard', color: 'bg-red-900/50 text-red-400 border-red-700' },
];

type Tab = 'ai' | 'existing' | 'leetcode' | 'agent';

type ProblemBrief = { id: number; title: string; topic: string; difficulty: string };

export default function WelcomeScreen({
  onStart,
  onStartExisting,
  onStartLeetcode,
  onOpenAdmin,
}: {
  onStart: (topic: string, difficulty: string, mode: string) => void;
  onStartExisting?: (problemId: number) => void;
  onStartLeetcode?: (url: string) => void;
  onOpenAdmin?: () => void;
}) {
  const [tab, setTab] = useState<Tab>('ai');
  const [topic, setTopic] = useState('数组');
  const [difficulty, setDifficulty] = useState('easy');
  const [problems, setProblems] = useState<ProblemBrief[]>([]);
  const [problemsLoading, setProblemsLoading] = useState(false);
  const [leetcodeUrl, setLeetcodeUrl] = useState('');
  const [selectedPid, setSelectedPid] = useState<number | null>(null);

  // Fetch existing problems
  useEffect(() => {
    if (tab === 'existing') {
      setProblemsLoading(true);
      fetch('http://localhost:8765/problems')
        .then(r => r.json())
        .then(data => setProblems(data.problems ?? []))
        .catch(() => setProblems([]))
        .finally(() => setProblemsLoading(false));
    }
  }, [tab]);

  return (
    <div className="flex h-screen items-center justify-center bg-ct-bg">
      <div className="w-full max-w-2xl space-y-6 px-6">
        {/* 标题 */}
        <div className="text-center">
          <h1 className="text-3xl font-bold text-ct-text">🤖 CodeTutor Agent</h1>
          <p className="mt-2 text-ct-muted">
            AI 编程私教 · 自主出题 · 对抗判题 · 渐进辅导
          </p>
        </div>

        {/* 标签切换 */}
        <div className="flex gap-1 rounded-lg bg-slate-800/50 p-1">
          {[
            { id: 'ai' as Tab, label: 'AI 出题' },
            { id: 'agent' as Tab, label: '🤖 Agent 导师' },
            { id: 'existing' as Tab, label: '从题库选' },
            { id: 'leetcode' as Tab, label: 'LeetCode 链接' },
            ...(onOpenAdmin ? [{ id: 'admin' as Tab, label: '🛡️ 管理' }] : []),
          ].map(t => (
            t.id === 'admin' ? (
              <button
                key="admin"
                onClick={() => onOpenAdmin?.()}
                className={`flex-1 rounded-md py-2 text-sm font-medium transition ${
                  tab === 'admin' ? 'bg-ct-accent text-white' : 'text-ct-muted hover:text-ct-text'
                }`}
              >
                {t.label}
              </button>
            ) : (
              <button
                key={t.id}
                onClick={() => setTab(t.id)}
                className={`flex-1 rounded-md py-2 text-sm font-medium transition ${
                  tab === t.id ? 'bg-ct-accent text-white' : 'text-ct-muted hover:text-ct-text'
                }`}
              >
                {t.label}
              </button>
            )
          ))}
        </div>

        {/* ── AI 出题 ── */}
        {tab === 'ai' && (
          <>
            <section>
              <h2 className="mb-3 text-sm font-semibold text-ct-text">选择知识点</h2>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {TOPICS.map(t => (
                  <button
                    key={t.id}
                    onClick={() => setTopic(t.id)}
                    className={`rounded-lg border px-3 py-2 text-left text-sm transition ${
                      topic === t.id
                        ? 'border-ct-accent bg-ct-accent/10 text-ct-accent'
                        : 'border-ct-border text-ct-muted hover:border-ct-accent/50'
                    }`}
                  >
                    <div className="font-medium">{t.label}</div>
                    <div className="mt-0.5 text-xs opacity-70">{t.desc}</div>
                  </button>
                ))}
              </div>
            </section>

            <section>
              <h2 className="mb-3 text-sm font-semibold text-ct-text">选择难度</h2>
              <div className="flex gap-3">
                {DIFFICULTIES.map(d => (
                  <button
                    key={d.id}
                    onClick={() => setDifficulty(d.id)}
                    className={`flex-1 rounded-lg border px-4 py-2 text-center text-sm font-medium transition ${
                      difficulty === d.id
                        ? d.color + ' border-2'
                        : 'border-ct-border text-ct-muted hover:border-ct-accent/50'
                    }`}
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            </section>

            <button
              onClick={() => onStart(topic, difficulty, 'practice')}
              className="w-full rounded-lg bg-ct-accent py-3 text-base font-semibold text-white transition hover:opacity-90"
            >
              开始练习
            </button>
          </>
        )}

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
                {problems.map(p => (
                  <button
                    key={p.id}
                    onClick={() => setSelectedPid(p.id)}
                    className={`w-full rounded-lg border px-4 py-2 text-left text-sm transition ${
                      selectedPid === p.id
                        ? 'border-ct-accent bg-ct-accent/10 text-ct-accent'
                        : 'border-ct-border text-ct-muted hover:border-ct-accent/50'
                    }`}
                  >
                    <span className="font-medium text-ct-text">{p.title}</span>
                    <span className="ml-2 text-xs">{p.topic}</span>
                    <span className={`ml-2 rounded px-1.5 py-0.5 text-xs ${
                      p.difficulty === 'easy' ? 'bg-green-900/50 text-green-400'
                      : p.difficulty === 'medium' ? 'bg-amber-900/50 text-amber-400'
                      : 'bg-red-900/50 text-red-400'
                    }`}>{p.difficulty}</span>
                  </button>
                ))}
              </div>
            )}
            <button
              onClick={() => selectedPid && onStartExisting?.(selectedPid)}
              disabled={!selectedPid}
              className="mt-4 w-full rounded-lg bg-ct-accent py-3 text-base font-semibold text-white transition hover:opacity-90 disabled:opacity-40"
            >
              开始练习
            </button>
          </section>
        )}

        {/* ── LeetCode 链接 ── */}
        {tab === 'leetcode' && (
          <section>
            <h2 className="mb-3 text-sm font-semibold text-ct-text">粘贴 LeetCode 题目链接</h2>
            <input
              type="text"
              value={leetcodeUrl}
              onChange={e => setLeetcodeUrl(e.target.value)}
              placeholder="https://leetcode.com/problems/two-sum/"
              className="w-full rounded-lg border border-ct-border bg-slate-800/50 px-4 py-3 text-sm text-ct-text placeholder-ct-muted outline-none focus:border-ct-accent"
            />
            <p className="mt-2 text-xs text-ct-muted">支持 leetcode.com 和 leetcode.cn 的题目链接</p>
            <button
              onClick={() => leetcodeUrl.trim() && onStartLeetcode?.(leetcodeUrl.trim())}
              disabled={!leetcodeUrl.trim()}
              className="mt-4 w-full rounded-lg bg-ct-accent py-3 text-base font-semibold text-white transition hover:opacity-90 disabled:opacity-40"
            >
              解析并开始
            </button>
          </section>
        )}

        {/* ── Agent 导师模式 ── */}
        {tab === 'agent' && (
          <section className="text-center">
            <div className="mb-4 rounded-lg border border-ct-border bg-slate-800/30 p-6">
              <p className="text-lg font-medium text-ct-text">🧑‍🏫 Agent 导师模式</p>
              <p className="mt-2 text-sm text-ct-muted">
                与 AI 导师直接对话，告诉 TA 你想练什么类型、难度、具体方向的题目。
                AI 会通过多轮对话了解你的需求，然后为你量身生成一道题。
              </p>
              <ul className="mt-3 space-y-1 text-left text-xs text-ct-muted">
                <li>💬 自然对话，告诉 AI 你想练什么</li>
                <li>🎯 AI 会追问细节，确保题目贴合你的需求</li>
                <li>🧠 提交后 AI 判题，给出温暖反馈和修复建议</li>
                <li>🔄 未通过可以多次修改，AI 持续辅导直到 AC</li>
              </ul>
            </div>
            <button
              onClick={() => onStart('', '', 'agent')}
              className="w-full rounded-lg bg-ct-accent py-3 text-base font-semibold text-white transition hover:opacity-90"
            >
              开始对话
            </button>
          </section>
        )}
      </div>
    </div>
  );
}