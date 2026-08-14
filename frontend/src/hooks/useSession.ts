/** 封装会话全部状态与回调，让 App.tsx 只管路由 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { createSession, getState, runCode, submitCode, getReferenceCode } from '../api/session';
import type { Message, ProblemMeta, RunResult, SessionStateResp, Submission } from '../types/session';
import type { JudgeReport } from '../types/judge';
import { useSSE } from './useSSE';
import { useProgressSSE } from './useProgressSSE';
import { API_BASE } from '../api/config';
import { useEditTrace } from './useEditTrace';

const BASE = API_BASE;
export type Screen = 'welcome' | 'loading' | 'main' | 'error' | 'admin';
export type TabId = 'desc' | 'history' | 'reference' | 'code' | 'run' | 'tutor' | 'agent-history';

export const DEFAULT_TAB_PANEL: Record<TabId, 'left' | 'right'> = {
  desc: 'left', history: 'left', reference: 'left',
  code: 'right', run: 'right', tutor: 'right', 'agent-history': 'left',
};

// ── 会话恢复（根治“自动回主页”bug）──
// 把做题页的关键状态落盘到 localStorage，整页刷新 / Vite HMR 重挂载后自动恢复，
// 避免纯内存 screen 被重置回 welcome。只持久化 main 屏，loading/error/admin 不持久化。
const RESTORE_KEY = 'code-tutor:session';
interface PersistedSession { screen: Screen; sessionId: string | null; mode: string; }
function loadPersisted(): PersistedSession | null {
  try {
    const raw = localStorage.getItem(RESTORE_KEY);
    if (!raw) return null;
    const p = JSON.parse(raw) as PersistedSession;
    if (p && typeof p.screen === 'string' && 'sessionId' in p) return p;
  } catch { /* ignore */ }
  return null;
}
function savePersisted(p: PersistedSession) {
  try { localStorage.setItem(RESTORE_KEY, JSON.stringify(p)); } catch { /* ignore */ }
}
function clearPersisted() { try { localStorage.removeItem(RESTORE_KEY); } catch { /* ignore */ } }

// ── 编辑器草稿缓存（根治“刷新吞代码”）──
// 按 sessionId 维度把用户当前编辑器内容落盘，刷新重挂载后用草稿回填编辑器，
// 避免只用题目模板 starter_code 覆盖用户未提交的代码。
const DRAFT_PREFIX = 'code-tutor:draft:';
function loadDraft(sid: string): string | null {
  try {
    const raw = localStorage.getItem(DRAFT_PREFIX + sid);
    return raw === null ? null : raw;
  } catch { return null; }
}
function saveDraft(sid: string, code: string) {
  try { localStorage.setItem(DRAFT_PREFIX + sid, code); } catch { /* ignore */ }
}
function clearDraft(sid: string | null) {
  if (!sid) return;
  try { localStorage.removeItem(DRAFT_PREFIX + sid); } catch { /* ignore */ }
}
// 页面生命周期内只读取一次 localStorage（模块级缓存，避免每次渲染都读）
let _cachedPersist: PersistedSession | null | undefined;
function getInitialPersist(): PersistedSession | null {
  if (_cachedPersist === undefined) _cachedPersist = loadPersisted();
  return _cachedPersist;
}

