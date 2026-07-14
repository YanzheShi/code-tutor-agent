import type { Message, FailedCase } from '../../types/session';
import MsgItem from './MsgItem';

export default function MessageList({
  messages,
  verdict,
  hintLevel,
  failedCase,
}: {
  messages: Message[];
  verdict: string | null;
  hintLevel: number;
  failedCase?: FailedCase | null;
}) {
  if (messages.length === 0) {
    return (
      <p className="text-ct-muted text-sm">提交代码后，导师会在这里给出建议和指导</p>
    );
  }

  return (
    <div className="space-y-3">
      {messages.map((msg, i) => {
        // 最新一条 tutor msg 旁加 Lx badge
        const isLatestTutor = msg.role === 'tutor' && i === messages.length - 1;
        return (
          <div key={i}>
            <MsgItem msg={msg} />
            {isLatestTutor && hintLevel > 0 && (
              <span className="ml-2 inline-block rounded bg-ct-info-bg px-1.5 py-0.5 text-xs text-ct-info">
                L{hintLevel}
              </span>
            )}
          </div>
        );
      })}

      {/* 判题短报 */}
      {verdict && verdict !== 'AC' && (
        <div className={`rounded border px-3 py-2 text-sm ${
          verdict === 'WA' ? 'border-ct-warn/30 bg-ct-warn-bg text-ct-warn'
          : 'border-ct-error/30 bg-ct-error-bg text-ct-error'
        }`}>
          {verdict === 'WA' && '答案不对，再检查一下逻辑'}
          {verdict === 'TLE' && '超时了，想想更高效的算法'}
          {verdict === 'RE' && '运行时出错，检查一下语法和边界'}
        </div>
      )}

      {/* Bug 2: 期望 vs 实际 对比面板（首个失败用例） */}
      {verdict && verdict !== 'AC' && failedCase && (
        <div className="rounded border border-ct-border bg-ct-surface-secondary p-3 space-y-2 text-xs">
          <div className="font-medium text-ct-muted">首个失败用例对比</div>
          <div>
            <div className="mb-1 text-ct-muted">输入</div>
            <pre className="overflow-x-auto rounded bg-ct-surface p-2 font-mono text-ct-text">
              {failedCase.input_args.length ? failedCase.input_args.map((a, i) => (
                <div key={i}>args[{i}]: {a}</div>
              )) : '（无）'}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-ct-success">期望输出</div>
            <pre className="overflow-x-auto rounded bg-ct-success-bg p-2 font-mono text-ct-success">
              {failedCase.expected_output || '（无）'}
            </pre>
          </div>
          <div>
            <div className="mb-1 text-ct-error">你的输出</div>
            <pre className="overflow-x-auto rounded bg-ct-error-bg p-2 font-mono text-ct-error">
              {failedCase.actual_output || '（无）'}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}