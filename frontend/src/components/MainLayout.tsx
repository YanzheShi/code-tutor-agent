import { useEffect, useRef } from 'react';
import ProblemDesc from './LeftPanel/ProblemDesc';
import AgentChat from './LeftPanel/AgentChat';
import CodeEditor from './LeftPanel/CodeEditor';
import MessageList from './RightPanel/MessageList';
import MsgItem from './RightPanel/MsgItem';
import ReviewCard from './RightPanel/ReviewCard';
import SubmissionHistory from './SubmissionHistory';
import RunResults from './RunResults';
import { TabButton, VerdictBadge } from './TabButton';
import { useMemo } from 'react';
import type { Message, ProblemMeta, RunResult, Submission, FailedCase } from '../types/session';
import type { JudgeReport } from '../types/judge';

type TabId = 'desc' | 'history' | 'reference' | 'code' | 'run' | 'tutor' | 'agent-history' | 'trace';

const TAB_LABELS: Record<TabId, string> = {
  desc: '题目描述', history: '提交记录', reference: '参考代码',
  code: '代码', run: '运行', tutor: '导师', 'agent-history': '导师对话', trace: '轨迹分析',
};

export type MainLayoutProps = {
  problem: ProblemMeta | null;
  mode: string;
  phase: string;
  nextProblemLoading: boolean;
  activeTabs: { left: TabId; right: TabId };
  tabPanel: Record<TabId, 'left' | 'right'>;
  splitRatio: number;
  tutorMessages: Message[];
  chatInput: string;
  editorCode: string;
  hintLevel: number;
  latestVerdict: string | null;
  judgeReport: JudgeReport | null;
  referenceCode: string;
  submissions: Submission[];
  runResults: RunResult[] | null;
  progressMsgs: string[];
  running: boolean;
  submittingFlag: boolean;
  isDialogPhase: boolean;
  isGenerating: boolean;
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
  onBackToWelcome: () => void;
  onAgentSend: (text: string) => void;
  analyzingTrace?: boolean;
  onAnalyzeTrace?: () => void;
  traceFailed?: boolean;
  traceAnalysis?: any | null;
  traceMessages?: Message[];
  traceAsking?: boolean;
  traceInput?: string;
  onSetTraceInput?: (v: string) => void;
  onTraceAsk?: (text: string) => void;
}

