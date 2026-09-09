import { useCallback, useEffect, useRef, useState } from 'react';
import Editor, { type OnMount } from '@monaco-editor/react';
import { useTheme } from '../../hooks/useTheme';

/** 编辑器内容行数硬上限（与后端 MAX_CODE_LINES=300 / 10KB 同一条线，2026-09-09）。 */
export const MAX_EDITOR_LINES = 300;

/** 编译错误标记事件名：useSession 在 run/submit 返回后派发，detail=null 表示清除。 */
export const CE_MARKER_EVENT = 'ct:compile-error';
export type CeMarkerDetail = { line: number; column: number; message: string } | null;

/** Monaco marker owner（重复 set 同 owner 即覆盖，避免堆积）。 */
export const CE_MARKER_OWNER = 'cta-compile-error';
/** Monaco MarkerSeverity.Error 的数值（8）——避免为常量引入 monaco 运行时依赖。 */
const MONACO_SEVERITY_ERROR = 8;

/** 由 CE 详情构造 Monaco marker 数组（纯函数，detail=null/无效 → 空数组=清除）。 */
export function buildCeMarkers(detail: CeMarkerDetail, lineCount: number) {
  if (!detail || !detail.line || detail.line < 1) return [];
  const line = Math.min(detail.line, Math.max(1, lineCount));
  const col = Math.max(1, detail.column || 1);
  return [
    {
      startLineNumber: line,
      startColumn: col,
      endLineNumber: line,
      endColumn: col + 1,
      message: detail.message || '编译错误',
      severity: MONACO_SEVERITY_ERROR,
    },
  ];
}

/**
 * 最近一次 CE 标记（模块级持久化）。
 * 编辑器随右面板 tab 切换会被卸载（MainLayout.renderPanelContent 条件渲染），
 * CE 事件可能落在未挂载窗口期——重挂载时必须立即恢复，否则「切回代码页红标消失」。
 */
let lastCeDetail: CeMarkerDetail = null;

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
  const monacoRef = useRef<Parameters<OnMount>[1] | null>(null);
  const [overLimitNotice, setOverLimitNotice] = useState(false);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** 设置 / 清除编译错误标记并滚动到出错行（编辑器未挂载时静默跳过，状态存模块级）。 */
  const applyCompileError = useCallback((detail: CeMarkerDetail) => {
    lastCeDetail = detail;
    const editor = editorRef.current;
    const monaco = monacoRef.current;
    const model = editor?.getModel();
    if (!editor || !monaco || !model) return;
    monaco.editor.setModelMarkers(model, CE_MARKER_OWNER, buildCeMarkers(detail, model.getLineCount()));
    if (detail && detail.line >= 1) {
      editor.revealLineInCenter(Math.min(detail.line, model.getLineCount()));
    }
  }, []);

  const handleMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
    (window as unknown as Record<string, unknown>).__ct_editor = editor;
    // e2e / 调试用：暴露 monaco 命名空间（getModelMarkers 断言红标）
    (window as unknown as Record<string, unknown>).__ct_monaco = monaco;
    // 重挂载立即恢复未过期的 CE 标记（tab 切换卸载窗口期丢失的事件在这里补上）
    if (lastCeDetail) applyCompileError(lastCeDetail);
  };

  // CE 标记事件：run/submit 返回后由 useSession 派发（ct:compile-error）
  useEffect(() => {
    const onCe = (e: Event) => applyCompileError((e as CustomEvent<CeMarkerDetail>).detail ?? null);
    window.addEventListener(CE_MARKER_EVENT, onCe);
    return () => window.removeEventListener(CE_MARKER_EVENT, onCe);
  }, [applyCompileError]);

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
      applyCompileError(null); // 用户开始改代码，旧编译错误标记即清除
      return;
    }
    applyCompileError(null); // 任何编辑都清标记（旧 CE 已过时，留着是噪音）
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
