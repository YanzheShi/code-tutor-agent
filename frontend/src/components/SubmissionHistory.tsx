import { useEffect, useState } from 'react';
import type { Submission } from '../types/session';

import { API_BASE } from '../api/config';

const BASE = API_BASE;

export default function SubmissionHistory({ problemId }: { problemId: number }) {
  const [submissions, setSubmissions] = useState<Submission[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(BASE + '/problem/' + problemId + '/submissions')
      .then(r => r.ok ? r.json() : { submissions: [] })
      .then(d => { if (!cancelled) { setSubmissions(d.submissions || []); setLoading(false); } })
      .catch(() => { if (!cancelled) { setSubmissions([]); setLoading(false); } });
    return () => { cancelled = true; };
  }, [problemId]);

  if (loading) {
    return <div className="flex-1 overflow-y-auto p-4"><p className="text-sm text-ct-muted">加载中...</p></div>;
  }

  if (submissions.length === 0) {
    return <div className="flex-1 overflow-y-auto p-4"><p className="text-sm text-ct-muted">暂无提交记录</p></div>;
  }

  // 倒序：新提交在前面
  const reversed = [...submissions].reverse();

  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-3">
      {reversed.map((s, i) => {
        const isExpanded = expandedIdx === i;
        const hasFullCode = s.code && s.code.length > 300;
        return (
          <div key={i} className="rounded border border-ct-border bg-ct-surface p-3 text-xs">
            {/* 头行：判题结论 + 时间 + 展开按钮 */}
            <div className="flex items-center gap-3 mb-2">
              <span className={'font-bold text-sm ' + (s.verdict === 'AC' ? 'text-ct-success' : s.verdict === 'WA' ? 'text-ct-warn' : 'text-ct-error')}>
                {s.verdict || (s.judge_results?.[0]?.status) || 'RE'}
              </span>
              <span className="text-ct-muted">{s.timestamp || new Date().toLocaleTimeString()}</span>
              <button
                className="ml-auto text-ct-muted hover:text-white transition-colors"
                onClick={() => setExpandedIdx(isExpanded ? null : i)}
                title={isExpanded ? '收起代码' : '展开代码'}
              >
                {isExpanded ? '▲' : '▼'}
              </button>
            </div>

            {/* 判题详情 */}
            <div className="flex gap-2 text-ct-muted mb-1">
              {s.judge_results?.map((jr, j) => (
                <span key={j} className={'text-xs ' + (jr.status === 'AC' ? 'text-ct-success' : jr.status === 'WA' ? 'text-ct-warn' : 'text-ct-error')}>
                  {jr.phase}: {jr.status} ({jr.runtime_ms?.toFixed(0)}ms)
                </span>
              ))}
            </div>

            {/* WA 期望 vs 实际对比 */}
            {s.verdict !== 'AC' && s.judge_results?.some(j => j.phase === 'base' && j.status !== 'AC') && (() => {
              const fail = s.judge_results?.find(j => j.phase === 'base' && j.status !== 'AC');
              const hasInput = (fail?.input_args?.length ?? 0) > 0;
              const hasExpected = !!fail?.expected_output;
              const hasActual = !!fail?.actual_output;
              if (!hasInput && !hasExpected && !hasActual) return null;
              return (
                <div className="rounded border border-ct-warn/30 bg-ct-warn-bg p-2 mb-2 text-xs space-y-1">
                  <span className="font-semibold text-ct-warn">首个失败用例对比</span>
                  {hasInput && <div><span className="text-ct-muted">输入: </span><code className="text-ct-text">{JSON.stringify(fail!.input_args)}</code></div>}
                  {hasExpected && <div><span className="text-ct-muted">期望: </span><code className="text-ct-success">{fail!.expected_output}</code></div>}
                  {hasActual && <div><span className="text-ct-muted">实际: </span><code className="text-ct-error">{fail!.actual_output}</code></div>}
                </div>
              );
            })()}

            {/* 代码区：展开显示全文，未展开截断 */}
            <pre
              className={'rounded bg-ct-surface-secondary p-2 text-ct-muted text-xs overflow-x-auto ' + (isExpanded ? 'max-h-none' : 'max-h-24 overflow-y-hidden')}
              onClick={() => setExpandedIdx(isExpanded ? null : i)}
              style={{ cursor: hasFullCode || !isExpanded ? 'pointer' : 'default' }}
            >
              {isExpanded ? s.code : (s.code?.slice(0, 300) || '')}
              {!isExpanded && hasFullCode ? '...' : ''}
            </pre>
          </div>
        );
      })}
    </div>
  );
}