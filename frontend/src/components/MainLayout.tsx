import { useRef } from 'react';
import ProblemDesc from './LeftPanel/ProblemDesc';
import AgentChat from './LeftPanel/AgentChat';
import CodeEditor from './LeftPanel/CodeEditor';
import MessageList from './RightPanel/MessageList';
import ReviewCard from './RightPanel/ReviewCard';
import SubmissionHistory from './SubmissionHistory';
import RunResults from './RunResults';
import { TabButton, VerdictBadge } from './TabButton';
import type { Message, ProblemMeta, RunResult, Submission } from '../types/session';
import type { JudgeReport } from '../types/judge';

type TabId = 'desc' | 'history' | 'reference' | 'code' | 'run' | 'tutor' | 'agent-history';

const TAB_LABELS: Record<TabId, string> = {
  desc: '题目描述', history: '提交记录', reference: '参考代码',
  code: '代码', run: '运行', tutor: '导师', 'agent-history': '导师对话',
};

export interface MainLayoutProps {
  problem: ProblemMeta | null;
  mode: string;
  activeTabs: { left: TabId; right: TabId };
  tabPanel: Record<TabId, 'left' | 'right'>;
  splitRatio: number;
  tutorMessages: Message[];
  chatInput: string;
  editorCode: string;
  hintLevel: number;
  latestVerdict: string | null;
  judgeReport: JudgeReport | null;
  submissions: Submission[];
  referenceCode: string;
  runResults: RunResult[] | null;
  progressMsgs: string[];
  running: boolean;
  submittingFlag: boolean;
  isDialogPhase: boolean;
  isDone: boolean;
  dragging: React.MutableRefObject<boolean>;
  dragTab: React.MutableRefObject<TabId | null>;
  editorInitialized?: React.MutableRefObject<boolean>;
  chatEndRef: React.RefObject<HTMLDivElement | null>;
  onSetChatInput: (v: string) => void;
  onSetActiveTabs: (v: { left: TabId; right: TabId } | ((prev: { left: TabId; right: TabId }) => { left: TabId; right: TabId })) => void;
  onSetTabPanel: (v: Record<TabId, 'left' | 'right'> | ((prev: Record<TabId, 'left' | 'right'>) => Record<TabId, 'left' | 'right'>)) => void;
  onSetSplitRatio: (v: number) => void;
  onSetEditorCode: (v: string) => void;
  onSetRunResults: (v: RunResult[] | null) => void;
  onSetTutorMessages: (v: Message[] | ((prev: Message[]) => Message[])) => void;
  onSetProgressMsgs: (v: string[]) => void;
  onRun: () => void;
  onSubmit: () => void;
  onChat: () => void;
  onNext: () => void;
  onAgentSend: (text: string) => void;
}

