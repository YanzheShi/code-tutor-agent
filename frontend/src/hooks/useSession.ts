/** 封装会话全部状态与回调，让 App.tsx 只管路由 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createSession, getState, runCode, submitCode, getReferenceCode } from '../api/session';
import type { Message, ProblemMeta, RunResult, SessionStateResp, Submission } from '../types/session';
import type { JudgeReport } from '../types/judge';
import { useSSE } from './useSSE';

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
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const screenRef = useRef(screen);
  useEffect(() => { screenRef.current = screen; }, [screen]);
  const dragging = useRef(false);
  const dragTab = useRef<TabId | null>(null);
  const editorInitialized = useRef(false);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const { readStream } = useSSE();

  // ── 状态同步 ──
  function applySessionState(resp: SessionStateResp, fillEditor = false) {
    setSessionId(resp.session_id); setProblem(resp.problem);
    if (resp.mode) setMode(resp.mode);
    setTutorMessages(resp.tutor_messages); setHintLevel(resp.hint_level);
    setLatestVerdict(resp.last_verdict);
    setJudgeReport(resp.last_review_payload as JudgeReport | null);
    setSubmissions((resp.submissions || []) as Submission[]);
    if ((resp as any).last_run_results) setRunResults((resp as any).last_run_results);
    if (fillEditor && resp.problem?.starter_code && !editorInitialized.current) {
      setEditorCode(resp.problem.starter_code);
      editorInitialized.current = true;
    }
  }

  // ── 轮询 ──
  const startPolling = useCallback((sid: string) => {
    pollTimer.current = setInterval(async () => {
      try {
        if (screenRef.current === 'main') {
          if (pollTimer.current) clearInterval(pollTimer.current);
          return;
        }
        const st = await getState(sid);
        if (st.progress_messages) setProgressMsgs(st.progress_messages);
        if (st.mode) setMode(st.mode);
        if (st.mode === 'agent' && st.tutor_messages?.length) {
          setTutorMessages(st.tutor_messages as Message[]);
        }
        if (st.status !== 'generating' && st.problem && screenRef.current === 'loading') {
          applySessionState(st, true);
          setScreen('main');
        }
        if (st.mode === 'agent' && st.problem && st.status !== 'dialog') {
          applySessionState(st, true);
          if (!st.problem?.starter_code && !editorInitialized.current) {
            setEditorCode('class Solution:\n    def solution(self, nums, val):\n        pass\n');
            editorInitialized.current = true;
          }
          setActiveTabs(prev => ({ ...prev, left: 'desc' }));
          setActiveTabs(prev => ({ ...prev, right: 'code' }));
        }
        const msgs = st.progress_messages || [];
        const bgDone = msgs.some(m => m.includes('✅') || m.includes('已就绪') || m.includes('已导入'));
        const bgFailed = msgs.some(m => m.includes('❌') || m.includes('⚠️'));
        if (bgDone) {
          if (pollTimer.current) clearInterval(pollTimer.current);
          setProgressMsgs([]);
        }
        if (bgFailed) {
          if (pollTimer.current) clearInterval(pollTimer.current);
          if (screenRef.current === 'loading') { setErrorMsg('生成失败，请重试'); setScreen('error'); }
        }
      } catch {}
    }, 1500);
  }, []);

  useEffect(() => () => { if (pollTimer.current) clearInterval(pollTimer.current); }, []);

  // ── 创建会话 ──
  const handleStart = useCallback(async (topic: string, difficulty: string, m: string) => {
    setScreen('loading'); setProgressMsgs([]);
    setTabPanel({ ...DEFAULT_TAB_PANEL }); setActiveTabs({ left: 'desc', right: 'code' });
    editorInitialized.current = false;
    if (m === 'agent') { setScreen('main'); setMode('agent'); }
    try {
      const resp = await createSession({ topic, difficulty, mode: m });
      setSessionId(resp.session_id); startPolling(resp.session_id);
    } catch (e) { setScreen('error'); setErrorMsg(String(e)); }
  }, [startPolling]);

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

  const handleStartLeetcode = useCallback(async (url: string) => {
    setScreen('loading'); setProgressMsgs(['正在解析 LeetCode 题目...']);
    setTabPanel({ ...DEFAULT_TAB_PANEL }); setActiveTabs({ left: 'desc', right: 'code' });
    editorInitialized.current = false;
    try {
      const r1 = await fetch(BASE + '/leetcode/parse', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
      if (!r1.ok) throw new Error('解析失败 (' + r1.status + ')');
      const parsed = await r1.json();
      const r2 = await fetch(BASE + '/session', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ topic: parsed.title || 'leetcode', difficulty: parsed.difficulty || 'medium', mode: 'practice', leetcode: parsed }) });
      if (!r2.ok) throw new Error('创建会话失败 (' + r2.status + ')');
      const resp = await r2.json(); setSessionId(resp.session_id); startPolling(resp.session_id);
    } catch (e) { setScreen('error'); setErrorMsg(e instanceof Error ? e.message : String(e)); }
  }, [startPolling]);

  // ── 提交 ──
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
      await readStream(sid, `我运行了代码，结果：${passed}/${total} 通过。\n\`\`\`\n${code.slice(0, 500)}\n\`\`\`\n请帮我分析一下。`, (token) => {
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
      for (let i = 0; i < 20; i++) {
        const s = await getState(sessionId);
        if (s.problem) {
          applySessionState(s, true);
          if (!s.problem?.starter_code && !editorInitialized.current) { setEditorCode('class Solution:\n    def solution(self):\n        pass\n'); editorInitialized.current = true; }
          setActiveTabs(prev => ({ ...prev, left: 'desc' })); setActiveTabs(prev => ({ ...prev, right: 'code' }));
          return;
        }
        await new Promise(r => setTimeout(r, 1500));
      }
    } catch (e) { console.error('Agent chat error:', e); }
  }, [sessionId, readStream]);

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

  // ── 重置 ──
  const handleNext = useCallback(() => {
    setScreen('welcome'); setSessionId(null); setProblem(null); setEditorCode('');
    setTutorMessages([]); setHintLevel(0); setLatestVerdict(null); setJudgeReport(null);
    setErrorMsg(''); setProgressMsgs([]); setRunResults(null); setSubmissions([]); setReferenceCode('');
    setMode('practice'); setTabPanel({ ...DEFAULT_TAB_PANEL }); setActiveTabs({ left: 'desc', right: 'code' });
    editorInitialized.current = false;
  }, []);

  const handleOpenAdmin = useCallback(() => setScreen('admin'), []);

  return {
    screen, mode, problem, editorCode, tutorMessages, hintLevel, latestVerdict,
    judgeReport, submissions, referenceCode, errorMsg, progressMsgs, runResults,
    running, submittingFlag, activeTabs, tabPanel, splitRatio, chatInput,
    sessionId, isDialogPhase: mode === 'agent' && !problem,
    isAC: latestVerdict === 'AC', isDone: latestVerdict === 'AC' && judgeReport !== null,
    dragging, dragTab, chatEndRef, editorInitialized,
    setEditorCode, setActiveTabs, setTabPanel, setSplitRatio, setChatInput,
    setTutorMessages, setRunResults, setProgressMsgs,
    onStart: handleStart, onStartExisting: handleStartExisting,
    onStartLeetcode: handleStartLeetcode, onSubmit: handleSubmit,
    onRun: handleRun, onChat: handleChat, onNext: handleNext,
    onOpenAdmin: handleOpenAdmin, onAgentSend: handleAgentSend,
    setScreen,
  };
}