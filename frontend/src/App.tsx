import LoadingScreen from './components/LoadingScreen';
import WelcomeScreen from './components/WelcomeScreen';
import AdminPanel from './components/AdminPanel';
import MainLayout, { type MainLayoutProps } from './components/MainLayout';
import { useSession } from './hooks/useSession';
import { useTheme } from './hooks/useTheme';

function ThemeToggle() {
  const { theme, toggle } = useTheme();
  return (
    <button
      onClick={toggle}
      className="fixed top-3 right-3 z-50 rounded-full border border-ct-border bg-ct-panel/90 p-2 text-sm shadow-sm backdrop-blur-sm transition hover:opacity-80"
      title={theme === 'dark' ? '切换浅色主题' : '切换深色主题'}
    >
      {theme === 'dark' ? '☀️' : '🌙'}
    </button>
  );
}

export default function App() {
  const s = useSession();
  const { screen, errorMsg, progressMsgs } = s;

  if (screen === 'error') return <LoadingScreen progressMsgs={[]} errorMsg={errorMsg} onRetry={s.onBackToWelcome} />;
  if (screen === 'welcome') return (
    <><ThemeToggle /><WelcomeScreen onStart={s.onStart} onStartExisting={s.onStartExisting}
      onOpenAdmin={s.onOpenAdmin} /></>
  );
  if (screen === 'loading') return <LoadingScreen progressMsgs={progressMsgs} onRetry={s.onBackToWelcome} />;
  if (screen === 'admin') return <AdminPanel onClose={() => s.setScreen('welcome')} />;

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
    analyzingTrace: s.analyzingTrace, onAnalyzeTrace: s.onAnalyzeTrace,
  };
  return <MainLayout {...mainProps} />;
}