export function useSession() {
  const initial = getInitialPersist();
  // 只恢复 main 屏（做题页）；loading/error/admin 刷新后回 welcome 更安全
  const [screen, setScreen] = useState<Screen>(
    initial?.screen === 'main' && initial?.sessionId ? 'main' : 'welcome',
  );

  const [sessionId, setSessionId] = useState<string | null>(initial?.sessionId ?? null);
  // 编辑轨迹采集（仅前端采集 + 落本地文件，不做后端处理）
  const editTrace = useEditTrace(sessionId);
  const [mode, setMode] = useState<string>(initial?.mode || 'practice');

  // 持久化：main 屏且有 sessionId 时落盘；其余情况（welcome/loading/error/admin）清掉，
  // 保证刷新后只恢复到做题页，不会卡在 loading/error 等中间态。
  useEffect(() => {
    if (screen === 'main' && sessionId) savePersisted({ screen, sessionId, mode });
    else clearPersisted();
  }, [screen, sessionId, mode]);

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
  // 编辑器初始化：优先用持久化草稿，否则题目模板，再兜底默认模板。
  // editorInitialized 守卫保证每个会话只初始化一次，避免重复覆盖用户代码。
  const initEditor = useCallback((problem: ProblemMeta | null, sid: string | null) => {
    if (editorInitialized.current || !problem) return;
    const starter = problem.starter_code || '';
    const draft = sid ? loadDraft(sid) : null;
    if (draft !== null) setEditorCode(draft);
    else if (starter) setEditorCode(starter);
    else setEditorCode('class Solution:\n    def solution(self):\n        pass\n');
    editorInitialized.current = true;
  }, []);

  function applySessionState(resp: SessionStateResp, fillEditor = false) {
    setSessionId(resp.session_id); setProblem(resp.problem);
    if (resp.mode) setMode(resp.mode);
    setTutorMessages(resp.tutor_messages); setHintLevel(resp.hint_level);
    setLatestVerdict(resp.last_verdict);
    if ((resp as any).phase) setPhase((resp as any).phase);
    setJudgeReport(resp.last_review_payload as JudgeReport | null);
    setSubmissions((resp.submissions || []) as Submission[]);
    if ((resp as any).last_run_results) setRunResults((resp as any).last_run_results);
    if (fillEditor && resp.problem) initEditor(resp.problem, resp.session_id);
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

  // 草稿自动持久化：编辑器初始化完成后，用户每次改动防抖写入（按 sessionId）。
  // 初始化阶段（editorInitialized=false）不写，避免把空串/模板覆盖真实草稿。
  useEffect(() => {
    if (!editorInitialized.current || !sessionId) return;
    const t = setTimeout(() => saveDraft(sessionId, editorCode), 300);
    return () => clearTimeout(t);
  }, [editorCode, sessionId]);

  // 重挂载恢复：若初始即 main（来自持久化），重新拉取会话状态，避免刷新后“空做题页”。
  // 题目仍在生成则继续监听进度；否则已可在做题页等待提交。
  const didRestore = useRef(false);
  useEffect(() => {
    if (didRestore.current) return;
    const restoredScreen = initial?.screen;
    const restoredSessionId = initial?.sessionId;
    if (restoredScreen !== 'main' || !restoredSessionId) return;
    didRestore.current = true;
    (async () => {
      try {
        const state = await getState(restoredSessionId);
        applySessionState(state, true);
        if (!state.problem) startProgress(restoredSessionId);
      } catch {
        clearPersisted();
        setScreen('welcome');
      }
    })();
    // 仅挂载时执行一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    editTrace.mark('submit'); // 提交锚点：强制记一条当前代码快照并落盘
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
    editTrace.mark('run'); // 运行锚点：强制记一条当前代码快照
    setRunning(true); setRunResults(null); setActiveTabs(prev => ({ ...prev, right: 'run' }));
    try {
      const resp = await runCode(sid, code);
      setRunResults(resp.results);
      // 运行 = 快速自测：仅展示 Judge0 执行结果，不触发 LLM 导师评价。
      // 同时把运行结果摘要拼接进对话（纯本地、不调 LLM、不会误判 AC），
      // 恢复「点运行后对话里直接看到结果」的体感；详细逐用例在「运行结果」标签。
      if (resp.results && resp.results.length > 0) {
        const pass = resp.results.filter(r => r.passed).length;
        const total = resp.results.length;
        let summary: string;
        if (pass === total) {
          summary = `✅ **运行结果**：样例 ${pass}/${total} 全部通过！点「提交」跑完整用例试试吧。`;
          // 运行全过 = 本地自测通过，清掉上次提交遗留的非 AC 判题短报（如 WA 的
          // 「答案不对，再检查一下逻辑」），避免与「✅ 全部通过」同时出现造成误导。
          setLatestVerdict(null);
        } else {
          const fails = resp.results
            .filter(r => !r.passed)
            .map(r => `• 用例#${r.test_case_id}（${r.status}）输入: ${r.input_args?.join(' ') || '—'} → 期望 ${r.expected || '—'}`)
            .join('\n');
          summary = `⚠️ **运行结果**：样例 ${pass}/${total} 通过。\n${fails}`;
        }
        setTutorMessages(prev => [...prev, { role: 'tutor', content: summary }]);
        setActiveTabs(prev => ({ ...prev, right: 'tutor' }));
      }
    } catch (e) { setErrorMsg(String(e)); }
    finally { setRunning(false); }
  }, [sessionId, editorCode, running, setTutorMessages]);

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

    // 「放弃 / 下一题 / 继续出题」：重入导师对话（保留历史、隐藏题目/代码栏，不出新题）。
    // 2026-08-04：题库（practice）入口也统一走对话式选题，切到 agent 模式，
    // 否则 isDialogPhase（依赖 mode==='agent'）为 false，对话界面渲染不出来。
    const applyAgentReenter = (data: any) => {
      setMode('agent');
      setProblem(null);
      setPhase('dialog');
      setEditorCode('');
      clearDraft(sessionId);
      editorInitialized.current = false;
      if (data?.tutor_messages?.length) setTutorMessages(data.tutor_messages as Message[]);
      setHintLevel(0); setLatestVerdict(null); setJudgeReport(null);
      setRunResults(null); setSubmissions([]);
      setProgressMsgs([]);
      // 左栏对话，右栏显示空代码编辑器（占位，不让右侧空着或重复导师面板）
      setEditorCode('');
      setActiveTabs({ left: 'agent-history', right: 'code' });
    };

    // 2026-08-04：换题统一走「对话式选题」——先与导师沟通再定题，不再直接出题。
    const callNextProblem = async (preference: string = 'continue_dialog') => {
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
          clearDraft(sessionId);
          editorInitialized.current = false;
          initEditor(data.problem as ProblemMeta, sessionId);
          // Agent 模式保留后端返回的 tutor_messages，不清空
          if (mode === 'agent' && data.tutor_messages?.length) {
            setTutorMessages(data.tutor_messages as Message[]);
          } else {
            setTutorMessages([]);
          }
          setHintLevel(0); setLatestVerdict(null); setJudgeReport(null);
          setRunResults(null); setSubmissions([]);
          setProgressMsgs([]);
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

    // AC → 已完成：统一重入出题对话（先沟通后出题），不再按模式直接出题
    if (latestVerdict === 'AC' && sessionId) {
      await callNextProblem('continue_dialog');
      return;
    }

    // solving → 放弃确认
    if (phase === 'solving' && sessionId) {
      const ok = window.confirm('当前代码还没提交，确定放弃这题去下一题？');
      if (ok) { await callNextProblem('continue_dialog'); }
      return;
    }

    // 非 AC 且已提交（如 WA 后后端置 phase=done）→ 放弃本题、重入对话选下一题，
    // 不要回主页。原默认分支会直接 setScreen('welcome')，导致「换一题」把用户踢回首页
    // 且全程无后端请求（日志无变化）。
    if (sessionId && phase !== 'dialog') {
      await callNextProblem('continue_dialog');
      return;
    }

    // 其余（dialog 阶段的「← 返回」等）→ 回主页
    clearDraft(sessionId);
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
    clearDraft(sessionId);
    closeProgress();
    setScreen('welcome'); setSessionId(null); setProblem(null); setEditorCode('');
    setTutorMessages([]); setHintLevel(0); setLatestVerdict(null); setJudgeReport(null);
    setErrorMsg(''); setProgressMsgs([]); setRunResults(null); setSubmissions([]); setReferenceCode('');
    setMode('practice'); setTabPanel({ ...DEFAULT_TAB_PANEL }); setActiveTabs({ left: 'desc', right: 'code' });
    editorInitialized.current = false;
    setPhase('solving'); setStatus(''); setNextProblemLoading(false);
  }, [closeProgress, sessionId]);

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
    flushEditTrace: editTrace.flush,
    markEditTrace: editTrace.mark,
    onRun: handleRun, onChat: handleChat, onNext: handleNext,
    onBackToWelcome: handleBackToWelcome,
    onOpenAdmin: handleOpenAdmin, onAgentSend: handleAgentSend,
    setScreen,
  };
}