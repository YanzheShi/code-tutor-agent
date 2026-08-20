import { useEffect, useRef } from 'react';
import Editor, { type OnMount } from '@monaco-editor/react';
import { useTheme } from '../../hooks/useTheme';

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
  }, []);

  return (
    <Editor
      height="100%"
      defaultLanguage="python"
      theme={theme === 'light' ? 'vs' : 'vs-dark'}
      value={code}
      onChange={(v) => onChange(v ?? '')}
      onMount={handleMount}
      options={{
        minimap: { enabled: false },
        fontSize: 14,
        lineNumbers: 'on',
        scrollBeyondLastLine: false,
        automaticLayout: true,
        padding: { top: 8 },
      }}
    />
  );
}