import type { Submission } from '../types/session';

export default function SubmissionHistory({ submissions }: { submissions: Submission[] }) {
  if (submissions.length === 0) {
    return <div className="flex-1 overflow-y-auto p-4"><p className="text-sm text-ct-muted">暂无提交记录</p></div>;
  }
  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-3">
      {[...submissions].reverse().map((s, i) => (
        <div key={i} className="rounded border border-ct-border bg-slate-800/20 p-3 text-xs">
          <div className="flex items-center gap-3 mb-2">
            <span className={'font-bold text-sm ' + (s.verdict === 'AC' ? 'text-ct-success' : s.verdict === 'WA' ? 'text-ct-warn' : 'text-ct-error')}>
              {s.verdict || (s.judge_results?.[0]?.status) || 'RE'}
            </span>
            <span className="text-ct-muted">{s.timestamp || new Date().toLocaleTimeString()}</span>
          </div>
          <div className="flex gap-2 text-ct-muted mb-1">
            {s.judge_results?.map((jr, j) => (
              <span key={j} className={'text-xs ' + (jr.status === 'AC' ? 'text-ct-success' : jr.status === 'WA' ? 'text-ct-warn' : 'text-ct-error')}>
                {jr.phase}: {jr.status} ({jr.runtime_ms?.toFixed(0)}ms)
              </span>
            ))}
          </div>
          <pre className="max-h-24 overflow-y-auto rounded bg-slate-900/50 p-2 text-ct-muted text-xs">
            {s.code?.slice(0, 300) || ''}{s.code?.length > 300 ? '...' : ''}
          </pre>
        </div>
      ))}
    </div>
  );
}