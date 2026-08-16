/**
 * useEditTrace —— 前端代码编辑轨迹采集（仅采集 + 传输，不做后端处理）。
 *
 * 事件类型与触发：
 *  - edit  ：行为触发（Monaco onDidChangeModelContent）。按"字符改动量 / 行改动量"累积，
 *           跨过阈值（DELTA_THRESHOLD 字符 或 LINE_THRESHOLD 行）才记一条，带全量代码 + change 语义。
 *           既保留"写循环又删"的过程，又把事件数压到合理量级。
 *  - idle  ：纯停顿标记（A/B/D）。用户停手 >= IDLE_MS 即视为一次停顿；idle 事件只在
 *           "停顿结束（下次敲键 / 提交 / 卸载）"时发出一条，idleMs = 真实停顿时长。
 *           idle 不再清零 delta、不再背靠背 flood、不再存代码——只存 idleMs。
 *  - run/submit ：行为锚点（mark 强制记一条并立即落盘）。submit 同时把本会话置为终态，
 *           其后产生的编辑/停顿一律丢弃（C：杜绝"提交后编辑器清空被记成空编辑"的脏数据）。
 *
 * 关键纪律（避免数据膨胀 / 信号失真）：
 *  - edit 与 idle 解耦：idle 不碰 pendingDelta，停顿只作"停顿标记"，不吞噬编辑（A）。
 *  - flush 不造 edit：每 2s 只发送已产生的事件，零星改动由 capturePending 在
 *    停顿结束 / mark / 卸载前补记一条（F 细阈值下 edit 变多，但仍是实质改动）。
 *  - 阈值双判：字符 >=50 或 行 >=3 任一满足即记一条 edit（F）。
 *
 * 体量估算（50 字符 / 3 行阈值，30 分钟做题）：连续写 ~3000 字符 → ~30-60 条 edit；
 * 停顿若干 → 每条真实停顿一条 idle；总计通常 < 150 条、< 300KB。
 *
 * 注意：验证采集质量的 spike，不做任何 LLM / 画像处理。
 */
import { useCallback, useEffect, useRef } from 'react';

const DELTA_THRESHOLD = 50; // 自上次记录起累计改动字符数达到此值即记一条 edit（调细可改 30）
const LINE_THRESHOLD = 3; // 或累计改动行数达到此值即记一条 edit（与字符阈值取"或"）
const IDLE_MS = 4000; // 停手超过该时长视为一次"停顿"（卡壳起点）
const FLUSH_INTERVAL_MS = 2000; // 批量上报间隔（仅影响落盘及时性，不影响事件数）

export type TraceChange = 'insert' | 'delete' | 'replace' | null;

export interface TraceEvent {
  ts: number; // epoch ms
  type: 'edit' | 'idle' | 'run' | 'submit';
  code?: string; // 全量代码快照（idle 不带，仅 edit/run/submit 带）
  cursor?: { line: number; col: number } | null; // 光标（idle 不带）
  change?: TraceChange; // edit 专用：增/删/替
  idleMs?: number; // idle 专用：真实停顿时长（ms）
}

// 结构化最小接口，避免直接依赖 monaco-editor 类型包
interface MiniEditor {
  getValue(): string;
  getPosition(): { lineNumber: number; column: number } | null;
  // 回调拿到 Monaco 的 IModelContentChangedEvent（用 any 避开 monaco 类型依赖），
  // 其中 e.changes[].rangeLength / .text / .range 用于计算"真实改动字符/行数"。
  onDidChangeModelContent(cb: (e: any) => void): { dispose(): void };
}

function getCursor(ed: MiniEditor): { line: number; col: number } | null {
  const p = ed.getPosition();
  return p ? { line: p.lineNumber, col: p.column } : null;
}

function diffChange(curLen: number, snapLen: number): TraceChange {
  if (curLen > snapLen) return 'insert';
  if (curLen < snapLen) return 'delete';
  return 'replace';
}

