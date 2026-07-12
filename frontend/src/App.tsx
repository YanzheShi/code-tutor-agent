import LoadingScreen from './components/LoadingScreen';
import WelcomeScreen from './components/WelcomeScreen';
import AdminPanel from './components/AdminPanel';
import MainLayout, { type MainLayoutProps } from './components/MainLayout';
import { useSession } from './hooks/useSession';

export default function App() {
  const s = useSession();
  const { screen, errorMsg, progressMsgs } = s;

  if (screen === 'error') return <LoadingScreen progressMsgs={[]} errorMsg={errorMsg} onRetry={s.onNext} />;
  if (screen === 'welcome') return (
    <WelcomeScreen onStart={s.onStart} onStartExisting={s.onStartExisting}
      onStartLeetcode={s.onStartLeetcode} onOpenAdmin={s.onOpenAdmin} />
  );
  if (screen === 'loading') return <LoadingScreen progressMsgs={progressMsgs} onRetry={s.onNext} />;
  if (screen === 'admin') return <AdminPanel onClose={() => s.setScreen('welcome')} />;

  const mainProps: MainLayoutProps = {
    problem: s.problem, mode: s.mode, phase: (s as any).phase || 'solving',
    activeTabs: s.activeTabs, tabPanel: s.tabPanel, splitRatio: s.splitRatio,
    tutorMessages: s.tutorMessages, chatInput: s.chatInput,
    editorCode: s.editorCode, hintLevel: s.hintLevel,
    latestVerdict: s.latestVerdict, judgeReport: s.judgeReport,
    referenceCode: s.referenceCode,
    runResults: s.runResults, progressMsgs: s.progressMsgs,
    running: s.running, submittingFlag: s.submittingFlag,
    isDialogPhase: s.isDialogPhase, isDone: s.isDone,
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
    onNext: s.onNext, onAgentSend: s.onAgentSend,
  };
  return <MainLayout {...mainProps} />;
}