export default function MainLayout(props: MainLayoutProps) {
  const {
    problem, mode, activeTabs, tabPanel, splitRatio, tutorMessages, chatInput,
    editorCode, hintLevel, latestVerdict, judgeReport, submissions, referenceCode,
    runResults, progressMsgs, running, submittingFlag, isDialogPhase, isDone,
    dragging, dragTab, chatEndRef,
    onSetChatInput, onSetActiveTabs, onSetTabPanel, onSetSplitRatio, onSetEditorCode,
    onSetTutorMessages, onSetRunResults, onSetProgressMsgs,
    onRun, onSubmit, onChat, onNext, onAgentSend,
  } = props;

  const isLoading = running || submittingFlag;

  const panelTabs = {
    left: (Object.entries(tabPanel) as [TabId, 'left' | 'right'][])
      .filter(([tid, p]) => p === 'left' && (tid !== 'reference' || latestVerdict === 'AC'))
      .map(([tid]) => tid),
    right: (Object.entries(tabPanel) as [TabId, 'left' | 'right'][])
      .filter(([tid, p]) => p === 'right')
      .map(([tid]) => tid),
  };

  function handleSplitDrag(e: React.MouseEvent | MouseEvent) {
    if (!dragging.current) return;
    const container = document.getElementById('split-container');
    if (!container) return;
    const rect = container.getBoundingClientRect();
    onSetSplitRatio(Math.max(20, Math.min(80, ((e.clientX - rect.left) / rect.width) * 100)));
  }

  function handleTabDragStart(tabId: TabId) {
    return (e: React.DragEvent) => {
      dragTab.current = tabId;
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', tabId);
    };
  }

  function handleTabDragOver(e: React.DragEvent) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  }

  function handleTabDrop(targetPanel: 'left' | 'right') {
    return (e: React.DragEvent) => {
      e.preventDefault();
      const tabId = dragTab.current;
      if (!tabId) return;
      dragTab.current = null;
      const currentPanel = tabPanel[tabId];
      if (currentPanel === targetPanel) return;
      onSetTabPanel(prev => ({ ...prev, [tabId]: targetPanel }));
      onSetActiveTabs(prev => ({ ...prev, [targetPanel]: tabId }));
      const remainingInSource = (Object.entries(tabPanel) as [TabId, 'left' | 'right'][])
        .filter(([tid, p]) => p === currentPanel && tid !== tabId)
        .map(([tid]) => tid);
      if (remainingInSource.length > 0) {
        const stillActive = remainingInSource.includes(activeTabs[currentPanel]);
        if (!stillActive) {
          onSetActiveTabs(prev => ({ ...prev, [currentPanel]: remainingInSource[0] }));
        }
      }
    };
  }

  function renderPanelContent(panel: 'left' | 'right') {
    const tab = activeTabs[panel];
    switch (tab) {
    case 'desc':
      return <div className="flex-1 overflow-y-auto p-4">{problem && <ProblemDesc problem={problem} />}</div>;
    case 'history':
      return <SubmissionHistory submissions={submissions} />;
    case 'reference':
      return (
        <div className="flex-1 overflow-y-auto p-4">
          {referenceCode ? <pre className="rounded border border-ct-border bg-slate-900/50 p-4 text-sm font-mono text-ct-text overflow-x-auto">{referenceCode}</pre>
          : <p className="text-sm text-ct-muted">暂无参考代码</p>}
        </div>
      );
    case 'code':
      return (
        <div className="flex-1 flex flex-col">
          <div className="flex-1"><CodeEditor code={editorCode} onChange={onSetEditorCode} starterCode={problem?.starter_code} /></div>
        </div>
      );
    case 'run':
      return <RunResults results={runResults} running={running} />;
    case 'tutor':
      return (
        <div className="flex-1 flex flex-col">
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            <MessageList messages={tutorMessages} verdict={latestVerdict} hintLevel={hintLevel} />
            {latestVerdict === 'AC' && judgeReport && <div className="mt-3"><ReviewCard report={judgeReport} /></div>}
            <div ref={chatEndRef} />
          </div>
          <div className="border-t border-ct-border p-3 flex gap-2">
            <input type="text" value={chatInput} onChange={e => onSetChatInput(e.target.value)}
              placeholder="向导师提问..."
              className="flex-1 rounded border border-ct-border bg-slate-800/50 px-3 py-2 text-xs text-ct-text placeholder-ct-muted outline-none focus:border-ct-accent"
              onKeyDown={e => { if (e.key === 'Enter') onChat(); }} />
            <button onClick={onChat} disabled={!chatInput.trim()}
              className="rounded bg-ct-accent px-3 py-2 text-xs text-white disabled:opacity-40">发送</button>
          </div>
        </div>
      );
    case 'agent-history':
      return (
        <div className="flex-1 overflow-hidden">
          <AgentChat messages={tutorMessages} onSend={() => {}} disabled={true} />
        </div>
      );
    default:
      return null;
    }
  }

  return (
    <div className="flex h-screen flex-col">
      {/* 顶栏进度条 */}
      {progressMsgs.length > 0 && !isDialogPhase && (
        <div className="flex items-center gap-2 border-b border-ct-border bg-slate-800/60 px-4 py-1.5 text-xs text-ct-muted">
          <div className="h-3 w-3 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
          <span className="flex-1">{progressMsgs[progressMsgs.length - 1]}</span>
        </div>
      )}

      {/* 全屏 loading 遮罩 */}
      {isLoading && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
        <div className="flex items-center gap-3 rounded-lg bg-slate-800 px-6 py-4 shadow-lg">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
          <span className="text-sm text-ct-text">{running ? '运行中...' : '判题中...'}</span>
        </div>
      </div>}

      {/* 分割面板 */}
      <div id="split-container" className="flex flex-1 overflow-hidden" style={{ cursor: dragging.current ? 'col-resize' : undefined }}
        onMouseMove={handleSplitDrag} onMouseUp={() => { dragging.current = false; }} onMouseLeave={() => { dragging.current = false; }}>

        {/* ── 左面板 ── */}
        <div className="flex flex-col overflow-hidden border-r border-ct-border" style={{ width: splitRatio + '%' }}>
          {isDialogPhase ? (
            <>
              <div className="flex border-b border-ct-border bg-slate-900/50 text-xs">
                <TabButton label="Agent 对话" active={true} onClick={() => {}} />
              </div>
              <div className="flex-1 overflow-hidden">
                <AgentChat messages={tutorMessages} onSend={onAgentSend} disabled={!!problem} />
              </div>
            </>
          ) : (
            <>
              <div className="flex border-b border-ct-border bg-slate-900/50 text-xs min-h-[34px]"
                onDragOver={handleTabDragOver} onDrop={handleTabDrop('left')}>
                <button onClick={onNext} className="mr-1 rounded px-2 py-1 text-xs text-ct-muted hover:text-ct-text shrink-0" title="返回出题页">← 返回</button>
                {panelTabs.left.map(tid => (
                  <TabButton key={tid} label={TAB_LABELS[tid]} tabId={tid}
                    active={activeTabs.left === tid}
                    onClick={() => onSetActiveTabs(prev => ({ ...prev, left: tid }))}
                    onDragStart={handleTabDragStart(tid)} />
                ))}
                {panelTabs.left.length === 0 && (
                  <span className="px-3 py-2 text-ct-muted text-xs italic">拖入标签页</span>
                )}
              </div>
              {renderPanelContent('left')}
            </>
          )}
        </div>

        {/* 分割线 */}
        <div className="flex w-1.5 cursor-col-resize items-center justify-center bg-ct-border/30 hover:bg-ct-accent/40"
          onMouseDown={() => { dragging.current = true; }}>
          <div className="h-8 w-0.5 rounded bg-ct-muted/40" />
        </div>

        {/* ── 右面板 ── */}
        <div className="flex flex-col overflow-hidden" style={{ width: (100 - splitRatio) + '%' }}>
          {isDialogPhase ? (
            <>
              <div className="flex border-b border-ct-border bg-slate-900/50 text-xs">
                <button onClick={onNext} className="mr-auto rounded px-2 py-1 text-xs text-ct-muted hover:text-ct-text" title="返回出题页">← 返回</button>
                {panelTabs.right.map(tid => (
                  <TabButton key={tid} label={TAB_LABELS[tid]} tabId={tid}
                    active={activeTabs.right === tid}
                    onClick={() => onSetActiveTabs(prev => ({ ...prev, right: tid }))}
                    onDragStart={handleTabDragStart(tid)} />
                ))}
              </div>
              {renderPanelContent('right')}
              <div className="flex items-center gap-3 border-t border-ct-border p-3">
                <button onClick={onRun} disabled={!editorCode.trim() || running || isDialogPhase}
                  className="rounded border border-ct-border px-4 py-2 text-sm text-ct-text hover:bg-slate-700/30 disabled:opacity-40">{running ? '运行中...' : '\u25b6 运行'}</button>
                <button onClick={onSubmit} disabled={!editorCode.trim() || submittingFlag || isDialogPhase}
                  className="rounded bg-ct-accent px-5 py-2 text-sm font-medium text-white disabled:opacity-40">{submittingFlag ? '判题中...' : '提交'}</button>
                {latestVerdict && <span className="text-xs text-ct-muted">上次: <VerdictBadge verdict={latestVerdict} /></span>}
                <button onClick={onNext} className="ml-auto text-xs text-ct-muted hover:text-ct-text">换一题</button>
              </div>
            </>
          ) : (
            <>
              <div className="flex border-b border-ct-border bg-slate-900/50 text-xs min-h-[34px]"
                onDragOver={handleTabDragOver} onDrop={handleTabDrop('right')}>
                {panelTabs.right.map(tid => (
                  <TabButton key={tid} label={TAB_LABELS[tid]} tabId={tid}
                    active={activeTabs.right === tid}
                    onClick={() => onSetActiveTabs(prev => ({ ...prev, right: tid }))}
                    onDragStart={handleTabDragStart(tid)} />
                ))}
                {panelTabs.right.length === 0 && (
                  <span className="px-3 py-2 text-ct-muted text-xs italic">拖入标签页</span>
                )}
              </div>
              {renderPanelContent('right')}
              <div className="flex items-center gap-3 border-t border-ct-border p-3">
                {isDone ? (
                  <><button onClick={onNext} className="rounded bg-ct-accent px-5 py-2 text-sm font-medium text-white hover:opacity-90">下一题</button>
                  <span className="text-xs text-ct-muted">AC 了！继续挑战下一题</span></>
                ) : (
                  <><button onClick={onRun} disabled={!editorCode.trim() || running || isDialogPhase}
                    className="rounded border border-ct-border px-4 py-2 text-sm text-ct-text hover:bg-slate-700/30 disabled:opacity-40">{running ? '运行中...' : '\u25b6 运行'}</button>
                  <button onClick={onSubmit} disabled={!editorCode.trim() || submittingFlag || isDialogPhase}
                    className="rounded bg-ct-accent px-5 py-2 text-sm font-medium text-white disabled:opacity-40">{submittingFlag ? '判题中...' : '提交'}</button>
                  {latestVerdict && <span className="text-xs text-ct-muted">上次: <VerdictBadge verdict={latestVerdict} /></span>}
                  <button onClick={onNext} className="ml-auto text-xs text-ct-muted hover:text-ct-text">换一题</button>
                  </>)}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}