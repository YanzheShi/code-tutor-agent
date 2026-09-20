/**
 * Beta 阶段标识角标。
 * 深浅主题兼容（沿用 ct-* token + 透明度修饰），带悬停说明，
 * 复用点：主页 Logo 旁（WelcomeScreen）、做题界面返回按钮旁（MainLayout）。
 */
export default function BetaBadge() {
  return (
    <span
      className="inline-flex shrink-0 items-center rounded-full border border-ct-accent/40 bg-ct-accent/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-ct-accent"
      title="CodeTutor Agent 目前处于 Beta 阶段：功能持续迭代，可能偶有变动或数据重置"
    >
      Beta
    </span>
  );
}
