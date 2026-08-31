import React, { memo, useEffect, useId, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import mermaid from 'mermaid';
import 'katex/dist/katex.min.css';
import { useTheme } from '../hooks/useTheme';

/**
 * 共享的 Markdown 渲染组件。
 *
 * 用于 tutor / AI 导师的消息气泡，统一处理加粗、列表、行内与块级代码、链接，
 * 以及 $...$ / $$...$$ LaTeX 数学公式（KaTeX 渲染）。
 * 额外支持 ```mermaid 代码块渲染为矢量图（跟随主题切换，语法错误时降级显示原文）。
 */

// mermaid 全局只初始化一次；theme 在组件内按需覆盖。
mermaid.initialize({
  startOnLoad: false,
  securityLevel: 'loose',
  fontFamily: 'inherit',
});

/** 把 mermaid 主题名映射到 mermaid.initialize 的 theme 值。 */
function mermaidThemeFor(theme: 'dark' | 'light'): 'dark' | 'default' {
  return theme === 'dark' ? 'dark' : 'default';
}

/**
 * 渲染单个 mermaid 代码块。
 * - 异步调用 mermaid.render 得到 SVG 后注入容器；
 * - 语法错误 / 渲染失败时降级为显示原始文本，不拖垮整条消息。
 */
const MermaidBlock = memo(function MermaidBlock({ code, theme }: { code: string; theme: 'dark' | 'light' }) {
  const reactId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const renderId = `mermaid-${reactId.replace(/[^a-zA-Z0-9]/g, '')}`;

    (async () => {
      try {
        // 每次渲染前按当前主题重设 theme，保证切换主题后图同步更新。
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'loose',
          fontFamily: 'inherit',
          theme: mermaidThemeFor(theme),
        });
        // 先用 parse 做语法校验：只验证、不往 DOM 塞内容。
        // 语法错直接抛异常走 catch 降级，避免触发 render 把错误节点残留到 body。
        await mermaid.parse(code);
        const { svg } = await mermaid.render(renderId, code);
        if (cancelled) return;
        if (containerRef.current) {
          containerRef.current.innerHTML = svg;
          setError(null);
        }
      } catch (e) {
        if (cancelled) return;
        // 安全网：清除 mermaid 可能 append 到 body 的临时错误节点（id 即 renderId），
        // 防止「Syntax error in text …」脱离消息气泡显示在页面底部。
        document.getElementById(renderId)?.remove();
        setError(e instanceof Error ? e.message : String(e));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [code, theme, reactId]);

  if (error) {
    return (
      <pre className="overflow-x-auto rounded bg-ct-surface-secondary p-3 text-xs font-mono text-ct-text">
        <code>{code}</code>
        <div className="mt-1 text-[11px] text-ct-error">mermaid 渲染失败：{error}</div>
      </pre>
    );
  }

  return <div ref={containerRef} className="my-2 flex justify-center" />;
});

/**
 * 容错预处理：把 ` ```python Solution: ` 这类「语言标识后紧跟说明文字」的畸形
 * 围栏规范化为标准写法 ` ```python ` + 换行，避免 LLM 偶发格式错误时整个代码块
 * 无法渲染。只处理同一行内紧跟的非换行文字（正常的 ` ```python\n代码 ` 不受影响）。
 */
function normalizeFences(src: string): string {
  return src.replace(/```([a-zA-Z0-9_+\-]+)[ \t]+([^\n`]+)/g, '```$1\n$2');
}

function Markdown({ content }: { content: string }) {
  const { theme } = useTheme();
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
          // mermaid 代码块：拦截并渲染为矢量图，不走普通 <pre>。
          if (className === 'language-mermaid') {
            const codeText = String(children).replace(/\n$/, '');
            return <MermaidBlock code={codeText} theme={theme} />;
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

// memo：仅当 content 变化时才重渲染。避免父组件（如聊天输入 state 更新）
// 引起的无关重渲染波及历史消息，导致含 mermaid 大图时整页抖动。
export default memo(Markdown);
