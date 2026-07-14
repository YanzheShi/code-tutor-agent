import ReactMarkdown from 'react-markdown';
import type { Message } from '../../types/session';

export default function MsgItem({ msg }: { msg: Message }) {
  const isTutor = msg.role === 'tutor';

  return (
    <div className={`flex ${isTutor ? 'justify-start' : 'justify-end'}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm leading-relaxed ${
          isTutor
            ? 'bg-ct-panel text-ct-text'
            : 'bg-ct-accent/20 text-ct-text'
        }`}
      >
        <ReactMarkdown
          components={{
            code: ({ className, children, ...props }) => {
              const isInline = !className;
              if (isInline) {
                return <code className="rounded bg-ct-hover px-1 py-0.5 text-xs font-mono" {...props}>{children}</code>;
              }
              return (
                <pre className="overflow-x-auto rounded bg-ct-surface-secondary p-3 text-xs font-mono">
                  <code className={className} {...props}>{children}</code>
                </pre>
              );
            },
            strong: ({ children }) => <strong className="font-semibold text-ct-text">{children}</strong>,
            p: ({ children }) => <p className="mb-1 last:mb-0">{children}</p>,
            ul: ({ children }) => <ul className="list-disc pl-4 space-y-0.5">{children}</ul>,
            ol: ({ children }) => <ol className="list-decimal pl-4 space-y-0.5">{children}</ol>,
            li: ({ children }) => <li>{children}</li>,
            a: ({ href, children }) => <a href={href} className="text-ct-accent underline" target="_blank" rel="noreferrer">{children}</a>,
          }}
        >
          {msg.content}
        </ReactMarkdown>
      </div>
    </div>
  );
}