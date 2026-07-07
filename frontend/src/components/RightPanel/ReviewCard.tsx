import type { JudgeReport } from '../../types/judge';

export default function ReviewCard({ report }: { report: JudgeReport }) {
  const styleColor =
    report.style_rating === 'good' ? 'text-ct-success'
    : report.style_rating === 'fair' ? 'text-ct-warn'
    : 'text-ct-error';

  return (
    <div className="rounded-lg border border-ct-border bg-ct-panel/50 p-4">
      <h3 className="mb-3 text-sm font-semibold text-ct-text">📊 评审卡片</h3>

      <div className="space-y-2 text-sm">
        {/* 复杂度 */}
        <div className="flex gap-4">
          <div>
            <span className="text-ct-muted text-xs">时间</span>
            <p className="font-mono text-ct-text">{report.time_complexity ?? '-'}</p>
          </div>
          <div>
            <span className="text-ct-muted text-xs">空间</span>
            <p className="font-mono text-ct-text">{report.space_complexity ?? '-'}</p>
          </div>
        </div>

        {/* 风格 */}
        <div>
          <span className="text-ct-muted text-xs">风格评级</span>
          <p className={`font-medium ${styleColor}`}>
            {report.style_rating ?? '-'}
          </p>
        </div>

        {report.style_notes && report.style_notes.length > 0 && (
          <ul className="list-disc space-y-0.5 pl-4 text-xs text-ct-muted">
            {report.style_notes.map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        )}

        {report.summary && (
          <p className="border-t border-ct-border pt-2 text-xs italic text-ct-muted">
            {report.summary}
          </p>
        )}
      </div>
    </div>
  );
}