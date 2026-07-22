import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';

/**
 * 共享的 Markdown 渲染组件。
 *
 * 用于 tutor / AI 导师的消息气泡，统一处理加粗、列表、行内与块级代码、链接，
 * 以及 $...$ / $$...$$ LaTeX 数学公式（KaTeX 渲染）。
 */
/**
 * 容错预处理：把 ` ```python Solution: ` 这类「语言标识后紧跟说明文字」的畸形
 * 围栏规范化为标准写法 ` ```python ` + 换行，避免 LLM 偶发格式错误时整个代码块
 * 无法渲染。只处理同一行内紧跟的非换行文字（正常的 ` ```python\n代码 ` 不受影响）。
 */
function normalizeFences(src: string): string {
  return src.replace(/```([a-zA-Z0-9_+\-]+)[ \t]+([^\n`]+)/g, '```$1\n$2');
}

export default function Markdown({ content }: { content: string }) {
  const normalized = normalizeFences(content || '');
  return (
    <ReactMarkdown
      remarkPlugins={[remarkMath]}
      rehypePlugins={[rehypeKatex]}
      components={{
        code: ({ className, children, ...props }) => {
          const isInline = !className;
          if (isInline) {
            return (
              <code className="rounded bg-ct-hover px-1 py-0.5 text-xs font-mono" {...props}>
                {children}
              </code>
            );
          }
          return (
            <pre className="overflow-x-auto rounded bg-ct-surface-secondary p-3 text-xs font-mono">
              <code className={className} {...props}>
                {children}
              </code>
            </pre>
          );
        },
        strong: ({ children }) => <strong className="font-semibold text-ct-text">{children}</strong>,
        p: ({ children }) => <p className="mb-1 last:mb-0">{children}</p>,
        ul: ({ children }) => <ul className="list-disc pl-4 space-y-0.5">{children}</ul>,
        ol: ({ children }) => <ol className="list-decimal pl-4 space-y-0.5">{children}</ol>,
        li: ({ children }) => <li>{children}</li>,
        a: ({ href, children }) => (
          <a href={href} className="text-ct-accent underline" target="_blank" rel="noreferrer">
            {children}
          </a>
        ),
      }}
    >
      {normalized}
    </ReactMarkdown>
  );
}
