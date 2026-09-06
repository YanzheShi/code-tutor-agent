import { useEffect, useState } from 'react';
import LoadingScreen from './components/LoadingScreen';
import LoginScreen from './components/LoginScreen';
import WelcomeScreen from './components/WelcomeScreen';
import AdminPanel from './components/AdminPanel';
import SettingsPanel from './components/SettingsPanel';
import MainLayout, { type MainLayoutProps } from './components/MainLayout';
import { fetchMe, getStoredAuth, isAdmin, clearAuth, type AuthUser } from './api/auth';
import { useSession } from './hooks/useSession';

export default function App() {
  // ── 登录门禁（多用户改造 P4）：本地有凭证则后台校验 token，无凭证直接进登录页 ──
  const [user, setUser] = useState<AuthUser | null>(() => getStoredAuth()?.user ?? null);
  const [authChecked, setAuthChecked] = useState(() => !getStoredAuth());
  useEffect(() => {
    let cancelled = false;
    fetchMe().then((u) => {
      if (!cancelled) {
        setUser(u);
        setAuthChecked(true);
      }
    });
    return () => { cancelled = true; };
  }, []);

  // ── 退出登录：JWT 无状态，前端清掉 localStorage 凭证即视为登出（无需后端接口）──
  const logout = () => {
    clearAuth();
    setUser(null);
    // 整页重载回到登录页，并清空内存态（useSession 会以未登录身份重新初始化）
    window.location.reload();
  };

  const s = useSession();
  const { screen, errorMsg, progressMsgs } = s;

  if (!user) {
    if (!authChecked) return null; // 校验中，闪一下即过
    return (
      <LoginScreen
        onLoggedIn={() => {
          setUser(getStoredAuth()?.user ?? null);
          // 登录后整页重载，确保 useSession 以新用户身份初始化 localStorage 草稿/会话
          window.location.reload();
        }}
      />
    );
  }

  if (screen === 'error') return <LoadingScreen progressMsgs={[]} errorMsg={errorMsg} onRetry={s.onBackToWelcome} />;
  if (screen === 'settings') return <SettingsPanel onClose={() => s.setScreen('welcome')} />;
  if (screen === 'welcome') return (
    <WelcomeScreen onStart={s.onStart} onStartExisting={s.onStartExisting}
      onOpenAdmin={s.onOpenAdmin} onOpenSettings={s.onOpenSettings} onLogout={logout} user={user} />
  );
  if (screen === 'loading') return <LoadingScreen progressMsgs={progressMsgs} onRetry={s.onBackToWelcome} />;
  if (screen === 'admin') {
    // admin 入口仅 admin 角色可进（非 admin 误入时回落 welcome）
    if (!isAdmin()) {
      return (
        <WelcomeScreen onStart={s.onStart} onStartExisting={s.onStartExisting}
          onOpenAdmin={s.onOpenAdmin} onOpenSettings={s.onOpenSettings} onLogout={logout} user={user} />
      );
    }
    return <AdminPanel onClose={() => s.setScreen('welcome')} />;
  }

  const mainProps: MainLayoutProps = {
    problem: s.problem, mode: s.mode, phase: (s as any).phase || 'solving',
    nextProblemLoading: (s as any).nextProblemLoading || false,
    activeTabs: s.activeTabs, tabPanel: s.tabPanel, splitRatio: s.splitRatio,
    tutorMessages: s.tutorMessages, chatInput: s.chatInput,
    editorCode: s.editorCode, hintLevel: s.hintLevel,
    latestVerdict: s.latestVerdict, judgeReport: s.judgeReport,
    referenceCode: s.referenceCode,
    submissions: s.submissions,
    runResults: s.runResults, progressMsgs: s.progressMsgs,
    running: s.running, submittingFlag: s.submittingFlag,
    isDialogPhase: s.isDialogPhase, isDone: s.isDone, isGenerating: s.isGenerating,
    dragging: s.dragging, dragTab: s.dragTab, chatEndRef: s.chatEndRef,
    editorInitialized: s.editorInitialized,
    onSetChatInput: s.setChatInput,
    onSetActiveTabs: s.setActiveTabs,
    onSetTabPanel: s.setTabPanel,
    onSetSplitRatio: s.setSplitRatio,
    onSetEditorCode: s.setEditorCode,
    onSetTutorMessages: s.setTutorMessages,
    onSetRunResults: s.setRunResults,
    onSetProgressMsgs: s.setProgressMsgs,
    onRun: s.onRun, onSubmit: s.onSubmit, onChat: s.onChat,
        onNext: s.onNext, onBackToWelcome: (s as any).onBackToWelcome || (() => {}), onAgentSend: s.onAgentSend,
    onLogout: logout,
    onOpenSettings: () => s.setScreen('settings'),
    analyzingTrace: s.analyzingTrace, onAnalyzeTrace: s.onAnalyzeTrace,
    traceFailed: s.traceFailed,
    traceAnalysis: s.traceAnalysis, traceMessages: s.traceMessages,
    traceAsking: s.traceAsking, traceInput: s.traceInput,
    onSetTraceInput: s.setTraceInput, onTraceAsk: s.onTraceAsk,
  };
  return <MainLayout {...mainProps} />;
}
