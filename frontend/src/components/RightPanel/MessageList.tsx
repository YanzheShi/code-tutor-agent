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

      {/* LeetCode 风格失败面板（首个失败用例） */}
      {verdict && verdict !== 'AC' && failedCase && (
        <div className="rounded border border-ct-border bg-ct-surface-secondary p-3 space-y-2 text-xs">
          {verdict === 'RE' ? (
            <>
              <div className="font-medium text-ct-error">执行出错</div>
              {failedCase.detail && (
                <pre className="overflow-x-auto rounded bg-ct-surface p-2 font-mono text-ct-error whitespace-pre-wrap">
                  {failedCase.detail}
                </pre>
              )}
              {failedCase.input_args.length > 0 && (
                <div>
                  <div className="mb-1 text-ct-muted">最后执行的输入</div>
                  <pre className="overflow-x-auto rounded bg-ct-surface p-2 font-mono text-ct-text">
                    {failedCase.input_args.map((a, i) => <div key={i}>{a}</div>)}
                  </pre>
                </div>
              )}
            </>
          ) : (
            <>
              {failedCase.input_args.length > 0 && (
                <div>
                  <span className="text-ct-muted">输入: </span>
                  <code className="text-ct-text">{failedCase.input_args.join(', ')}</code>
                </div>
              )}
              {failedCase.actual_output !== '' && (
                <div>
                  <span className="text-ct-muted">输出: </span>
                  <code className="text-ct-error">{failedCase.actual_output}</code>
                </div>
              )}
              {failedCase.expected_output !== '' && (
                <div>
                  <span className="text-ct-muted">期望: </span>
                  <code className="text-ct-success">{failedCase.expected_output}</code>
                </div>
              )}
              {failedCase.explanation && (
                <div>
                  <span className="text-ct-muted">用例说明: </span>
                  <span className="text-ct-text">{failedCase.explanation}</span>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}