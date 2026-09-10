import { useState } from 'react';
import AuthModal from './AuthModal';
import { createTrialUser } from '../api/auth';

/** 访客主页（方案 B 访客改造）：未登录用户的第一屏。
 *
 * 取代原「整页登录墙」：先展示产品价值（特性 + 题库预告），用户点
 * 「开始使用 / 登录」才弹 AuthModal；登录/注册成功后由 App 整页刷新进入正常流程。
 * 纯静态展示，不调用任何业务 API（题库接口仍需登录，这里只做主题预告）。
 */

const FEATURES = [
  {
    icon: '题',
    title: 'AI 对话式出题',
    desc: '跟导师聊聊想练什么，按你的水平动态调整难度与方向',
  },
  {
    icon: '判',
    title: '内置 OJ 即时判题',
    desc: '样例快速运行 + 全量提交双通道，秒级反馈对错',
  },
  {
    icon: '析',
    title: '做题轨迹分析',
    desc: '复盘解题过程，暴露薄弱点，给出面试备考建议',
  },
  {
    icon: '像',
    title: '个人能力画像',
    desc: '六大维度错误模式追踪，弱在哪、练哪里一目了然',
  },
] as const;

// 题库主题预告（与后端出题选择器的主题方向一致，静态展示、不拉接口）
const TOPIC_TEASERS = [
  '数组与双指针', '链表', '栈与队列', '哈希表', '二叉树', '堆',
  '回溯', '动态规划', '贪心', '图论',
] as const;

export default function GuestWelcome({ onLoggedIn }: { onLoggedIn: () => void }) {
  const [authOpen, setAuthOpen] = useState(false);
  // 一键免注册体验：创建测试用户直接进主页（2026-09-10 测试用户体系）
  const [trialBusy, setTrialBusy] = useState(false);
  const [trialError, setTrialError] = useState('');

  const handleTrial = async () => {
    if (trialBusy) return;
    setTrialBusy(true);
    setTrialError('');
    try {
      await createTrialUser();
      onLoggedIn(); // App 整页刷新，useSession 以体验账号身份初始化
    } catch (err) {
      setTrialError(err instanceof Error ? err.message : '体验创建失败，请重试');
      setTrialBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen flex-col bg-ct-bg text-ct-text">
      {/* 顶栏 */}
      <header className="border-b border-ct-border/60">
        <div className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-ct-accent text-sm font-medium text-white">
              CT
            </div>
            <span className="text-base font-medium">Code Tutor · 编程私教</span>
          </div>
          <button
            type="button"
            onClick={() => setAuthOpen(true)}
            className="rounded-lg border border-ct-border bg-ct-panel px-4 py-1.5 text-sm text-ct-text transition hover:bg-ct-hover"
          >
            登录
          </button>
        </div>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-6">
        {/* Hero */}
        <section className="pb-12 pt-16 text-center">
          <h1 className="text-3xl font-medium leading-snug">
            你的 AI 编程私教
          </h1>
          <p className="mx-auto mt-4 max-w-xl text-base leading-relaxed text-ct-muted">
            从出题、写码、判题到复盘的完整刷题闭环——
            导师陪你选题、判题给反馈、分析你的解题轨迹，把零散刷题变成有针对性的学习和提升。
          </p>
          <div className="mt-8 flex flex-col items-center justify-center gap-3">
            <div className="flex items-center justify-center gap-3">
              <button
                type="button"
                onClick={handleTrial}
                disabled={trialBusy}
                className="rounded-lg bg-ct-accent px-8 py-3 text-base font-medium text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {trialBusy ? '正在进入…' : '免注册直接体验'}
              </button>
              <button
                type="button"
                onClick={() => setAuthOpen(true)}
                className="rounded-lg border border-ct-border bg-ct-panel px-8 py-3 text-base font-medium text-ct-text transition hover:bg-ct-hover"
              >
                开始使用
              </button>
            </div>
            {trialError && (
              <p className="text-sm text-red-500">{trialError}</p>
            )}
            <p className="text-xs text-ct-muted">
              体验账号与正式账号做题额度相同，注册后记录完整保留
            </p>
          </div>
        </section>

        {/* 特性卡片 */}
        <section className="grid grid-cols-1 gap-4 pb-12 sm:grid-cols-2 lg:grid-cols-4">
          {FEATURES.map((f) => (
            <div key={f.title} className="rounded-xl border border-ct-border bg-ct-panel p-5">
              <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-ct-info-bg text-sm font-medium text-ct-accent">
                {f.icon}
              </div>
              <h3 className="mt-3 text-sm font-medium text-ct-text">{f.title}</h3>
              <p className="mt-1.5 text-xs leading-relaxed text-ct-muted">{f.desc}</p>
            </div>
          ))}
        </section>

        {/* 题库预告 */}
        <section className="pb-16">
          <div className="rounded-2xl border border-ct-border bg-ct-panel p-6 text-center">
            <h2 className="text-base font-medium">题库覆盖这些方向</h2>
            <p className="mt-1 text-sm text-ct-muted">登录后解锁完整题库与你的个人进度</p>
            <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
              {TOPIC_TEASERS.map((t) => (
                <span
                  key={t}
                  className="rounded-full border border-ct-border bg-ct-surface px-3 py-1 text-xs text-ct-muted"
                >
                  {t}
                </span>
              ))}
            </div>
          </div>
        </section>
      </main>

      {/* 页脚 */}
      <footer className="border-t border-ct-border/60 py-5">
        <div className="mx-auto w-full max-w-5xl px-6 text-center text-xs text-ct-muted">
          Code Tutor Agent · 开源于{' '}
          <a
            href="https://github.com/YanzheShi/code-tutor-agent"
            target="_blank"
            rel="noreferrer"
            className="text-ct-accent hover:underline"
          >
            GitHub
          </a>
        </div>
      </footer>

      <AuthModal open={authOpen} onClose={() => setAuthOpen(false)} onLoggedIn={onLoggedIn} />
    </div>
  );
}
