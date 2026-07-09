import { useEffect, useRef } from 'react';
import type { Message } from '../../types/session';

/**
 * AgentChat — 左面板的 Agent 对话组件。
 *
 * 显示 AI 导师和用户的对话记录，底部有输入框。
 * 用于 Agent 导师模式的出题前对话阶段（status === "dialog"）。
 *
 * Props:
 *   messages: 对话消息列表（来自 tutor_messages）
 *   onSend: 用户发送消息的回调，接收输入文本
 *   disabled: 是否禁用输入（出题中/已完成对话）
 */
export default function AgentChat({
  messages,
  onSend,
  disabled = false,
  inputPlaceholder = '回复 AI 导师...',
}: {
  messages: Message[];
  onSend: (text: string) => void;
  disabled?: boolean;
  inputPlaceholder?: string;
}) {
  const chatEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // 自动滚动到最新消息
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && !disabled && inputRef.current?.value.trim()) {
      onSend(inputRef.current.value.trim());
      inputRef.current.value = '';
    }
  };

  return (
    <div className="flex h-full flex-col">
      {/* 消息列表 */}
      <div className="flex-1 overflow-y-auto space-y-3 p-4">
        {messages.length === 0 && (
          <p className="text-center text-sm text-ct-muted">对话尚未开始...</p>
        )}
        {messages.map((msg, i) => (
          <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
            <div
              className={`max-w-[85%] rounded-lg px-3 py-2 text-sm leading-relaxed ${
                msg.role === 'user'
                  ? 'bg-ct-accent/20 text-ct-text'
                  : 'bg-slate-800/60 text-ct-text'
              }`}
            >
              <div className="text-xs text-ct-muted mb-1">
                {msg.role === 'user' ? '你' : 'AI 导师'}
              </div>
              <div className="whitespace-pre-wrap">{msg.content}</div>
            </div>
          </div>
        ))}
        <div ref={chatEndRef} />
      </div>

      {/* 输入框 */}
      <div className="border-t border-ct-border p-3">
        <input
          ref={inputRef}
          type="text"
          placeholder={disabled ? '对话已完成...' : inputPlaceholder}
          disabled={disabled}
          onKeyDown={handleKeyDown}
          className="w-full rounded-lg border border-ct-border bg-slate-800/50 px-3 py-2 text-sm text-ct-text placeholder-ct-muted outline-none focus:border-ct-accent disabled:opacity-40"
        />
      </div>
    </div>
  );
}