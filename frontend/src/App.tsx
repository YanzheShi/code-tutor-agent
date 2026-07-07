import { useCallback, useEffect, useRef, useState } from 'react';
import { createSession, getState, runCode, submitCode, getReferenceCode } from './api/session';
import type { Message, ProblemMeta, RunResult, SessionStateResp, Submission } from './types/session';
import type { JudgeReport } from './types/judge';
import WelcomeScreen from './components/WelcomeScreen';
import AdminPanel from './components/AdminPanel';
import ProblemDesc from './components/LeftPanel/ProblemDesc';
import CodeEditor from './components/LeftPanel/CodeEditor';
import MessageList from './components/RightPanel/MessageList';
import ReviewCard from './components/RightPanel/ReviewCard';

const BASE = 'http://localhost:8765';
type Screen = 'welcome' | 'loading' | 'main' | 'error' | 'admin';
type LeftTab = 'desc' | 'history' | 'reference';
type RightTab = 'code' | 'run' | 'tutor';

export default function App() {
  const [screen, setScreen] = useState<Screen>('welcome');
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [problem, setProblem] = useState<ProblemMeta | null>(null);
  const [editorCode, setEditorCode] = useState('');
  const [tutorMessages, setTutorMessages] = useState<Message[]>([]);
  const [hintLevel, setHintLevel] = useState(0);
  const [latestVerdict, setLatestVerdict] = useState<string | null>(null);
  const [judgeReport, setJudgeReport] = useState<JudgeReport | null>(null);
  const [submissions, setSubmissions] = useState<Submission[]>([]);
  const [referenceCode, setReferenceCode] = useState('');
  const [errorMsg, setErrorMsg] = useState('');
  const [progressMsgs, setProgressMsgs] = useState<string[]>([]);
  const [runResults, setRunResults] = useState<RunResult[] | null>(null);
  const [running, setRunning] = useState(false);
  const [submittingFlag, setSubmittingFlag] = useState(false);
  const [activeLeftTab, setActiveLeftTab] = useState<LeftTab>('desc');
  const [activeRightTab, setActiveRightTab] = useState<RightTab>('code');
  const [splitRatio, setSplitRatio] = useState(50);
  const [chatInput, setChatInput] = useState('');
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
    const screenRef = useRef(screen);
    useEffect(() => { screenRef.current = screen; }, [screen]);
    const dragging = useRef(false);
    const chatEndRef = useRef<HTMLDivElement>(null);

  const startPolling = useCallback((sid: string) => {
    pollTimer.current = setInterval(async () => {
      try {
        const state = await getState(sid);
        if (state.progress_messages) setProgressMsgs(state.progress_messages);
        // Switch to main screen once problem is ready
        if (state.status !== 'generating' && state.problem && screenRef.current === 'loading') {
          applySessionState(state, true);
          setScreen('main');
        }
        // Stop polling when background gen is done (✅ marker) or failed (❌/⚠️)
            const msgs = state.progress_messages || [];
            const bgDone = msgs.some(m => m.includes('✅') || m.includes('已就绪') || m.includes('已导入'));
            const bgFailed = msgs.some(m => m.includes('❌') || m.includes('⚠️'));
            if (bgDone) {
              if (pollTimer.current) clearInterval(pollTimer.current);
            }
            if (bgFailed) {
              if (pollTimer.current) clearInterval(pollTimer.current);
              if (screenRef.current === 'loading') {
                setErrorMsg('生成失败，请重试');
                setScreen('error');
              }
            }
      } catch {}
    }, 1500);
  }, []);

  const handleStart = useCallback(async (topic: string, difficulty: string, mode: string) => {
    setScreen('loading'); setProgressMsgs([]);
    try {
      const resp = await createSession({ topic, difficulty, mode });
      setSessionId(resp.session_id); startPolling(resp.session_id);
    } catch (e) { setScreen('error'); setErrorMsg(String(e)); }
  }, [startPolling]);

  const handleStartExisting = useCallback(async (problemId: number) => {
    setScreen('loading'); setProgressMsgs([]);
    try {
      const r = await fetch(BASE + '/session/by-problem/' + problemId, { method: 'POST' });
      if (!r.ok) throw new Error('failed: ' + r.status);
      const state: SessionStateResp = await r.json();
      applySessionState(state, true); setScreen('main');
    } catch (e) { setScreen('error'); setErrorMsg(String(e)); }
  }, []);

  const handleStartLeetcode = useCallback(async (url: string) => {
    setScreen('loading'); setProgressMsgs(['正在解析 LeetCode 题目...']);
    try {
      const r1 = await fetch(BASE + '/leetcode/parse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
      if (!r1.ok) throw new Error('解析失败 (' + r1.status + ')');
      const parsed = await r1.json();
      const r2 = await fetch(BASE + '/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ topic: parsed.title || 'leetcode', difficulty: parsed.difficulty || 'medium', mode: 'practice', leetcode: parsed }) });
      if (!r2.ok) throw new Error('创建会话失败 (' + r2.status + ')');
      const resp = await r2.json(); setSessionId(resp.session_id); startPolling(resp.session_id);
    } catch (e) { setScreen('error'); setErrorMsg(e instanceof Error ? e.message : String(e)); }
  }, [startPolling]);

  useEffect(() => { return () => { if (pollTimer.current) clearInterval(pollTimer.current); }; }, []);

  function applySessionState(resp: SessionStateResp, fillEditor = false) {
    setSessionId(resp.session_id); setProblem(resp.problem);
    setTutorMessages(resp.tutor_messages); setHintLevel(resp.hint_level);
    setLatestVerdict(resp.last_verdict);
    setJudgeReport(resp.last_review_payload as JudgeReport | null);
    setSubmissions((resp.submissions || []) as Submission[]);
    if ((resp as any).last_run_results) setRunResults((resp as any).last_run_results);
    if (fillEditor && resp.problem?.starter_code) setEditorCode(resp.problem.starter_code);
  }

  const handleSubmit = useCallback(async () => {
    if (!sessionId || submittingFlag) return;
    const sid = sessionId; const code = editorCode;
    setSubmittingFlag(true); setRunResults(null);
    try {
      const resp = await submitCode(sid, code);
      if (resp.tutor_message) setTutorMessages(prev => [...prev, { role: 'user' as const, content: code.slice(0, 200) }, { role: 'tutor' as const, content: resp.tutor_message }]);
      setHintLevel(resp.hint_level); setLatestVerdict(resp.verdict);
      const full = await getState(sid);
            setSubmissions((full.submissions || []) as Submission[]);
            // Also fetch persistent submissions
            const p = full.problem;
            if (p) {
              try {
                const pr = await fetch(BASE + '/problem/' + p.problem_id + '/submissions');
                if (pr.ok) { const pd = await pr.json(); setSubmissions(pd.submissions || []); }
              } catch {}
            }
      if (resp.verdict === 'AC') {
        if (resp.status === 'done') setJudgeReport(full.last_review_payload as JudgeReport | null);
        try { const ref = await getReferenceCode(sid); setReferenceCode(ref.code); } catch {}
      }
      setActiveRightTab('tutor');
    } catch (e) { setErrorMsg(String(e)); setScreen('error'); }
    finally { setSubmittingFlag(false); }
  }, [sessionId, editorCode, submittingFlag, submitCode, getState, getReferenceCode]);

  const handleRun = useCallback(async () => {
    if (!sessionId || running || !editorCode.trim()) return;
    const sid = sessionId; const code = editorCode;
    setRunning(true); setRunResults(null); setActiveRightTab('run');
    try { const resp = await runCode(sid, code); setRunResults(resp.results); }
    catch (e) { setErrorMsg(String(e)); }
    finally { setRunning(false); }
  }, [sessionId, editorCode, running, runCode]);

  const handleChat = useCallback(async () => {
    if (!chatInput.trim() || !sessionId) return;
    const msg = chatInput.trim();
    setChatInput('');
    setTutorMessages(prev => [...prev, { role: 'user' as const, content: msg }, { role: 'tutor' as const, content: '' }]);

    try {
      const resp = await fetch(BASE + '/session/' + sessionId + '/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg }),
      });
      if (!resp.ok || !resp.body) {
        setTutorMessages(prev => { const next = [...prev]; if (next.length) next[next.length - 1] = { role: 'tutor' as const, content: '(chat not available)' }; return next; });
        return;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split('\n\n');
        buffer = events.pop() || '';
        for (const event of events) {
          if (event.startsWith('data: ')) {
            const token = event.slice(6);
            if (token === '__DONE__') continue;
            setTutorMessages(prev => {
              const next = [...prev];
              const last = next[next.length - 1];
              if (last && last.role === 'tutor') {
                next[next.length - 1] = { role: 'tutor' as const, content: (last.content || '') + token };
              }
              return next;
            });
          }
        }
      }
    } catch (e) {
      setTutorMessages(prev => { const next = [...prev]; if (next.length) next[next.length - 1] = { role: 'tutor' as const, content: '(chat error)' }; return next; });
    }
    }, [chatInput, sessionId]);

  const handleNext = useCallback(() => {
    setScreen('welcome'); setSessionId(null); setProblem(null); setEditorCode('');
    setTutorMessages([]); setHintLevel(0); setLatestVerdict(null); setJudgeReport(null);
    setErrorMsg(''); setProgressMsgs([]); setRunResults(null); setSubmissions([]); setReferenceCode('');
  }, []);

  const handleOpenAdmin = useCallback(() => {
    setScreen('admin');
  }, []);

  const handleSplitDrag = useCallback((e: React.MouseEvent | MouseEvent) => {
    if (!dragging.current) return;
    const container = document.getElementById('split-container');
    if (!container) return;
    const rect = container.getBoundingClientRect();
    setSplitRatio(Math.max(20, Math.min(80, ((e.clientX - rect.left) / rect.width) * 100)));
  }, []);

  const isLoading = running || submittingFlag;

  // === Screens ===
  if (screen === 'error') return (
    <div className="flex h-screen items-center justify-center"><div className="text-center">
      <p className="text-lg text-ct-error">出错了</p>
      <p className="mt-1 text-sm text-ct-muted">{errorMsg}</p>
      <button onClick={handleNext} className="mt-4 rounded bg-ct-accent px-4 py-2 text-sm text-white">重试</button>
    </div></div>
  );
  if (screen === 'welcome') return <WelcomeScreen onStart={handleStart} onStartExisting={handleStartExisting} onStartLeetcode={handleStartLeetcode} onOpenAdmin={handleOpenAdmin} />;
  if (screen === 'loading') return (
    <div className="flex h-screen flex-col items-center justify-center gap-4">
      <div className="flex items-center gap-2"><div className="h-4 w-4 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" /><p className="text-lg text-ct-muted">出题中，请稍候...</p></div>
      {progressMsgs.length > 0 && <div className="max-w-md space-y-1">{progressMsgs.map((msg, i) => <p key={i} className={'text-sm ' + (i === progressMsgs.length - 1 ? 'text-ct-accent' : 'text-ct-muted/60')}>{msg}</p>)}</div>}
    </div>
  );
  if (screen === 'admin') return <AdminPanel onClose={() => setScreen('welcome')} />;

  const isAC = latestVerdict === 'AC'; const isDone = isAC && judgeReport !== null;
  const runPassed = runResults ? runResults.filter(r => r.passed).length : 0;
  const runFailed = runResults ? runResults.filter(r => !r.passed) : [];
  const leftTabs: LeftTab[] = ['desc', 'history', ...(isAC ? ['reference' as LeftTab] : [])];
  const rightTabs: RightTab[] = ['code', 'run', 'tutor'];

  return (
    <div className="flex h-screen flex-col">
      {/* 后台生成进度条 */}
      {progressMsgs.length > 0 && (
        <div className="flex items-center gap-2 border-b border-ct-border bg-slate-800/60 px-4 py-1.5 text-xs text-ct-muted">
          <div className="h-3 w-3 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
          <span className="flex-1">{progressMsgs[progressMsgs.length - 1]}</span>
        </div>
      )}
      {/* 加载遮罩 */}
      {isLoading && <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
        <div className="flex items-center gap-3 rounded-lg bg-slate-800 px-6 py-4 shadow-lg">
          <div className="h-5 w-5 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />
          <span className="text-sm text-ct-text">{running ? '运行中...' : '判题中...'}</span>
        </div>
      </div>}

      <div id="split-container" className="flex flex-1 overflow-hidden" style={{ cursor: dragging.current ? 'col-resize' : undefined }}
        onMouseMove={handleSplitDrag} onMouseUp={() => { dragging.current = false; }} onMouseLeave={() => { dragging.current = false; }}>
        {/* 返回按钮 */}
        <button onClick={handleNext} className="absolute left-2 top-2 z-10 rounded bg-slate-800/80 px-2 py-1 text-xs text-ct-muted hover:text-ct-text"
          title="返回出题页">← 返回</button>
        {/* ── 左面板 ── */}
        <div className="flex flex-col overflow-hidden border-r border-ct-border" style={{ width: splitRatio + '%' }}>
          <div className="flex border-b border-ct-border bg-slate-900/50 text-xs">
            <TabButton label="题目描述" active={activeLeftTab === 'desc'} onClick={() => setActiveLeftTab('desc')} />
            <TabButton label="提交记录" active={activeLeftTab === 'history'} onClick={() => setActiveLeftTab('history')} />
            {isAC && <TabButton label="参考代码" active={activeLeftTab === 'reference'} onClick={() => setActiveLeftTab('reference')} />}
          </div>

          <div className={'flex-1 overflow-y-auto p-4 ' + (activeLeftTab === 'desc' ? 'block' : 'hidden')}>
            {problem && <ProblemDesc problem={problem} />}
          </div>

          <div className={'flex-1 overflow-y-auto p-4 space-y-3 ' + (activeLeftTab === 'history' ? 'block' : 'hidden')}>
            {submissions.length === 0 && <p className="text-sm text-ct-muted">暂无提交记录</p>}
            {[...submissions].reverse().map((s, i) => (
              <div key={i} className="rounded border border-ct-border bg-slate-800/20 p-3 text-xs">
                <div className="flex items-center gap-3 mb-2">
                  <span className={'font-bold text-sm ' + (s.verdict === 'AC' ? 'text-ct-success' : s.verdict === 'WA' ? 'text-ct-warn' : 'text-ct-error')}>
                    {s.verdict || (s.judge_results?.[0]?.status) || 'RE'}
                  </span>
                  <span className="text-ct-muted">{s.timestamp || new Date().toLocaleTimeString()}</span>
                </div>
                <div className="flex gap-2 text-ct-muted mb-1">
                  {s.judge_results?.map((jr, j) => (
                    <span key={j} className={'text-xs ' + (jr.status === 'AC' ? 'text-ct-success' : jr.status === 'WA' ? 'text-ct-warn' : 'text-ct-error')}>
                      {jr.phase}: {jr.status} ({jr.runtime_ms?.toFixed(0)}ms)
                    </span>
                  ))}
                </div>
                <pre className="max-h-24 overflow-y-auto rounded bg-slate-900/50 p-2 text-ct-muted text-xs">
                  {s.code?.slice(0, 300) || ''}{s.code?.length > 300 ? '...' : ''}
                </pre>
              </div>
            ))}
          </div>

          <div className={'flex-1 overflow-y-auto p-4 ' + (activeLeftTab === 'reference' ? 'block' : 'hidden')}>
            {referenceCode ? <pre className="rounded border border-ct-border bg-slate-900/50 p-4 text-sm font-mono text-ct-text overflow-x-auto">{referenceCode}</pre>
            : <p className="text-sm text-ct-muted">暂无参考代码</p>}
          </div>
        </div>

        <div className="flex w-1.5 cursor-col-resize items-center justify-center bg-ct-border/30 hover:bg-ct-accent/40"
          onMouseDown={() => { dragging.current = true; }}>
          <div className="h-8 w-0.5 rounded bg-ct-muted/40" />
        </div>

        {/* ── 右面板 ── */}
        <div className="flex flex-col overflow-hidden" style={{ width: (100 - splitRatio) + '%' }}>
          <div className="flex border-b border-ct-border bg-slate-900/50 text-xs">
            <TabButton label="代码" active={activeRightTab === 'code'} onClick={() => setActiveRightTab('code')} />
            <TabButton label="运行" active={activeRightTab === 'run'} onClick={() => setActiveRightTab('run')}
              badge={runResults ? (runPassed === runResults.length ? '\u2713' : '\u2717') : undefined} />
            <TabButton label="导师" active={activeRightTab === 'tutor'} onClick={() => setActiveRightTab('tutor')}
              badge={tutorMessages.length > 0 ? String(tutorMessages.filter(m => m.role === 'tutor').length) : undefined} />
          </div>

          {/* ── 代码 Tab ── */}
          <div className={'flex-1 flex flex-col ' + (activeRightTab === 'code' ? 'flex' : 'hidden')}>
            <div className="flex-1"><CodeEditor code={editorCode} onChange={setEditorCode} starterCode={problem?.starter_code} /></div>
          </div>

          {/* ── 运行 Tab ── */}
          <div className={'flex-1 overflow-y-auto p-4 space-y-3 ' + (activeRightTab === 'run' ? 'block' : 'hidden')}>
            {!runResults && !running && <p className="text-sm text-ct-muted">点击「运行」查看结果</p>}
            {running && <p className="text-sm text-ct-accent animate-pulse">运行中...</p>}
            {runResults && (
              <>
                <h3 className={'text-sm font-bold ' + (runPassed === runResults.length ? 'text-ct-success' : 'text-ct-warn')}>
                  运行结果: {runPassed}/{runResults.length} 通过
                  <span className="ml-2 text-[10px] text-ct-muted font-normal">Judge0</span>
                </h3>
                {runResults.map(r => (
                  <div key={r.test_case_id} className={"rounded border p-3 text-xs " + (r.passed ? "border-green-800/30 bg-green-900/10" : "border-red-800/30 bg-red-900/10")}>
                    <div className="flex items-center gap-2 mb-1">
                      <span className={"font-bold " + (r.passed ? "text-ct-success" : "text-ct-error")}>{r.passed ? "✓" : "✗"}</span>
                      <span className="text-ct-text font-medium">用例 #{r.test_case_id}</span>
                      <span className="text-ct-muted">({r.status})</span>
                      {r.runtime_ms > 0 && <span className="text-ct-muted text-[10px]">{r.runtime_ms.toFixed(1)}ms</span>}
                      {r.memory_kb > 0 && <span className="text-ct-muted text-[10px] ml-1">| {r.memory_kb.toFixed(0)}KB</span>}
                    </div>
                    {r.detail && <div className="text-ct-muted mb-1">{r.detail}</div>}
                    <div className="text-ct-muted">期望: {r.expected}</div>
                  </div>
                ))}
              </>
            )}
          </div>

          {/* ── 导师 Tab ── */}
          <div className={'flex-1 flex flex-col ' + (activeRightTab === 'tutor' ? 'flex' : 'hidden')}>
            <div className="flex-1 overflow-y-auto p-4 space-y-3">
              <MessageList messages={tutorMessages} verdict={latestVerdict} hintLevel={hintLevel} />
              {latestVerdict === 'AC' && judgeReport && <div className="mt-3"><ReviewCard report={judgeReport} /></div>}
              <div ref={chatEndRef} />
            </div>
            {/* 聊天输入框 */}
            <div className="border-t border-ct-border p-3 flex gap-2">
              <input type="text" value={chatInput} onChange={e => setChatInput(e.target.value)}
                placeholder="向导师提问..."
                className="flex-1 rounded border border-ct-border bg-slate-800/50 px-3 py-2 text-xs text-ct-text placeholder-ct-muted outline-none focus:border-ct-accent"
                onKeyDown={e => { if (e.key === 'Enter') handleChat(); }} />
              <button onClick={handleChat} disabled={!chatInput.trim()}
                className="rounded bg-ct-accent px-3 py-2 text-xs text-white disabled:opacity-40">发送</button>
            </div>
          </div>

          {/* 底部操作栏 */}
          <div className="flex items-center gap-3 border-t border-ct-border p-3">
            {isDone ? (
              <><button onClick={handleNext} className="rounded bg-ct-accent px-5 py-2 text-sm font-medium text-white hover:opacity-90">下一题</button>
              <span className="text-xs text-ct-muted">AC 了！继续挑战下一题</span></>
            ) : (
              <><button onClick={handleRun} disabled={!editorCode.trim() || running}
                className="rounded border border-ct-border px-4 py-2 text-sm text-ct-text hover:bg-slate-700/30 disabled:opacity-40">{running ? '运行中...' : '\u25b6 运行'}</button>
              <button onClick={handleSubmit} disabled={!editorCode.trim() || submittingFlag}
                className="rounded bg-ct-accent px-5 py-2 text-sm font-medium text-white disabled:opacity-40">{submittingFlag ? '判题中...' : '提交'}</button>
              {latestVerdict && <span className="text-xs text-ct-muted">上次: <VerdictBadge verdict={latestVerdict} /></span>}
              <button onClick={handleNext} className="ml-auto text-xs text-ct-muted hover:text-ct-text">换一题</button>
              </>)}
          </div>
        </div>
      </div>
    </div>
  );
}

function TabButton({ label, active, onClick, badge }: { label: string; active: boolean; onClick: () => void; badge?: string }) {
  return (
    <button onClick={onClick} className={'relative px-4 py-2 font-medium transition-colors ' + (active ? 'border-b-2 border-ct-accent text-ct-text' : 'text-ct-muted hover:text-ct-text')}>
      {label}
      {badge !== undefined && <span className={'ml-1 ' + (active ? 'text-ct-accent' : 'text-ct-muted')}>{badge}</span>}
    </button>
  );
}

function VerdictBadge({ verdict }: { verdict: string }) {
  const colors: Record<string, string> = { AC: 'text-ct-success', WA: 'text-ct-warn', TLE: 'text-ct-error', RE: 'text-ct-error' };
  return <span className={'font-bold ' + (colors[verdict] ?? 'text-ct-muted')}>{verdict}</span>;
}
