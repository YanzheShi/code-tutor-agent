import type { Message } from '../../types/session';
import Markdown from '../Markdown';

export default function MsgItem({ msg }: { msg: Message }) {
  const isTutor = msg.role === 'tutor';
  const kind = (msg.metadata?.kind as string | undefined) ?? '';

  // 轨迹分析过渡摘要卡（双落点：主聊天可见卡；summary 来自 TraceSummary）
  if (kind === 'trace-summary') {
    const summary = (msg.metadata?.summary ?? {}) as {
      summary_text?: string;
      bullets?: string[];
    };
    return (
      <div className="flex justify-start">
        <div className="max-w-[92%] space-y-2 rounded-lg border border-ct-accent/40 bg-ct-accent/5 p-3 text-sm">
          <div className="flex items-center gap-1 font-medium text-ct-accent">📌 上一题轨迹分析摘要</div>
          {summary.summary_text && (
            <p className="whitespace-pre-wrap leading-relaxed text-ct-text">{summary.summary_text}</p>
          )}
          {Array.isArray(summary.bullets) && summary.bullets.length > 0 && (
            <ul className="list-disc space-y-1 pl-5 text-ct-text">
              {summary.bullets.map((b, i) => <li key={i}>{b}</li>)}
            </ul>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={`flex ${isTutor ? 'justify-start' : 'justify-end'}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm leading-relaxed ${
          isTutor
            ? 'bg-ct-panel text-ct-text'
            : 'bg-ct-accent/20 text-ct-text'
        }`}
      >
        <Markdown content={msg.content} />
      </div>
    </div>
  );
}
