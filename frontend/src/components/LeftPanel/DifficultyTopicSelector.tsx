import { useEffect, useState } from 'react';
import type { DiffValue } from './AgentChat';
import { API_BASE } from '../../api/config';

const DIFF_GRADIENT: Record<DiffValue, string> = {
  easy: 'linear-gradient(135deg, #34D399 0%, #10B981 100%)',
  medium: 'linear-gradient(135deg, #FBBF24 0%, #F59E0B 100%)',
  hard: 'linear-gradient(135deg, #FB7185 0%, #EF4444 100%)',
  random: 'linear-gradient(135deg, #8B5CF6 0%, #7C3AED 100%)',
};

const RANDOM_GRADIENT = 'linear-gradient(135deg, #8B5CF6 0%, #7C3AED 100%)';

// 常用主题的专属渐变；后端新下发的主题没有预设渐变时走 gradientFor 哈希兜底
const TOPIC_GRADIENT: Record<string, string> = {
  数组: 'linear-gradient(135deg, #60A5FA 0%, #3B82F6 100%)',
  字符串: 'linear-gradient(135deg, #22D3EE 0%, #06B6D4 100%)',
  哈希表: 'linear-gradient(135deg, #2DD4BF 0%, #14B8A6 100%)',
  双指针: 'linear-gradient(135deg, #F472B6 0%, #EC4899 100%)',
  动态规划: 'linear-gradient(135deg, #A78BFA 0%, #8B5CF6 100%)',
  二叉树: 'linear-gradient(135deg, #34D399 0%, #059669 100%)',
  random: RANDOM_GRADIENT,
};

const DIFFS: { value: DiffValue; label: string }[] = [
  { value: 'easy', label: '简单' },
  { value: 'medium', label: '中等' },
  { value: 'hard', label: '困难' },
  { value: 'random', label: '随机' },
];

// 后端接口不可用时的兜底静态列表（与后端 topics.py TOPIC_CATALOG 保持一致）
const FALLBACK_TOPICS: { value: string; label: string }[] = [
  { value: '数组', label: '数组' },
  { value: '字符串', label: '字符串' },
  { value: '哈希表', label: '哈希表' },
  { value: '双指针', label: '双指针' },
  { value: '动态规划', label: '动态规划' },
  { value: '二叉树', label: '二叉树' },
];

// 未知主题的渐变兜底：按名称哈希到 HSL 色相，保证每个主题颜色稳定且可区分
function gradientFor(topic: string): string {
  const preset = TOPIC_GRADIENT[topic];
  if (preset) return preset;
  let h = 0;
  for (let i = 0; i < topic.length; i++) h = (h * 31 + topic.charCodeAt(i)) % 360;
  return `linear-gradient(135deg, hsl(${h} 70% 55%) 0%, hsl(${(h + 40) % 360} 65% 45%) 100%)`;
}

export function diffLabel(v: DiffValue): string {
  return DIFFS.find((d) => d.value === v)?.label ?? v;
}