export default function MainLayout(props: MainLayoutProps) {
  const {
    problem, mode, phase, nextProblemLoading, activeTabs, tabPanel, splitRatio, tutorMessages, chatInput,
    editorCode, hintLevel, latestVerdict, judgeReport, referenceCode,
    submissions, runResults, progressMsgs, running, submittingFlag, isDialogPhase, isGenerating, isDone,
    dragging, dragTab, chatEndRef,
    onSetChatInput, onSetActiveTabs, onSetTabPanel, onSetSplitRatio, onSetEditorCode,
    onSetTutorMessages, onSetRunResults, onSetProgressMsgs,
    onRun, onSubmit, onChat, onNext, onBackToWelcome, onAgentSend,
  analyzingTrace = false, onAnalyzeTrace,
  traceFailed = false,
  traceAnalysis = null, traceMessages = [], traceAsking = false, traceInput = '',
  onSetTraceInput = () => {}, onTraceAsk = () => {},
  } = props;

  const isLoading = running || submittingFlag;

  // 导师 Tab 自动滚到底部（AgentChat 已有同样逻辑，这里补上：
  // 之前 chatEndRef 只挂载从不滚动，导致点开导师 tab 时滚动条停在最上面）
  useEffect(() => {
    if (activeTabs.left === 'tutor' || activeTabs.right === 'tutor') {
      chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [chatEndRef, activeTabs, tutorMessages]);

  // Bug 2: 从最后一条 submission 的 base 阶段提取首个失败用例，用于「期望 vs 实际」对比
  const failedCase: FailedCase | null = useMemo(() => {
    if (latestVerdict === 'AC') return null;
    if (!submissions || submissions.length === 0) return null;
    const last = submissions[submissions.length - 1];
    const base = (last.judge_results || []).find(j => j.phase === 'base');
    if (!base) return null;
    const inputArgs = base.input_args || [];
    const expected = base.expected_output || '';
    const actual = base.actual_output || '';
    if (!inputArgs.length && !expected && !actual) return null;
    return { input_args: inputArgs, expected_output: expected, actual_output: actual, explanation: base.explanation || '', detail: base.detail || '' };
  }, [submissions, latestVerdict]);

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
      return <SubmissionHistory problemId={problem?.problem_id ?? 0} />;
    case 'reference':
      return (
        <div className="flex-1 overflow-y-auto p-4">
          {referenceCode ? <pre className="rounded border border-ct-border bg-ct-surface-secondary p-4 text-sm font-mono text-ct-text overflow-x-auto">{referenceCode}</pre>
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
        <div className="flex-1 flex flex-col min-h-0">
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            <MessageList messages={tutorMessages} verdict={latestVerdict} hintLevel={hintLevel} failedCase={failedCase} />
            {latestVerdict === 'AC' && judgeReport && <div className="mt-3"><ReviewCard report={judgeReport} /></div>}
            <div ref={chatEndRef} />
          </div>
          <div className="border-t border-ct-border p-3 flex gap-2">
            {latestVerdict === 'AC' && (
              <button onClick={onAnalyzeTrace} disabled={analyzingTrace}
                className="shrink-0 self-end rounded border border-ct-accent/50 px-3 py-2 text-xs text-ct-accent hover:bg-ct-accent/10 disabled:opacity-50"
                title="分析你本次做题的编辑轨迹（独立复盘，不写入能力画像）">
                {analyzingTrace ? '分析中…' : '📊 轨迹分析'}
              </button>
            )}
            <textarea value={chatInput} onChange={e => onSetChatInput(e.target.value)}
              placeholder="向导师提问..."
              rows={2}
              className="flex-1 rounded border border-ct-border bg-ct-input px-3 py-2 text-xs text-ct-text placeholder-ct-muted outline-none focus:border-ct-accent resize-none"
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onChat(); } }} />
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
    case 'trace':
      return (
        <div className="flex-1 flex flex-col min-h-0">
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            {analyzingTrace && <p className="text-sm text-ct-muted">⏳ 正在分析你的做题轨迹…</p>}
            {!analyzingTrace && traceMessages.length === 0 && (
              <div className="flex h-full items-center justify-center">
                <button onClick={onAnalyzeTrace} disabled={analyzingTrace}
                  className="rounded border border-ct-accent/50 px-4 py-2 text-sm text-ct-accent hover:bg-ct-accent/10 disabled:opacity-50"
                  title="分析你本次做题的编辑轨迹（独立复盘，不写入能力画像）">
                  📊 分析本次做题轨迹
                </button>
              </div>
            )}
            {traceMessages.map((m, i) => <MsgItem key={i} msg={m} />)}
            {traceFailed && !analyzingTrace && (
              <div className="flex justify-center pt-2">
                <button onClick={onAnalyzeTrace}
                  className="rounded border border-ct-accent/50 px-4 py-2 text-sm text-ct-accent hover:bg-ct-accent/10"
                  title="重新分析本次做题轨迹">
                  🔄 重试轨迹分析
                </button>
              </div>
            )}
          </div>
          {traceAnalysis && (
            <div className="border-t border-ct-border p-3 flex gap-2">
              <textarea value={traceInput} onChange={e => onSetTraceInput(e.target.value)}
                placeholder="对分析追问，比如「为什么卡了 200 秒？」"
                rows={2}
                className="flex-1 rounded border border-ct-border bg-ct-input px-3 py-2 text-xs text-ct-text placeholder-ct-muted outline-none focus:border-ct-accent resize-none"
                onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onTraceAsk(traceInput); } }} />
              <button onClick={() => onTraceAsk(traceInput)} disabled={!traceInput.trim() || traceAsking}
                className="rounded bg-ct-accent px-3 py-2 text-xs text-white disabled:opacity-40">追问</button>
            </div>
          )}
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
        <div className="flex items-center gap-2 border-b border-ct-border bg-ct-surface px-4 py-1.5 text-xs text-ct-muted">
          <div className="h-3 w-3 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
          <span className="flex-1">{progressMsgs[progressMsgs.length - 1]}</span>
        </div>
      )}

      {/* 全屏 loading 遮罩 */}
      {/* 全屏 loading 遮罩 */}{isLoading && <div className="fixed inset-0 z-50 flex items-center justify-center bg-ct-overlay">
              <div className="flex items-center gap-3 rounded-lg bg-ct-surface-secondary px-6 py-4 shadow-lg">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
                <span className="text-sm text-ct-text">{running ? '运行中...' : '判题中...'}</span>
              </div>
            </div>}

            {/* 下一题生成中遮罩 */}
            {nextProblemLoading && <div className="fixed inset-0 z-50 flex items-center justify-center bg-ct-overlay">
              <div className="flex flex-col items-center gap-3 rounded-lg bg-ct-surface-secondary px-8 py-6 shadow-lg">
                <div className="h-6 w-6 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
                <div className="max-w-md space-y-1 text-center">
                  {(progressMsgs.length > 0 ? progressMsgs : ['正在准备下一题…']).map((msg, i) => (
                    <p key={i} className={'text-sm ' + (i === (progressMsgs.length > 0 ? progressMsgs.length - 1 : 0) ? 'text-ct-accent' : 'text-ct-muted/60')}>{msg}</p>
                  ))}
                </div>
              </div>
            </div>}

      {/* 分割面板 */}
      <div id="split-container" className="flex flex-1 overflow-hidden" style={{ cursor: dragging.current ? 'col-resize' : undefined }}
        onMouseMove={handleSplitDrag} onMouseUp={() => { dragging.current = false; }} onMouseLeave={() => { dragging.current = false; }}>

        {/* ── 左面板 ── */}
        <div className="flex flex-col overflow-hidden border-r border-ct-border" style={{ width: splitRatio + '%' }}>
          {isDialogPhase ? (
            <>
              <div className="flex border-b border-ct-border bg-ct-surface-secondary text-xs min-h-[34px]">
                <button onClick={onBackToWelcome} className="rounded px-2 py-1 text-xs text-ct-muted hover:text-ct-text" title="回到主页">← 回到主页</button>
                <TabButton label="Agent 对话" active={true} onClick={() => {}} />
              </div>
              <div className="flex-1 overflow-hidden relative">
                <AgentChat messages={tutorMessages} onSend={onAgentSend} showSelector disabled={!!problem || isGenerating || (isDialogPhase && progressMsgs.length > 0)} />
                {(isGenerating || (isDialogPhase && progressMsgs.length > 0)) && (
                  <div className="absolute inset-x-0 bottom-0 flex items-center gap-2 border-t border-ct-border bg-ct-surface px-4 py-2 text-xs text-ct-muted">
                    <div className="h-3 w-3 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
                    <span className="flex-1">{progressMsgs.length > 0 ? progressMsgs[progressMsgs.length - 1] : '正在为你生成题目 🚀'}</span>
                  </div>
                )}
              </div>
            </>
          ) : (
            <>
              <div className="flex border-b border-ct-border bg-ct-surface-secondary text-xs min-h-[34px]"
                onDragOver={handleTabDragOver} onDrop={handleTabDrop('left')}>
                <button onClick={onBackToWelcome} className="mr-1 rounded px-2 py-1 text-xs text-ct-muted hover:text-ct-text shrink-0" title="返回出题页">← 返回</button>
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
              <div className="flex border-b border-ct-border bg-ct-surface-secondary text-xs">
                <button onClick={onNext} className="mr-auto rounded px-2 py-1 text-xs text-ct-muted hover:text-ct-text" title="返回出题页">← 返回</button>
                {panelTabs.right.filter(tid => tid !== 'tutor').map(tid => (
                  <TabButton key={tid} label={TAB_LABELS[tid]} tabId={tid}
                    active={activeTabs.right === tid}
                    onClick={() => onSetActiveTabs(prev => ({ ...prev, right: tid }))}
                    onDragStart={handleTabDragStart(tid)} />
                ))}
              </div>
              {renderPanelContent('right')}
              {problem ? (
                <div className="flex items-center gap-3 border-t border-ct-border p-3">
                  <button onClick={onRun} disabled={!editorCode.trim() || running || isDialogPhase}
                    className="rounded border border-ct-border px-4 py-2 text-sm text-ct-text hover:bg-ct-hover disabled:opacity-40">{running ? '运行中...' : '\u25b6 运行'}</button>
                  <button onClick={onSubmit} disabled={!editorCode.trim() || submittingFlag || isDialogPhase}
                    className="rounded bg-ct-accent px-5 py-2 text-sm font-medium text-white disabled:opacity-40">{submittingFlag ? '判题中...' : '提交'}</button>
                  {latestVerdict && <span className="text-xs text-ct-muted">上次: <VerdictBadge verdict={latestVerdict} /></span>}
                  <button onClick={onNext} className="ml-auto text-xs text-ct-muted hover:text-ct-text">{phase === 'solving' ? '放弃这题' : '换一题'}</button>
                </div>
              ) : (
                <div className="flex items-center border-t border-ct-border p-3">
                  <span className="text-xs text-ct-muted">题目生成后可运行、提交，或让导师换一题</span>
                </div>
              )}
            </>
          ) : (
            <>
              <div className="flex border-b border-ct-border bg-ct-surface-secondary text-xs min-h-[34px]"
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
                <button onClick={onRun} disabled={!editorCode.trim() || running || isDialogPhase}
                  className="rounded border border-ct-border px-4 py-2 text-sm text-ct-text hover:bg-ct-hover disabled:opacity-40">{running ? '运行中...' : '\u25b6 运行'}</button>
                <button onClick={onSubmit} disabled={!editorCode.trim() || submittingFlag || isDialogPhase}
                  className="rounded bg-ct-accent px-5 py-2 text-sm font-medium text-white disabled:opacity-40">{submittingFlag ? '判题中...' : '提交'}</button>
                {latestVerdict && <span className="text-xs text-ct-muted">上次: <VerdictBadge verdict={latestVerdict} /></span>}
                {isDone ? (
                  <><span className="text-xs text-ct-success">AC 了！可继续提交不同解法</span>
                  <button onClick={onNext} className="ml-auto rounded bg-ct-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90">继续出题</button></>
                ) : (
                  <button onClick={onNext} className="ml-auto text-xs text-ct-muted hover:text-ct-text">{phase === 'solving' ? '放弃这题' : '换一题'}</button>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}