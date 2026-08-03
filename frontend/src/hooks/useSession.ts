/** 封装会话全部状态与回调，让 App.tsx 只管路由 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createSession, getState, runCode, submitCode, getReferenceCode } from '../api/session';
import type { Message, ProblemMeta, RunResult, SessionStateResp, Submission } from '../types/session';
import type { JudgeReport } from '../types/judge';
import { useSSE } from './useSSE';
import { useProgressSSE } from './useProgressSSE';

const BASE = 'http://localhost:8765';
export type Screen = 'welcome' | 'loading' | 'main' | 'error' | 'admin';
export type TabId = 'desc' | 'history' | 'reference' | 'code' | 'run' | 'tutor' | 'agent-history';

export const DEFAULT_TAB_PANEL: Record<TabId, 'left' | 'right'> = {
  desc: 'left', history: 'left', reference: 'left',
  code: 'right', run: 'right', tutor: 'right', 'agent-history': 'left',
};

export function useSession() {
  const [screen, setScreen] = useState<Screen>('welcome');
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [mode, setMode] = useState<string>('practice');
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
  const [activeTabs, setActiveTabs] = useState<{ left: TabId; right: TabId }>({ left: 'desc', right: 'code' });
  const [tabPanel, setTabPanel] = useState<Record<TabId, 'left' | 'right'>>({ ...DEFAULT_TAB_PANEL });
  const [splitRatio, setSplitRatio] = useState(50);
  const [chatInput, setChatInput] = useState('');
  const [phase, setPhase] = useState<string>('solving');
  const [status, setStatus] = useState('');
  const [nextProblemLoading, setNextProblemLoading] = useState(false);
  const { readStream } = useSSE();
  const { subscribe: subscribeProgress, close: closeProgress } = useProgressSSE();
  const dragging = useRef(false);
  const dragTab = useRef<TabId | null>(null);
  const editorInitialized = useRef(false);
  const chatEndRef = useRef<HTMLDivElement>(null);

  // ── 状态同步 ──
  function applySessionState(resp: SessionStateResp, fillEditor = false) {
    setSessionId(resp.session_id); setProblem(resp.problem);
    if (resp.mode) setMode(resp.mode);
    setTutorMessages(resp.tutor_messages); setHintLevel(resp.hint_level);
    setLatestVerdict(resp.last_verdict);
    if ((resp as any).phase) setPhase((resp as any).phase);
    setJudgeReport(resp.last_review_payload as JudgeReport | null);
    setSubmissions((resp.submissions || []) as Submission[]);
    if ((resp as any).last_run_results) setRunResults((resp as any).last_run_results);
    if (fillEditor && resp.problem?.starter_code && !editorInitialized.current) {
      setEditorCode(resp.problem.starter_code);
      editorInitialized.current = true;
    }
  }

  // ── SSE 实时进度（替代 setInterval 轮询 /state） ──
  // 后端 /session/{sid}/progress/stream 在题目就绪（或 agent 对话就绪）时会推送
  // 最终的 serialize_state，收到即进入对应界面，无需反复轮询。
  const startProgress = useCallback((sid: string) => {
    subscribeProgress(sid, {
      onProgress: (msg) => setProgressMsgs(prev => (prev.includes(msg) ? prev : [...prev, msg])),
      onDone: (state: any) => {
        applySessionState(state, true);
        if (state.problem) {
          // 题目就绪：补全默认模板、切到题面/编辑器
          if (!state.problem.starter_code && !editorInitialized.current) {
            setEditorCode('class Solution:\n    def solution(self):\n        pass\n');
            editorInitialized.current = true;
          }
          setActiveTabs({ left: 'desc', right: 'code' });
        }
        setProgressMsgs([]);
        setScreen('main');
      },
      onError: (msg) => {
        setErrorMsg(msg);
        setScreen('error');
      },
    });
  }, [subscribeProgress, applySessionState]);

  useEffect(() => () => { closeProgress(); }, [closeProgress]);

  // ── 创建会话 ──
  // Agent 模式的对话就绪与出题就绪都通过 SSE 的 done 事件推送（见 session.py
  // stream_progress），无需前端轮询 getState。
  const handleStart = useCallback(async (topic: string, difficulty: string, m: string) => {
    setTabPanel({ ...DEFAULT_TAB_PANEL }); setActiveTabs({ left: 'desc', right: 'code' });
    editorInitialized.current = false;
    setStatus('');
    setProgressMsgs([]);
    // Agent 模式直接进入对话界面（不开 loading），避免“出题中”闪屏；其余模式走 SSE 进度
    if (m !== 'agent') setScreen('loading');
    try {
      const resp = await createSession({ topic, difficulty, mode: m });
      setSessionId(resp.session_id);
      if (m === 'agent') {
        setScreen('main'); setMode('agent');
      }
      startProgress(resp.session_id);
    } catch (e) { setScreen('error'); setErrorMsg(String(e)); }
  }, [startProgress]);

  const handleStartExisting = useCallback(async (problemId: number) => {
    setScreen('loading'); setProgressMsgs([]);
    setTabPanel({ ...DEFAULT_TAB_PANEL }); setActiveTabs({ left: 'desc', right: 'code' });
    editorInitialized.current = false;
    try {
      const r = await fetch(BASE + '/session/by-problem/' + problemId, { method: 'POST' });
      if (!r.ok) throw new Error('failed: ' + r.status);
      applySessionState(await r.json(), true); setScreen('main');
    } catch (e) { setScreen('error'); setErrorMsg(String(e)); }
  }, []);

  // ── 提交 ──
  const handleSubmit = useCallback(async () => {
    if (!sessionId || submittingFlag) return;
    const sid = sessionId; const code = editorCode;
    setSubmittingFlag(true); setRunResults(null);
    try {
      const resp = await submitCode(sid, code);
      if (resp.tutor_message) setTutorMessages(prev => [...prev, { role: 'user' as const, content: code }, { role: 'tutor' as const, content: resp.tutor_message ?? '' }]);
      setHintLevel(resp.hint_level); setLatestVerdict(resp.verdict);
      const full = await getState(sid);
      setSubmissions((full.submissions || []) as Submission[]);
      // 同步后端 phase：AC 后后端 critic 会置 phase=reviewing，
      // 让 isDone (= phase==='reviewing' && verdict==='AC') 成立，从而显示「下一题」按钮
      if ((full as any).phase) setPhase((full as any).phase);
      if (full.problem) {
        try { const pr = await fetch(BASE + '/problem/' + full.problem.problem_id + '/submissions'); if (pr.ok) setSubmissions((await pr.json()).submissions || []); } catch {}
      }
      if (resp.verdict === 'AC') {
        if (resp.status === 'done') setJudgeReport(full.last_review_payload as JudgeReport | null);
        try { const ref = await getReferenceCode(sid); setReferenceCode(ref.code); } catch {}
      }
      setActiveTabs(prev => ({ ...prev, right: 'tutor' }));
    } catch (e) { setErrorMsg(String(e)); setScreen('error'); }
    finally { setSubmittingFlag(false); }
  }, [sessionId, editorCode, submittingFlag]);

  // ── 运行 ──
  const handleRun = useCallback(async () => {
    if (!sessionId || running || !editorCode.trim()) return;
    const sid = sessionId; const code = editorCode;
    setRunning(true); setRunResults(null); setActiveTabs(prev => ({ ...prev, right: 'run' }));
    try {
      const resp = await runCode(sid, code);
      setRunResults(resp.results);
      const passed = resp.results?.filter((r: any) => r.passed).length ?? 0;
      const total = resp.results?.length ?? 0;
      setTutorMessages(prev => [...prev, { role: 'user', content: `运行结果：${passed}/${total} 通过` }, { role: 'tutor', content: '' }]);
      await readStream(sid, `我运行了代码，结果：${passed}/${total} 通过。\n\`\`\`\n${code}\n\`\`\`\n请帮我分析一下。`, (token) => {
        setTutorMessages(prev => { const next = [...prev]; const last = next[next.length - 1]; if (last?.role === 'tutor') next[next.length - 1] = { role: 'tutor', content: (last.content || '') + token }; return next; });
      });
      setActiveTabs(prev => ({ ...prev, right: 'tutor' }));
    } catch (e) { setErrorMsg(String(e)); }
    finally { setRunning(false); }
  }, [sessionId, editorCode, running, readStream]);

  // ── Agent 对话 ──
  const handleAgentSend = useCallback(async (text: string) => {
    if (!sessionId) return;
    setTutorMessages(prev => [...prev, { role: 'user', content: text }, { role: 'tutor', content: '' }]);
    try {
      await readStream(sessionId, text, (token) => {
        setTutorMessages(prev => { const next = [...prev]; const last = next[next.length - 1]; if (last?.role === 'tutor') next[next.length - 1] = { role: 'tutor', content: (last.content || '') + token }; return next; });
      });
    } catch (e) { console.error('Agent chat error:', e); return; }
    // 对话若触发出下一题，题目就绪时后端会经 SSE 推送 done 事件自动进入主界面，
    // 无需轮询 getState。纯对话（无题）时 SSE 也会推 dialog-ready 的 done，无副作用。
    startProgress(sessionId);
  }, [sessionId, readStream, startProgress]);

  // ── 普通聊天 ──
  const handleChat = useCallback(async () => {
    if (!chatInput.trim() || !sessionId) return;
    const msg = chatInput.trim(); setChatInput('');
    setTutorMessages(prev => [...prev, { role: 'user' as const, content: msg }, { role: 'tutor' as const, content: '' }]);
    try {
      const ok = await readStream(sessionId, msg, (token) => {
        setTutorMessages(prev => { const next = [...prev]; const last = next[next.length - 1]; if (last?.role === 'tutor') next[next.length - 1] = { role: 'tutor' as const, content: (last.content || '') + token }; return next; });
      });
      if (!ok) setTutorMessages(prev => { const next = [...prev]; if (next.length) next[next.length - 1] = { role: 'tutor' as const, content: '(chat not available)' }; return next; });
    } catch {
      setTutorMessages(prev => { const next = [...prev]; if (next.length) next[next.length - 1] = { role: 'tutor' as const, content: '(chat error)' }; return next; });
    }
  }, [chatInput, sessionId, readStream]);

  // ── 下一题 / 新会话 ──
  const handleNext = useCallback(async () => {
    if (nextProblemLoading) return;

    // Agent 模式「放弃 / 下一题」：重入导师对话（保留历史、隐藏题目/代码栏，不出新题）
    const applyAgentReenter = (data: any) => {
      setProblem(null);
      setPhase('dialog');
      setEditorCode('');
      editorInitialized.current = false;
      if (data?.tutor_messages?.length) setTutorMessages(data.tutor_messages as Message[]);
      setHintLevel(0); setLatestVerdict(null); setJudgeReport(null);
      setRunResults(null); setSubmissions([]);
      setProgressMsgs([]);
      // 左栏对话，右栏显示空代码编辑器（占位，不让右侧空着或重复导师面板）
      setEditorCode('');
      setActiveTabs({ left: 'agent-history', right: 'code' });
    };

    const callNextProblem = async (preference: string = 'next_in_plan') => {
      setNextProblemLoading(true);
      setProgressMsgs(['正在准备下一题…']);
      // 用 SSE 实时收进度（替代 setInterval 轮询）；POST 返回的 data 才是权威结果
      subscribeProgress(sessionId!, {
        onProgress: (msg) => setProgressMsgs(prev => (prev.includes(msg) ? prev : [...prev, msg])),
      });
      try {
        const resp = await fetch(BASE + '/session/' + sessionId + '/next-problem', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ preference }),
        });
        if (!resp.ok) { setNextProblemLoading(false); return; }
        const data = await resp.json();

        if (data.problem) {
          setProblem(data.problem as ProblemMeta);
          setPhase(data.phase || 'solving');
          setEditorCode((data.problem as any).starter_code || '');
          // Agent 模式保留后端返回的 tutor_messages，不清空
          if (mode === 'agent' && data.tutor_messages?.length) {
            setTutorMessages(data.tutor_messages as Message[]);
          } else {
            setTutorMessages([]);
          }
          setHintLevel(0); setLatestVerdict(null); setJudgeReport(null);
          setRunResults(null); setSubmissions([]);
          setProgressMsgs([]);
          editorInitialized.current = false;
          setNextProblemLoading(false);
          closeProgress();
          return;
        }

        // Agent 模式 / 继续出题 重入对话：后端返回 problem=null + 历史（Bug 5/8/9）
        if (!data.problem && data.phase === 'dialog') {
          applyAgentReenter(data);
          setNextProblemLoading(false);
          closeProgress();
          return;
        }
      } catch { /* fall through to welcome */ }
      closeProgress();
      setNextProblemLoading(false);
    };

    // AC → 已完成：agent 模式重入出题对话，其余模式直接出下一题（不再显示"放弃这题"）
    if (latestVerdict === 'AC' && sessionId) {
      if (mode === 'agent') {
        setMode('agent');
        await callNextProblem('continue_dialog');
      } else {
        await callNextProblem('next_in_plan');
      }
      return;
    }

    // solving → 放弃确认
    if (phase === 'solving' && sessionId) {
      const ok = window.confirm('当前代码还没提交，确定放弃这题去下一题？');
      if (ok) { await callNextProblem(); }
      return;
    }

    // 默认：新会话（回 welcome）
    setScreen('welcome'); setSessionId(null); setProblem(null); setEditorCode('');
    setTutorMessages([]); setHintLevel(0); setLatestVerdict(null); setJudgeReport(null);
    setErrorMsg(''); setProgressMsgs([]); setRunResults(null); setSubmissions([]); setReferenceCode('');
    setMode('practice'); setTabPanel({ ...DEFAULT_TAB_PANEL }); setActiveTabs({ left: 'desc', right: 'code' });
    editorInitialized.current = false;
    setPhase('solving');
  }, [phase, latestVerdict, sessionId, mode]);

  const handleOpenAdmin = useCallback(() => setScreen('admin'), []);

  // ── 始终回到欢迎页（不受 phase 影响） ──
  const handleBackToWelcome = useCallback(() => {
    setScreen('welcome'); setSessionId(null); setProblem(null); setEditorCode('');
    setTutorMessages([]); setHintLevel(0); setLatestVerdict(null); setJudgeReport(null);
    setErrorMsg(''); setProgressMsgs([]); setRunResults(null); setSubmissions([]); setReferenceCode('');
    setMode('practice'); setTabPanel({ ...DEFAULT_TAB_PANEL }); setActiveTabs({ left: 'desc', right: 'code' });
    editorInitialized.current = false;
    setPhase('solving'); setStatus(''); setNextProblemLoading(false);
  }, []);

  return {
    screen, mode, phase, nextProblemLoading, problem, editorCode, tutorMessages, hintLevel, latestVerdict,
    judgeReport, submissions, referenceCode, errorMsg, progressMsgs, runResults,
    running, submittingFlag, activeTabs, tabPanel, splitRatio, chatInput,
    sessionId, status, isDialogPhase: mode === 'agent' && !problem && !nextProblemLoading,
    isGenerating: mode === 'agent' && status === 'awaiting_problem',
    isAC: latestVerdict === 'AC', isDone: latestVerdict === 'AC',
    dragging, dragTab, chatEndRef, editorInitialized,
    setEditorCode, setActiveTabs, setTabPanel, setSplitRatio, setChatInput,
    setTutorMessages, setRunResults, setProgressMsgs,
    onStart: handleStart, onStartExisting: handleStartExisting,
    onSubmit: handleSubmit,
    onRun: handleRun, onChat: handleChat, onNext: handleNext,
    onBackToWelcome: handleBackToWelcome,
    onOpenAdmin: handleOpenAdmin, onAgentSend: handleAgentSend,
    setScreen,
  };
}