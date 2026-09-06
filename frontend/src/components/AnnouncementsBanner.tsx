/**
 * 主页系统通知横幅（docs/monitoring-alerts-design.md §15.3）。
 *
 * 数据源：GET /announcements（双来源：admin 手动 + monitor 自动降级通告）。
 * 行为：
 * - 进入主界面拉取一次，停留期间每 60s 轮询；
 * - 多条并存只显示最高级别的一条（critical > warning > info）；
 * - monitor 来源的 critical 横幅不可关闭（系统降级必须让用户看到），
 *   其余可关闭，关闭状态存 localStorage（按公告 id）。
 * 拉取失败静默——横幅绝不能影响做题主流程。
 */
import { useCallback, useEffect, useState } from 'react';
import { API_BASE } from '../api/config';
import { getStoredAuth } from '../api/auth';

type Level = 'info' | 'warning' | 'critical';

interface Announcement {
  id: number;
  level: Level;
  title: string;
  content: string;
  source: 'admin' | 'monitor' | string;
}

const LEVEL_ORDER: Record<Level, number> = { critical: 3, warning: 2, info: 1 };
const POLL_MS = 60_000;
const CLOSED_KEY = (uid: string | number) => `code-tutor:closed-announcements:${uid}`;

function readClosed(): number[] {
  try {
    const uid = getStoredAuth()?.user?.id ?? 0;
    return JSON.parse(localStorage.getItem(CLOSED_KEY(uid)) || '[]') as number[];
  } catch {
    return [];
  }
}

function dismiss(id: number): void {
  try {
    const uid = getStoredAuth()?.user?.id ?? 0;
    const closed = readClosed().filter((x) => x !== id);
    closed.push(id);
    localStorage.setItem(CLOSED_KEY(uid), JSON.stringify(closed.slice(-50)));
  } catch {
    /* 忽略 */
  }
  window.dispatchEvent(new Event('announcements-changed'));
}

const LEVEL_STYLE: Record<Level, string> = {
  critical: 'bg-ct-error-bg text-ct-error border-ct-error/30',
  warning: 'bg-ct-warn-bg text-ct-warn border-ct-warn/30',
  info: 'bg-ct-info-bg text-ct-info border-ct-info/30',
};

export default function AnnouncementsBanner() {
  const [items, setItems] = useState<Announcement[]>([]);
  const [closedIds, setClosedIds] = useState<number[]>(() => readClosed());

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/announcements`, {
        headers: { Authorization: `Bearer ${getStoredAuth()?.token ?? ''}` },
      });
      if (!r.ok) return;
      const data = await r.json();
      setItems(Array.isArray(data?.announcements) ? data.announcements : []);
    } catch {
      /* 静默 */
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    const onClosedChange = () => setClosedIds(readClosed());
    window.addEventListener('announcements-changed', onClosedChange);
    return () => {
      clearInterval(timer);
      window.removeEventListener('announcements-changed', onClosedChange);
    };
  }, [load]);

  const visible = items
    .filter((a) => !closedIds.includes(a.id))
    .sort((a, b) => LEVEL_ORDER[b.level] - LEVEL_ORDER[a.level] || b.id - a.id);
  const top = visible[0];
  if (!top) return null;

  const undismissible = top.source === 'monitor' && top.level === 'critical';

  return (
    <div
      data-testid="announcements-banner"
      className={`flex w-full items-start gap-2 border-b px-4 py-2 text-[13px] leading-relaxed ${LEVEL_STYLE[top.level] ?? LEVEL_STYLE.info}`}
    >
      <span className="mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium"
        style={{ background: 'rgba(0,0,0,0.06)' }}>
        {top.level === 'critical' ? '紧急' : top.level === 'warning' ? '注意' : '通知'}
      </span>
      <div className="min-w-0 flex-1">
        <span className="font-medium">{top.title}</span>
        {top.content && <span className="ml-2 opacity-90">{top.content}</span>}
      </div>
      {!undismissible && (
        <button
          type="button"
          aria-label="关闭公告"
          className="shrink-0 rounded px-1 opacity-60 hover:opacity-100"
          onClick={() => dismiss(top.id)}
        >
          ✕
        </button>
      )}
    </div>
  );
}
