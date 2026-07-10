export function TabButton({ label, tabId, active, onClick, onDragStart }: {
  label: string;
  tabId?: string;
  active: boolean;
  onClick: () => void;
  onDragStart?: (e: React.DragEvent) => void;
}) {
  return (
    <button
      draggable={!!tabId}
      onDragStart={onDragStart}
      onClick={onClick}
      className={'relative px-4 py-2 font-medium transition-colors cursor-grab active:cursor-grabbing shrink-0 ' + (active ? 'border-b-2 border-ct-accent text-ct-text' : 'text-ct-muted hover:text-ct-text')}
    >
      {label}
    </button>
  );
}

export function VerdictBadge({ verdict }: { verdict: string }) {
  const colors: Record<string, string> = { AC: 'text-ct-success', WA: 'text-ct-warn', TLE: 'text-ct-error', RE: 'text-ct-error' };
  return <span className={'font-bold ' + (colors[verdict] ?? 'text-ct-muted')}>{verdict}</span>;
}