export default function DifficultyTopicSelector({
  difficulty,
  topic,
  onPickDifficulty,
  onPickTopic,
  onStart,
  onParseLeetcode,
  disabled = false,
}: {
  difficulty: DiffValue | null;
  topic: string | null; // 'random' 或具体主题名
  onPickDifficulty: (v: DiffValue) => void;
  onPickTopic: (v: string) => void;
  onStart: () => void;
  onParseLeetcode?: (url: string) => void;
  disabled?: boolean;
}) {
  const bothSelected = !!difficulty && !!topic;
  const [url, setUrl] = useState('');
  const [urlError, setUrlError] = useState('');

  // 主题目录：默认先用兜底静态列表渲染（避免接口慢导致按钮区空白），
  // 接口成功后替换为后端主题（含新增主题），失败则静默保持兜底。
  const [topics, setTopics] = useState<{ value: string; label: string }[]>(FALLBACK_TOPICS);
  useEffect(() => {
    let cancelled = false;
    fetch(API_BASE + '/topics')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('topics http ' + r.status))))
      .then((data) => {
        if (cancelled) return;
        const list = Array.isArray(data?.topics) ? data.topics : [];
        if (list.length > 0) setTopics(list);
      })
      .catch(() => {
        /* 接口失败保持兜底列表 */
      });
    return () => { cancelled = true; };
  }, []);

  // 轻量格式校验：必须是 leetcode.com / leetcode.cn 的 /problems/xxx 链接
  const LC_RE = /https?:\/\/(?:www\.)?(?:leetcode\.(?:com|cn))\/problems\/[\w-]+/i;
  const handleParse = () => {
    let u = url.trim();
    // 兼容不带协议粘贴（如 placeholder 示例 leetcode.cn/problems/two-sum）
    if (u && !/^https?:\/\//i.test(u)) u = 'https://' + u;
    if (!LC_RE.test(u)) {
      setUrlError('请输入有效的 LeetCode 题目链接（leetcode.cn / leetcode.com 的 /problems/xxx）');
      return;
    }
    setUrlError('');
    onParseLeetcode?.(u);
  };

  // 选中态：满透明度 + 白环 + 阴影 + 放大 + ✓ 前缀；未选中降到 60% 明显变淡
  const diffStateCls = (active: boolean) =>
    active
      ? 'opacity-100 ring-2 ring-white shadow-xl scale-105 z-10'
      : 'opacity-60 hover:opacity-100';

  const topicStateCls = (active: boolean) =>
    active
      ? 'opacity-100 ring-2 ring-white shadow-lg scale-105 z-10'
      : 'opacity-60 hover:opacity-100';

  // 随机主题由前端追加在最后（UI 概念，后端目录不含）
  const allTopics = [...topics, { value: 'random', label: '随机主题' }];

  return (
    <div className="space-y-3 border-b border-ct-border bg-ct-surface px-3 py-3">
      {/* LeetCode URL 解析入口：置于难度/主题选择之上 */}
      <div>
        <div className="mb-1.5 flex items-center gap-1.5">
          <span className="inline-flex h-4 w-4 items-center justify-center rounded bg-amber-500 text-[10px] font-bold text-white">LC</span>
          <span className="text-xs font-semibold text-ct-muted">解析 LeetCode 题目</span>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={url}
            disabled={disabled}
            onChange={(e) => { setUrl(e.target.value); if (urlError) setUrlError(''); }}
            placeholder="粘贴 LeetCode 题目链接，如 leetcode.cn/problems/two-sum"
            className="flex-1 rounded-lg border border-ct-border bg-ct-input px-3 py-2 text-sm text-ct-text placeholder-ct-muted outline-none focus:border-ct-accent disabled:opacity-40"
          />
          <button
            type="button"
            onClick={handleParse}
            disabled={disabled || !url.trim()}
            style={{ backgroundImage: RANDOM_GRADIENT }}
            className="shrink-0 rounded-lg px-4 py-2 text-sm font-semibold text-white shadow-md transition hover:opacity-90 disabled:opacity-40"
          >
            解析题目
          </button>
        </div>
        {urlError && <p className="mt-1 text-xs text-red-500">{urlError}</p>}
      </div>

      <div>
        <div className="mb-1.5 text-xs font-semibold text-ct-muted">难度</div>
        <div className="flex gap-2">
          {DIFFS.map((d) => {
            const active = difficulty === d.value;
            return (
              <button
                key={d.value}
                type="button"
                disabled={disabled}
                onClick={() => onPickDifficulty(d.value)}
                style={{ backgroundImage: DIFF_GRADIENT[d.value] }}
                className={`flex-1 rounded-lg py-2 text-sm font-semibold text-white transition ${diffStateCls(active)}`}
              >
                {active ? `✓ ${d.label}` : d.label}
              </button>
            );
          })}
        </div>
      </div>

      <div>
        <div className="mb-1.5 text-xs font-semibold text-ct-muted">主题</div>
        <div className="flex flex-wrap gap-2">
          {allTopics.map((t) => {
            const active = topic === t.value;
            return (
              <button
                key={t.value}
                type="button"
                disabled={disabled}
                onClick={() => onPickTopic(t.value)}
                style={{ backgroundImage: gradientFor(t.value) }}
                className={`rounded-full px-3 py-1.5 text-sm font-medium text-white transition ${topicStateCls(active)}`}
              >
                {active ? `✓ ${t.label}` : t.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onStart}
          disabled={!bothSelected || disabled}
          style={{ backgroundImage: RANDOM_GRADIENT }}
          className="ml-auto rounded-lg px-4 py-1.5 text-sm font-semibold text-white shadow-md transition hover:opacity-90 disabled:opacity-40"
        >
          开始出题
        </button>
      </div>
      {!bothSelected && !disabled && (
        <p className="text-xs text-amber-500">请同时选择「难度」和「主题」，再点击「开始出题」。</p>
      )}
    </div>
  );
}