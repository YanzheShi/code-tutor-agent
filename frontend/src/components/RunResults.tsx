import type { RunResult } from '../types/session';

/** 编译错误指针块：源码行 + ^ 指针 + 消息，<pre> 保等宽与换行（LeetCode 风格）。 */
function CompileErrorPanel({ r }: { r: RunResult }) {
  const ce = r.compile_error!;
  return (
    <div className="mb-1 rounded bg-ct-surface-secondary p-2 overflow-x-auto">
      <div className="text-ct-error font-medium mb-1">
        Compile Error · Line {ce.line}{ce.column ? `:${ce.column}` : ''} · {ce.message}
      </div>
      {ce.text && (
        <pre className="whitespace-pre text-[11px] leading-5 text-ct-text m-0">
          {ce.text + '\n' + ce.pointer}
        </pre>
      )}
    </div>
  );
}

export default function RunResults({ results, running }: { results: RunResult[] | null; running: boolean }) {
  if (!results && !running) {
    return <div className="flex-1 overflow-y-auto p-4"><p className="text-sm text-ct-muted">点击「运行」查看结果</p></div>;
  }
  if (running) {
    return <div className="flex-1 overflow-y-auto p-4"><p className="text-sm text-ct-accent animate-pulse">运行中...</p></div>;
  }
  const passCount = results ? results.filter(r => r.passed).length : 0;
  return (
    <div className="flex-1 overflow-y-auto p-4 space-y-3">
      <h3 className={'text-sm font-bold ' + (passCount === results!.length ? 'text-ct-success' : 'text-ct-warn')}>
        运行结果: {passCount}/{results!.length} 通过
        <span className="ml-2 text-[10px] text-ct-muted font-normal">Judge0</span>
      </h3>
      {results!.map(r => (
        <div key={r.test_case_id} className={'rounded border p-3 text-xs ' + (r.passed ? 'border-ct-success/20 bg-ct-success-bg' : 'border-ct-error/20 bg-ct-error-bg')}>
          <div className="flex items-center gap-2 mb-1">
            <span className={'font-bold ' + (r.passed ? 'text-ct-success' : 'text-ct-error')}>{r.passed ? '\u2713' : '\u2717'}</span>
            <span className="text-ct-text font-medium">用例 #{r.test_case_id}</span>
            <span className="text-ct-muted">({r.status})</span>
            {r.runtime_ms > 0 && <span className="text-ct-muted text-[10px]">{r.runtime_ms.toFixed(1)}ms</span>}
            {r.memory_kb > 0 && <span className="text-ct-muted text-[10px] ml-1">| {r.memory_kb.toFixed(0)}KB</span>}
          </div>
          {r.input_args && r.input_args.length > 0 && (
            <div className="text-ct-muted mb-1">输入: {r.input_args.join(', ')}</div>
          )}
          {r.explanation && <div className="text-ct-muted mb-1">{r.explanation}</div>}
          {/* 编译错误：结构化 payload 优先（<pre> 指针）；其余 detail（RE 栈 / TLE 等）
              仅在「期望/实际」两行都缺时兜底展示——WA 的 expected=got= 文本与下行重复 */}
          {r.compile_error ? (
            <CompileErrorPanel r={r} />
          ) : (
            r.detail && !r.expected && !r.actual && (
              <div className="text-ct-muted mb-1 whitespace-pre-wrap">{r.detail}</div>
            )
          )}
          {/* 实际/期望红绿对比：提交判题映射的失败用例（带 actual）固定展示 */}
          {r.actual !== undefined && (
            <div className="text-ct-muted">实际: <span className="text-ct-error">{r.actual || '—'}</span></div>
          )}
          <div className="text-ct-muted">期望: <span className="text-ct-success">{r.expected || '—'}</span></div>
        </div>
      ))}
    </div>
  );
}