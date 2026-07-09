import { useRef } from 'react';
import Editor, { type OnMount } from '@monaco-editor/react';

export default function CodeEditor({
  code,
  onChange,
  starterCode,
}: {
  code: string;
  onChange: (v: string) => void;
  starterCode?: string;
}) {
  const editorRef = useRef<Parameters<OnMount>[0] | null>(null);

  const handleMount: OnMount = (editor) => {
    editorRef.current = editor;
    (window as unknown as Record<string, unknown>).__ct_editor = editor;
  };

  return (
    <Editor
      height="100%"
      defaultLanguage="python"
      theme="vs-dark"
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