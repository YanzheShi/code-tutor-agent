import { useEffect, useRef, useState } from 'react';
import Editor, { type OnMount } from '@monaco-editor/react';
import { useTheme } from '../../hooks/useTheme';

/** 编辑器内容行数硬上限（与后端 MAX_CODE_LINES=300 / 10KB 同一条线，2026-09-09）。 */
export const MAX_EDITOR_LINES = 300;

export default function CodeEditor({
  code,
  onChange,
  starterCode,
}: {
  code: string;
  onChange: (v: string) => void;
  starterCode?: string;
}) {
  const { theme } = useTheme();
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);
  const [overLimitNotice, setOverLimitNotice] = useState(false);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleMount: OnMount = (editor) => {
    editorRef.current = editor;
    (window as unknown as Record<string, unknown>).__ct_editor = editor;
  };

  // 卸载时清掉全局编辑器引用，避免编辑轨迹采集拿到已 dispose 的 Monaco 实例
  // P2-3: 卸载前先派发 ct:editor-unmount 事件，让 useEditTrace 在编辑器仍可读时
  // 兜底 capturePending + flushIdleIfPaused，零星改动不丢失
  useEffect(() => () => {
    window.dispatchEvent(new CustomEvent('ct:editor-unmount'));
    if ((window as unknown as Record<string, unknown>).__ct_editor === editorRef.current) {
      (window as unknown as Record<string, unknown>).__ct_editor = undefined;
    }
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
  }, []);

  // 行数硬拦：超过 MAX_EDITOR_LINES 截断到上限并提示。
  // 注意 Monaco 的 maxLines 选项只控制「自动撑高」的 UI 高度、不是内容硬上限，
  // 真正的拦截必须在 onChange（后端入口还有 413 兜底，两层守同一条线）。
  const handleChange = (v: string | undefined) => {
    const val = v ?? '';
    if (val.split('\n').length > MAX_EDITOR_LINES) {
      onChange(val.split('\n').slice(0, MAX_EDITOR_LINES).join('\n'));
      setOverLimitNotice(true);
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
      noticeTimer.current = setTimeout(() => setOverLimitNotice(false), 3000);
      return;
    }
    onChange(val);
  };

  // 计数口径与硬拦一致（split('\n').length），代码 prop 是唯一数据源、随受控更新自然刷新
  const lineCount = code ? code.split('\n').length : 1;
  const nearLimit = lineCount >= MAX_EDITOR_LINES - 20;

  return (
    <div className="relative h-full">
      <Editor
        height="100%"
        defaultLanguage="python"
        theme={theme === 'light' ? 'vs' : 'vs-dark'}
        value={code}
        onChange={handleChange}
        onMount={handleMount}
        options={{
          minimap: { enabled: false },
          fontSize: 14,
          lineNumbers: 'on',
          scrollBeyondLastLine: false,
          automaticLayout: true,
          padding: { top: 8 },
          // 注：Monaco 的 maxLines 选项仅控自动撑高且当前版本未暴露该类型，
          // 300 行硬拦统一由上方 handleChange 承担（后端 413 兜底同一条线）。
        }}
      />
      <div
        className={`pointer-events-none absolute bottom-1.5 right-3 z-10 select-none rounded px-1.5 py-0.5 text-[11px] tabular-nums ${
          lineCount > MAX_EDITOR_LINES
            ? 'bg-ct-error text-white'
            : nearLimit
              ? 'bg-ct-warn-bg text-ct-warn'
              : 'text-ct-muted'
        }`}
        title={`单次提交上限：${MAX_EDITOR_LINES} 行 / 10KB`}
      >
        {lineCount}/{MAX_EDITOR_LINES}
        {overLimitNotice && <span className="ml-1 font-medium">已达上限，超出部分未纳入</span>}
      </div>
    </div>
  );
}
