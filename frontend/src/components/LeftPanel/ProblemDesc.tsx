import type { ProblemMeta } from '../../types/session';

export default function ProblemDesc({ problem }: { problem: ProblemMeta }) {
  // Use HTML if available, otherwise wrap plain text in <p> tags
  const hasHtml = !!problem.description_html;
  const descToRender = hasHtml
    ? problem.description_html
    : '<p>' + (problem.description || '').replace(/\n\n/g, '</p><p>').replace(/\n/g, '<br>') + '</p>';

  return (
    <div className="space-y-4">
      {/* 标题 + 难度 */}
      <div className="flex items-center gap-3">
        <h2 className="text-lg font-bold text-ct-text">{problem.title}</h2>
        <span className={`rounded px-2 py-0.5 text-xs font-medium ${
          problem.difficulty === 'easy' ? 'bg-green-800/30 text-green-400' :
          problem.difficulty === 'medium' ? 'bg-yellow-800/30 text-yellow-400' :
          'bg-red-800/30 text-red-400'
        }`}>
          {problem.difficulty}
        </span>
      </div>

      {/* 描述 — 富文本 HTML */}
      <div
        className="prose prose-invert max-w-none text-sm leading-relaxed text-ct-text [&_pre]:rounded [&_pre]:bg-slate-800 [&_pre]:p-3 [&_pre]:text-xs [&_code]:rounded [&_code]:bg-slate-700/50 [&_code]:px-1 [&_code]:py-0.5 [&_img]:max-w-full [&_sup]:text-[0.65em] [&_sup]:align-top [&_strong]:font-semibold"
        dangerouslySetInnerHTML={{ __html: descToRender }}
      />

      {/* 可见测试用例 */}
      {(problem.visible_test_cases ?? []).length > 0 && (
        <div className="space-y-2">
          <h3 className="text-sm font-semibold text-ct-text">示例</h3>
          {problem.visible_test_cases!.map((tc, i) => (
            <div key={i} className="rounded border border-ct-border bg-slate-800/20 p-3 text-xs">
              <div className="mb-1 font-medium text-ct-text">示例 {i + 1}</div>
              {tc.explanation && (
                <div className="mb-1 text-ct-muted">{tc.explanation}</div>
              )}
              <div className="space-y-1 font-mono">
                <div>
                  <span className="text-ct-muted">输入：</span>
                  <span className="text-ct-text">
                    {(tc.input_args ?? []).map((a, j) => (
                      <code key={j} className="mr-1 rounded bg-slate-700/50 px-1 py-0.5">{a}</code>
                    ))}
                  </span>
                </div>
                <div>
                  <span className="text-ct-muted">输出：</span>
                  <code className="rounded bg-slate-700/50 px-1 py-0.5 text-ct-text">{tc.expected_output}</code>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}