export function useEditTrace(sessionId: string | null) {
  const pending = useRef<TraceEvent[]>([]);
  const lastCode = useRef<string>(''); // 上一次按键后的代码（用于算 delta 兜底）
  const lastSnapLen = useRef<number>(0); // 上一次落快照时的代码长度（用于算 change 类型）
  const pendingDelta = useRef<number>(0); // 自上次快照累计的字符改动量
  const pendingLineDelta = useRef<number>(0); // 自上次快照累计的改动行数
  const lastActivity = useRef<number>(Date.now()); // 最后一次敲键时间
  const pauseStart = useRef<number | null>(null); // 当前停顿起点（null=活动中）；idle 真实时长基准
  const terminal = useRef<boolean>(false); // 提交后冻结：其后编辑/停顿一律丢弃（C）
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flushTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // edit / run / submit 落一条带全量代码的快照
  const pushSnapshot = useCallback(
    (ed: MiniEditor, type: 'edit' | 'run' | 'submit', change?: TraceChange) => {
      const code = ed.getValue();
      pending.current.push({
        ts: Date.now(),
        type,
        code,
        cursor: getCursor(ed),
        change,
      });
      lastSnapLen.current = code.length;
      lastCode.current = code;
    },
    [],
  );

  // idle 只落一条"纯停顿"事件（只带真实 idleMs，不带代码/光标）——B/D
  const pushIdle = useCallback((idleMs: number) => {
    pending.current.push({ ts: Date.now(), type: 'idle', idleMs });
  }, []);

  // 把累计未记录的零星改动补记一条 edit（停顿结束 / mark / 卸载前调用）
  const capturePending = useCallback(
    (ed: MiniEditor) => {
      if (pendingDelta.current <= 0 && pendingLineDelta.current <= 0) return;
      pushSnapshot(ed, 'edit', diffChange(ed.getValue().length, lastSnapLen.current));
      pendingDelta.current = 0;
      pendingLineDelta.current = 0;
    },
    [pushSnapshot],
  );

  // 仅把"已产生"的事件发出去。flush 不造 edit（避免 2s 轮询把小改动强行记成 edit）。
  const flush = useCallback(() => {
    if (!sessionId) return;
    if (pending.current.length === 0) return;
    const events = pending.current;
    pending.current = [];
    // fire-and-forget：实时上报到后端接收 API（UPSERT 累加落 edit_traces 表）。
    // 注意：目标从 vite dev 中间件 /__edit_trace 改为真实后端 /session/{id}/edit-trace，
    // 由 vite proxy 转发到后端 8765，生产环境同域直连。
    fetch(`/session/${sessionId}/edit-trace`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events }),
    }).catch((e) => {
      // 静默失败会让人误以为"画像没数据=没弱项"。轨迹是 6 维错误画像的唯一
      // 多维来源，丢失即画像退化，因此至少打一条可见日志便于排查。
      console.warn('[edit-trace] 轨迹上报失败（已丢弃 %d 条事件）:', events.length, e);
    });
  }, [sessionId]);

  // 若有未结束的停顿，先记一条真实时长的 idle（停顿结束语义）
  const flushIdleIfPaused = useCallback(() => {
    if (terminal.current) return;
    if (pauseStart.current !== null) {
      pushIdle(Date.now() - pauseStart.current);
      pauseStart.current = null;
    }
  }, [pushIdle]);

  // 行为锚点：运行/提交时强制记一条当前代码快照，并立即落盘（不依赖阈值与 2s 周期）。
  // submit 同时置终态：其后编辑/停顿丢弃（C）。
  const mark = useCallback(
    (action: 'run' | 'submit') => {
      const ed = (window as unknown as { __ct_editor?: MiniEditor }).__ct_editor;
      if (ed && !terminal.current) {
        flushIdleIfPaused(); // 提交前若有未结束的停顿，先记 idle（真实时长）
        capturePending(ed); // 补记零星改动
        pushSnapshot(ed, action);
        pendingDelta.current = 0;
        pendingLineDelta.current = 0;
        if (action === 'submit') terminal.current = true; // 提交即终态
      }
      flush();
    },
    [capturePending, pushSnapshot, flush, flushIdleIfPaused],
  );

  useEffect(() => {
    if (!sessionId) return;

    // 新会话解冻：terminal 是组件级 ref，同一页面实例内跨会话存活。
    // 若沿用旧会话的"提交后冻结"，新会话的轨迹采集会被永久静默丢弃
    // （mark 不记快照、flush 空 → 零上传 → 后端 edit_traces 无记录）。
    // 冻结只对"提交时所在会话"有意义，会话切换必须重置。
    terminal.current = false;
    pauseStart.current = null;

    // 停顿计时器触发：仅标记"停顿已开始"（记录起点），不 emit、不清零 delta、不重置累计改动（A）。
    // 真正的 idle 事件在停顿结束时（下次敲键 / 提交 / 卸载）才发出，idleMs 为真实时长（D）。
    const idleFire = () => {
      const target = (window as unknown as { __ct_editor?: MiniEditor }).__ct_editor;
      if (!target) return;
      if (pauseStart.current === null) {
        pauseStart.current = lastActivity.current; // 以最后一次敲键为停顿起点
      }
    };

    const attach = (ed: MiniEditor) => {
      lastCode.current = ed.getValue();
      lastSnapLen.current = lastCode.current.length;
      const sub = ed.onDidChangeModelContent((e: any) => {
        // C：提交后冻结，其后任何编辑都不记录（含"编辑器清空"被记成空编辑的脏数据）
        if (terminal.current) return;

        const now = Date.now();

        // 停手后再次敲键 => 上一段停顿结束，先发真实时长 idle（B/D）
        flushIdleIfPaused();

        // 重置停顿计时
        lastActivity.current = now;
        if (idleTimer.current) clearTimeout(idleTimer.current);
        idleTimer.current = setTimeout(idleFire, IDLE_MS);

        // 累计"真实改动字符量"与"真实改动行数"，跨过任一阈值才记一条 edit（F）。
        // 字符量：Monaco 每个 change 的改动 = 被替换/删除的字符(rangeLength) + 新插入字符(text.length)，
        //   比单纯"长度差"更准（能捕获"等长替换"）。兜底用长度差。
        // 行数：新增行数(文本中 \n) 与 删除行数(range 跨越行) 取较大者近似"改动行数"。
        const cur = ed.getValue();
        let delta = 0;
        let lineDelta = 0;
        const changes = e && e.changes;
        if (Array.isArray(changes) && changes.length > 0) {
          for (const c of changes) {
            const added = typeof c.text === 'string' ? (c.text.match(/\n/g) || []).length : 0;
            const removed =
              c.range && typeof c.range.endLineNumber === 'number' && typeof c.range.startLineNumber === 'number'
                ? c.range.endLineNumber - c.range.startLineNumber
                : 0;
            lineDelta += Math.max(added, removed);
            delta += (c.rangeLength || 0) + (typeof c.text === 'string' ? c.text.length : 0);
          }
        } else {
          delta = Math.abs(cur.length - lastCode.current.length);
        }
        pendingDelta.current += delta;
        pendingLineDelta.current += lineDelta;
        lastCode.current = cur;

        if (pendingDelta.current >= DELTA_THRESHOLD || pendingLineDelta.current >= LINE_THRESHOLD) {
          pushSnapshot(ed, 'edit', diffChange(cur.length, lastSnapLen.current));
          pendingDelta.current = 0;
          pendingLineDelta.current = 0;
        }
      });
      // 必须包一层闭包再返回：sub.dispose 是 Monaco 类实例的原型方法、依赖 this，
      // 直接 `return sub.dispose` 解绑后在 cleanup 里调用会 this === undefined，
      // Monaco 内部读 this._isDisposed 抛 TypeError → React 卸载整棵树 → 白屏（回主页回归）
      return () => sub.dispose();
    };

    let detach: (() => void) | null = null;
    let tries = 0;
    // 持有轮询定时器，便于 cleanup 清理（编辑器可能尚未 mount）
    const poll = { current: null as ReturnType<typeof setInterval> | null };
    const existing = (window as unknown as { __ct_editor?: MiniEditor }).__ct_editor;
    if (existing) {
      detach = attach(existing);
    } else {
      poll.current = setInterval(() => {
        tries++;
        const e = (window as unknown as { __ct_editor?: MiniEditor }).__ct_editor;
        if (e || tries > 50) {
          if (poll.current) clearInterval(poll.current);
          if (e) detach = attach(e);
        }
      }, 200);
    }

    flushTimer.current = setInterval(flush, FLUSH_INTERVAL_MS);

    return () => {
      // 卸载前：先发未结束的停顿（真实 idleMs），再补记零星改动，最后发走（flush 已不造 edit）
      const ed = (window as unknown as { __ct_editor?: MiniEditor }).__ct_editor;
      if (ed && !terminal.current) {
        flushIdleIfPaused();
        capturePending(ed);
      }
      flush();
      if (idleTimer.current) clearTimeout(idleTimer.current);
      if (flushTimer.current) clearInterval(flushTimer.current);
      if (poll.current) clearInterval(poll.current);
      if (detach) detach();
    };
  }, [sessionId, flush, pushSnapshot, capturePending, flushIdleIfPaused]);

  return { flush, mark };
}
