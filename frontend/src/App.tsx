import { useEffect, useState } from 'react';
import LoadingScreen from './components/LoadingScreen';
import GuestWelcome from './components/GuestWelcome';
import WelcomeScreen from './components/WelcomeScreen';
import AdminPanel from './components/AdminPanel';
import SettingsPanel from './components/SettingsPanel';
import MainLayout, { type MainLayoutProps } from './components/MainLayout';
import { fetchMe, getStoredAuth, isAdmin, clearAuth, type AuthUser } from './api/auth';
import { useSession } from './hooks/useSession';
import { useErrorReport } from './hooks/useErrorReport';
import AnnouncementsBanner from './components/AnnouncementsBanner';

export default function App() {
  // 前端错误上报全局兜底（幂等安装，docs/monitoring-alerts-design.md §14.2）
  useErrorReport();

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
    // 访客主页（方案 B）：先看产品介绍，点「开始使用 / 登录」弹 AuthModal；
    // 登录/注册成功后整页刷新，确保 useSession 以新用户身份初始化 localStorage 草稿/会话
    return <GuestWelcome onLoggedIn={() => window.location.reload()} />;
  }

  if (screen === 'error') return <LoadingScreen progressMsgs={[]} errorMsg={errorMsg} onRetry={s.onBackToWelcome} />;
  if (screen === 'settings') return <SettingsPanel onClose={() => s.setScreen('welcome')} />;
  if (screen === 'welcome') return (
    <div className="flex min-h-screen flex-col bg-ct-bg">
      <AnnouncementsBanner />
      <div className="flex flex-1 items-center justify-center p-4">
        <WelcomeScreen onStart={s.onStart} onStartExisting={s.onStartExisting}
          onOpenAdmin={s.onOpenAdmin} onOpenSettings={s.onOpenSettings} onLogout={logout} user={user} />
      </div>
    </div>
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
    // 设置 / 退出登录只保留在主页（WelcomeScreen），做题界面不再传入
    analyzingTrace: s.analyzingTrace, onAnalyzeTrace: s.onAnalyzeTrace,
    traceFailed: s.traceFailed,
    traceAnalysis: s.traceAnalysis, traceMessages: s.traceMessages,
    traceAsking: s.traceAsking, traceInput: s.traceInput,
    onSetTraceInput: s.setTraceInput, onTraceAsk: s.onTraceAsk,
  };
  return (
    // 横幅 + 做题主界面共用一屏：横幅占自身高度，MainLayout 占满剩余空间，
    // 否则 h-screen(100vh) + 横幅会把底部「运行/提交」按钮栏顶出屏幕外
    <div className="flex h-screen flex-col overflow-hidden">
      <AnnouncementsBanner />
      <div className="min-h-0 flex-1">
        <MainLayout {...mainProps} />
      </div>
    </div>
  );
}
