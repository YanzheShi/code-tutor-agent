/**
 * useEditTrace —— 前端代码编辑轨迹采集（仅采集 + 传输，不做后端处理）。
 *
 * 事件类型与触发：
 *  - edit  ：行为触发（Monaco onDidChangeModelContent）。按"字符改动量 / 行改动量"累积，
 *           跨过阈值（DELTA_THRESHOLD 字符 或 LINE_THRESHOLD 行）才记一条，带全量代码 + change 语义。
 *           既保留"写循环又删"的过程，又把事件数压到合理量级。
 *  - idle  ：纯停顿标记。用户停手 >= IDLE_MS 即视为一次停顿；但只有 stuck(>=30s)
 *           和 away(>3min) 级停顿才显式记录——thinking 级(4s–30s) 是正常编码节奏
 *           （选词、读行、想变量名），隐含在 edit 时间戳间隔中，不单独记录。
 *           idle 事件 ts = 停顿起点（与后续 edit 不撞时间戳）。tab 隐藏/恢复由
 *           visibilitychange 切分：隐藏即结算当前停顿，恢复后重新计时（幂等化，
 *           同一区间只结算一次）；level 字段标注 thinking/stuck/away，away 兼容布尔。
 *  - run/submit ：行为锚点（mark 强制记一条并立即落盘）。submit 同时把本会话置为终态，
 *           其后产生的编辑/停顿一律丢弃（杜绝"提交后编辑器清空被记成空编辑"的脏数据）。
 *
 * 关键纪律（避免数据膨胀 / 信号失真）：
 *  - edit 与 idle 解耦：idle 不碰 pendingDelta，停顿只作"停顿标记"，不吞噬编辑。
 *  - flush 不造 edit：每 2s 只发送已产生的事件，零星改动由 capturePending 在
 *    停顿结束 / mark / 卸载前补记一条。
 *  - 阈值双判：字符 >=50 或 行 >=5 任一满足即记一条 edit。纯空白插入（连按 Enter）
 *    不计行数，只累计字符——空行刷屏不再逐条触发 edit。
 *
 * 快照存储（全量方案）：
 *  - 与上次快照相同 → 不存 code（same_as_prev: true，纯去重，不丢真相）。
 *  - 否则 edit/run/submit **一律存全量 code**。不再存 diff 链 / 检查点。
 *  - 后端 get_edit_trace 读取时按序直接取 code（reconstruct 退化为排序后取 code），
 *    下游 LLM 分析端零改动。
 *  - 全量方案根除 delta 链脆弱性：单点丢失只丢那一条、绝不传染半场；存储真相即代码本身。
 *  - idle 事件额外带 code_at_pause（卡壳锚定代码）+ dialogue_before（卡前导师提示），
 *    支撑抽取层产出结构化卡壳段 stuck_segments（见 trace/extract.py）。
 *
 * 体量估算（15 字符 / 1 行阈值 + 400ms 时间桶，30 分钟做题）：连续写 → ~数百条 edit，
 * 每条全量 code；分析前由 trace/extract.py 程序化蒸馏（去重 + 时间桶合并 + diff 抽取 +
 * 里程碑抽样 + token 预算裁剪），喂 LLM 的 payload 受控；原始细粒度 trace 由定时任务清理。
 *
 * 注意：验证采集质量的 spike，不做任何 LLM / 画像处理。
 */
import { useCallback, useEffect, useRef } from 'react';
import type { Message } from '../types/session';

const DELTA_THRESHOLD = 15; // 全量方案阈值收紧：宁密勿漏（分析前由 trace/extract.py 蒸馏）
const LINE_THRESHOLD = 1; // 任意一行改动即记一条 edit
const IDLE_MS = 4000; // 停手超过该时长视为一次"停顿"（卡壳起点）
const FLUSH_INTERVAL_MS = 2000; // 批量上报间隔（仅影响落盘及时性，不影响事件数）
const STUCK_MS = 30 * 1000; // 停顿 30s–3min 视为真实卡壳（thinking → stuck 边界）
const AWAY_MS = 3 * 60 * 1000; // 停顿 >3min 视为"离开"（标记 away=true；原 10min，收紧到 3min）
const MAX_FLUSH_RETRY = 3; // flush 失败最多重试次数
const ANCHOR_PREFIX = 'code-tutor:trace-anchor:';
const DIALOGUE_MAX_PER_EVENT = 4; // 单事件最多附带的导师轮次原文条数（后端也只取 4 条）
const DIALOGUE_TRUNCATE = 200; // 单条导师原文最大字符数（后端截断同值）

// 对话片段（dialogue_before 只存原文，不存完整 Message 对象）
export interface DialogueTurn {
  role: string;
  content: string;
}

export type TraceChange = 'insert' | 'delete' | 'replace' | null;

export interface TraceEvent {
  seq?: number; // 单调递增序号（hook 实例内自增；后端 (ts, seq) 排序次级键）
  ts: number; // epoch ms（idle 为停顿起点，其余为事件发生时刻）
  settled_ts?: number; // idle 专用：结算时刻（= 发出时刻，与 ts 分离以消歧排序）
  type: 'edit' | 'idle' | 'run' | 'submit';
  code?: string; // 全量代码快照（edit/run/submit 均存全量；idle 不带；旧 diff 数据读时兼容）
  same_as_prev?: boolean; // 与上一快照完全相同 → 未携带代码（纯去重，不丢真相）
  cursor?: { line: number; col: number } | null; // 光标（idle 不带）
  change?: TraceChange; // edit 专用：增/删/替（仅旧数据带，新数据不再计算）
  idleMs?: number; // idle 专用：真实停顿时长（ms）
  away?: boolean; // idle 专用：idleMs 超长（>AWAY_MS），疑似离开而非卡壳
  level?: 'thinking' | 'stuck' | 'away'; // idle 专用：停顿分级（4s–30s/30s–3min/>3min）
  problem_id?: string; // 当前题（edit-trace 端点据此按题隔离）
  dialogue_before?: DialogueTurn[]; // edit/submit/idle 专享：自锚点以来新增导师轮次原文（idle 也带，用于卡壳归因）
  code_at_pause?: string; // idle 专享：卡壳时刻的代码状态（卡壳锚定，支撑 stuck_segments）
}

// 结构化最小接口，避免直接依赖 monaco-editor 类型包
interface MiniEditor {
  getValue(): string;
  getPosition(): { lineNumber: number; column: number } | null;
  // 回调拿到 Monaco 的 IModelContentChangedEvent（用 any 避开 monaco 类型依赖），
  // 其中 e.changes[].rangeLength / .text / .range 用于计算"真实改动字符/行数"。
  onDidChangeModelContent(cb: (e: any) => void): { dispose(): void };
  getDomNode?(): HTMLElement | null; // 用于挂载 IME composition 事件监听
}

export interface UseEditTraceOpts {
  /** 当前题 problem_id（"123"），换题时由 useSession 通过 ref 更新。 */
  problemIdRef: React.MutableRefObject<string | null>;
  /** 当前导师对话（tutorMessages），用于计算 dialogue_before。 */
  tutorMessagesRef: React.MutableRefObject<Message[]>;
  /** 当前编辑器代码（React state），用于编辑器未挂载时（用户在别的 Tab）的 mark 兜底。 */
  codeRef?: React.MutableRefObject<string>;
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

// idle 分级：thinking（选词/思路间歇）→ stuck（真实卡壳）→ away（离开/查资料）
function idleLevel(idleMs: number): 'thinking' | 'stuck' | 'away' {
  if (idleMs > AWAY_MS) return 'away';
  if (idleMs >= STUCK_MS) return 'stuck';
  return 'thinking';
}

// 采集健康度统计（P2-1 可观测面）
export interface TraceStats {
  attachCount: number; // 编辑器实例代数（attach 次数）
  eventCounts: { edit: number; idle: number; run: number; submit: number };
  lastFlushTs: number; // 最近一次成功 flush 时间
  lastFlushCount: number; // 最近一次 flush 事件数
}

// 行级 diff 逻辑已迁移到后端（trace/preprocess.py:_diff_hunks），前端全量方案不再存 diff，
// 故删除 lineDiff / diffToText / changeFromDiff / DiffHunk 等前端死代码。

// 锚点持久化（localStorage）
function anchorKey(sid: string | null, pid: string): string {
  return ANCHOR_PREFIX + (sid || 'none') + ':' + pid;
}
function loadAnchor(sid: string | null, pid: string): number {
  try {
    const raw = localStorage.getItem(anchorKey(sid, pid));
    const n = raw ? parseInt(raw, 10) : 0;
    return Number.isFinite(n) && n >= 0 ? n : 0;
  } catch {
    return 0;
  }
}
function saveAnchor(sid: string | null, pid: string, value: number) {
  try {
    localStorage.setItem(anchorKey(sid, pid), String(value));
  } catch {
    /* ignore */
  }
}

function getEditor(): MiniEditor | undefined {
  return (window as unknown as { __ct_editor?: MiniEditor }).__ct_editor;
}

export function useEditTrace(sessionId: string | null, opts: UseEditTraceOpts) {
  const pending = useRef<TraceEvent[]>([]);
  const lastCode = useRef<string>(''); // 上一次按键后的代码（用于算 delta 兜底）
  const lastSnapLen = useRef<number>(0); // 上一次落快照时的代码长度（用于算 change 类型兜底）
  const lastSnapCode = useRef<string>(''); // 上一次快照的全量代码（same_as_prev 判定基准 + 去重）
  const lastSnapTs = useRef<number>(0); // 上一次快照时间（mark 自检用）
  const pendingDelta = useRef<number>(0); // 自上次快照累计的字符改动量
  const pendingLineDelta = useRef<number>(0); // 自上次快照累计的改动行数
  const lastActivity = useRef<number>(Date.now()); // 最后一次敲键时间
  const pauseStart = useRef<number | null>(null); // 当前停顿起点（null=活动中）；idle 真实时长基准
  const terminal = useRef<boolean>(false); // 提交后冻结：其后编辑/停顿一律丢弃
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flushTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  // attach 生命周期：与 mark/flush 的心跳补挂联动，避免"10s 轮询错过 → 整场静默"
  // P0-1: 保存实例引用而非布尔门——编辑器换实例时可检测并重新挂载
  const attachedEditorRef = useRef<MiniEditor | null>(null);
  const detachRef = useRef<(() => void) | null>(null);
  const flushFailRef = useRef<number>(0); // 连续失败次数（>=MAX_FLUSH_RETRY 丢弃）
  // P0-2: 停顿结算幂等——记录已结算到的时间戳，防 visibilitychange 双发
  const settledUpTo = useRef<number>(0);
  // P1-1: pid 未就绪时缓冲事件，避免落 default 桶
  const bufferedEvents = useRef<TraceEvent[]>([]);
  // P1-2: IME composition 状态——composition 期间挂起 edit 阈值判定
  const imeComposing = useRef<boolean>(false);
  // P2-2: 事件单调递增序号（hook 实例内自增）
  const seqCounter = useRef<number>(0);
  // P2-1: 采集健康度统计
  const statsRef = useRef<TraceStats>({
    attachCount: 0,
    eventCounts: { edit: 0, idle: 0, run: 0, submit: 0 },
    lastFlushTs: 0,
    lastFlushCount: 0,
  });

  // ── 锚点（对话相关性）──
  // consumedAnchor：已消费（已附进某个 edit/submit 的 dialogue_before）的导师消息数。
  // lastPid：上一次记录事件时的题，变化即换题 → 重置锚点为该题持久化值。
  const consumedAnchor = useRef<number>(0);
  const lastPid = useRef<string | null>(null);
  // stickyPid：最近已知的题。problemIdRef 短暂为 null 时回退它而非 'default'（防落错桶）。
  const stickyPid = useRef<string | null>(null);

  const currentPid = (): string => opts.problemIdRef.current ?? stickyPid.current ?? 'default';

  // 取"自锚点以来新增导师轮次原文"，并推进锚点。
  // 仅取 tutor 角色、排除 trace-* 类（分析自身消息），截断防膨胀。
  const takeDialogue = useCallback((): DialogueTurn[] => {
    const tut = opts.tutorMessagesRef.current || [];
    const from = Math.min(consumedAnchor.current, tut.length);
    const slice = tut.slice(from);
    consumedAnchor.current = tut.length;
    const turns: DialogueTurn[] = [];
    for (const m of slice) {
      if (m.role !== 'tutor') continue; // 只取导师轮次
      const kind = (m.metadata && (m.metadata.kind as unknown)) as string | undefined;
      if (kind && String(kind).startsWith('trace')) continue; // 排除分析自身消息
      const content = (m.content || '').slice(0, DIALOGUE_TRUNCATE);
      if (!content.trim()) continue;
      turns.push({ role: m.role, content });
      if (turns.length >= DIALOGUE_MAX_PER_EVENT) break;
    }
    return turns;
  }, [opts.tutorMessagesRef]);

  // edit / run / submit 落一条带代码的快照（压缩：same_as_prev / diff / 全量检查点）
  const pushSnapshot = useCallback(
    (ed: MiniEditor, type: 'edit' | 'run' | 'submit', change?: TraceChange, withDialogue = false, dialogueOverride?: DialogueTurn[]) => {
      const code = ed.getValue();
      const pid = currentPid();
      // 换题检测：pid 变化 → 重置锚点（读该题持久化值），避免跨题串对话
      if (pid !== lastPid.current) {
        consumedAnchor.current = loadAnchor(sessionId, pid);
        lastPid.current = pid;
      }
      // sticky 回退只在拿到真实 pid 时更新；pid 为 default（真不知道）不覆盖
      if (pid !== 'default') stickyPid.current = pid;

      const ev: TraceEvent = {
        seq: seqCounter.current++,
        ts: Date.now(),
        type,
        cursor: getCursor(ed),
        change,
        problem_id: pid,
      };

      // 代码存储策略（全量方案）：edit/run/submit 一律存全量 code。
      // 与上次快照相同 → 不存 code（same_as_prev，纯去重，不丢真相）；否则存全量。
      // 不再有 diff 链 / CHECKPOINT_EVERY / 检查点概念（根除 delta 链脆弱性）。
      if (code === lastSnapCode.current) {
        ev.same_as_prev = true;
      } else {
        ev.code = code; // 永远全量
      }

      if (dialogueOverride) {
        // 显式传入（submit 场景）：锚点已由调用方 takeDialogue 推进过一次
        ev.dialogue_before = dialogueOverride;
      } else if (withDialogue) {
        ev.dialogue_before = takeDialogue();
        saveAnchor(sessionId, pid, consumedAnchor.current);
      }
      pending.current.push(ev);
      statsRef.current.eventCounts[type] += 1;
      lastSnapLen.current = code.length;
      lastCode.current = code;
      lastSnapCode.current = code;
      lastSnapTs.current = Date.now();
    },
    [sessionId, takeDialogue],
  );

  // idle 落一条"纯停顿"事件（带真实 idleMs 分级）。
  // 全量方案下 idle 额外带 code_at_pause（卡壳锚定代码）+ dialogue_before（卡前导师提示），
  // 支撑后端 trace/extract.py 产出结构化卡壳段 stuck_segments（检测"卡在哪/为何卡"）。
  // ts = 停顿起点（= 现在 - idleMs），与后续 edit 不撞时间戳，时间线可直接对齐。
  const pushIdle = useCallback((idleMs: number) => {
    const level = idleLevel(idleMs);
    // thinking 级停顿（4s–30s）是正常编码节奏（选词、读行、想变量名），
    // 隐含在 edit 事件的时间戳间隔中，不单独记录——只记 stuck(30s+) 和 away(3min+)
    if (level === 'thinking') return;
    const settledTs = Date.now();
    const pid = currentPid();
    if (pid !== lastPid.current) {
      consumedAnchor.current = loadAnchor(sessionId, pid);
      lastPid.current = pid;
    }
    if (pid !== 'default') stickyPid.current = pid;
    const ed = getEditor();
    const codeAtPause = ed ? ed.getValue() : (opts.codeRef?.current ?? '');
    const ev: TraceEvent = {
      seq: seqCounter.current++,
      ts: settledTs - idleMs,
      settled_ts: settledTs,
      type: 'idle',
      idleMs,
      level,
      problem_id: pid,
      code_at_pause: codeAtPause || undefined,   // ★ 卡壳锚定的代码状态
      dialogue_before: takeDialogue(),           // ★ A2：卡壳前刚收到的提示原文
    };
    if (idleMs > AWAY_MS) ev.away = true; // 超长停顿大概率是"离开"，不当作卡壳（向后兼容）
    saveAnchor(sessionId, pid, consumedAnchor.current);
    pending.current.push(ev);
    statsRef.current.eventCounts.idle += 1;
  }, [sessionId, takeDialogue]);

  // 把累计未记录的零星改动补记一条 edit（停顿结束 / mark / 卸载前调用）
  const capturePending = useCallback(
    (ed: MiniEditor) => {
      if (pendingDelta.current <= 0 && pendingLineDelta.current <= 0) return;
      pushSnapshot(ed, 'edit', diffChange(ed.getValue().length, lastSnapLen.current), true);
      pendingDelta.current = 0;
      pendingLineDelta.current = 0;
    },
    [pushSnapshot],
  );

  // 停顿计时器触发：仅标记"停顿已开始"（记录起点），不 emit、不清零 delta、不重置累计改动。
  // 真正的 idle 事件在停顿结束时（下次敲键 / 提交 / 卸载 / tab 隐藏）才发出，idleMs 为真实时长。
  const idleFire = useCallback(() => {
    const target = getEditor();
    if (!target) return;
    if (pauseStart.current === null) {
      pauseStart.current = lastActivity.current; // 以最后一次敲键为停顿起点
    }
  }, []);

  // 若有未结束的停顿，先记一条真实时长的 idle（停顿结束语义）
  // P0-2: 无论是否结算了停顿，都更新 settledUpTo —— 防止 visibilitychange 双发
  const flushIdleIfPaused = useCallback(() => {
    if (terminal.current) return;
    const now = Date.now();
    if (pauseStart.current !== null) {
      pushIdle(now - pauseStart.current);
      pauseStart.current = null;
    }
    settledUpTo.current = now; // 标记"已结算到此刻"，visible 时据此判有无未结算区间
  }, [pushIdle]);

  // attach 编辑器并订阅改动（返回 detach）。attach 由 ensureAttached 统一触发，
  // mark / flush / 初始轮询都会尝试——编辑器晚挂载或组件重挂载都不再整场静默。
  const attachEditor = useCallback(
    (ed: MiniEditor) => {
      lastCode.current = ed.getValue();
      lastSnapLen.current = lastCode.current.length;
      lastSnapCode.current = lastCode.current;
      lastSnapTs.current = Date.now();

      // P1-2: IME composition 感知——在编辑器 DOM 上监听 compositionstart/end，
      // composition 期间挂起 edit 阈值判定（避免拼音中间态被记为独立 edit），
      // 但 lastActivity 照常更新（停顿计时不受影响）。compositionend 后按最终文本判阈值。
      const domNode = ed.getDomNode?.() ?? null;
      const onCompStart = () => { imeComposing.current = true; };
      const onCompEnd = () => {
        imeComposing.current = false;
        if (terminal.current) return;
        // composition 结束后，对累计改动一次判阈值
        if (pendingDelta.current >= DELTA_THRESHOLD || pendingLineDelta.current >= LINE_THRESHOLD) {
          const cur = ed.getValue();
          pushSnapshot(ed, 'edit', diffChange(cur.length, lastSnapLen.current), true);
          pendingDelta.current = 0;
          pendingLineDelta.current = 0;
        }
      };
      if (domNode) {
        domNode.addEventListener('compositionstart', onCompStart);
        domNode.addEventListener('compositionend', onCompEnd);
      }

      const sub = ed.onDidChangeModelContent((e: any) => {
        // 提交后冻结，其后任何编辑都不记录（含"编辑器清空"被记成空编辑的脏数据）
        if (terminal.current) return;

        const now = Date.now();

        // 停手后再次敲键 => 上一段停顿结束，先发真实时长 idle
        flushIdleIfPaused();

        // 重置停顿计时
        lastActivity.current = now;
        if (idleTimer.current) clearTimeout(idleTimer.current);
        idleTimer.current = setTimeout(idleFire, IDLE_MS);

        // 累计"真实改动字符量"与"真实改动行数"，跨过任一阈值才记一条 edit。
        // 字符量：Monaco 每个 change 的改动 = 被替换/删除的字符(rangeLength) + 新插入字符(text.length)，
        //   比单纯"长度差"更准（能捕获"等长替换"）。兜底用长度差。
        // 行数：新增行数(文本中 \n) 与 删除行数(range 跨越行) 取较大者近似"改动行数"；
        //   纯空白插入（连按 Enter 刷空行）不计行数——空行无信息量，只累计字符。
        const cur = ed.getValue();
        let delta = 0;
        let lineDelta = 0;
        const changes = e && e.changes;
        if (Array.isArray(changes) && changes.length > 0) {
          for (const c of changes) {
            const text = typeof c.text === 'string' ? c.text : '';
            const added = (text.match(/\n/g) || []).length;
            const removed =
              c.range && typeof c.range.endLineNumber === 'number' && typeof c.range.startLineNumber === 'number'
                ? c.range.endLineNumber - c.range.startLineNumber
                : 0;
            // 空行/纯缩进插入（text 含 \n 且 trim 后为空）不算行改动；删除行（text=''）正常计
            const blankInsert = added > 0 && text.trim() === '';
            if (!blankInsert) lineDelta += Math.max(added, removed);
            delta += (c.rangeLength || 0) + text.length;
          }
        } else {
          delta = Math.abs(cur.length - lastCode.current.length);
        }
        pendingDelta.current += delta;
        pendingLineDelta.current += lineDelta;
        lastCode.current = cur;

        // P1-2: IME composition 期间不产生 edit 快照（候选词变化是中间态噪音）
        if (imeComposing.current) return;

        if (pendingDelta.current >= DELTA_THRESHOLD || pendingLineDelta.current >= LINE_THRESHOLD) {
          // edit 带 dialogue_before（推进锚点）
          pushSnapshot(ed, 'edit', diffChange(cur.length, lastSnapLen.current), true);
          pendingDelta.current = 0;
          pendingLineDelta.current = 0;
        }
      });
      // 必须包一层闭包再返回：sub.dispose 是 Monaco 类实例的原型方法、依赖 this，
      // 直接 `return sub.dispose` 解绑后在 cleanup 里调用会 this === undefined，
      // Monaco 内部读 this._isDisposed 抛 TypeError → React 卸载整棵树 → 白屏（回主页回归）
      // P1-2: 同时清理 composition 事件监听
      return () => {
        sub.dispose();
        if (domNode) {
          domNode.removeEventListener('compositionstart', onCompStart);
          domNode.removeEventListener('compositionend', onCompEnd);
        }
      };
    },
    [pushSnapshot, capturePending, flushIdleIfPaused, idleFire],
  );

  // P0-1: 心跳补挂 + 实例一致性校验。编辑器已存在但尚未 attach → 立即 attach；
  // 编辑器实例已更换（Tab 切换导致卸载/重挂）→ 解绑旧订阅、重新挂载新实例。
  // attachedEditorRef 保存实例引用而非布尔值，使 ensureAttached 能检测实例变更。
  const ensureAttached = useCallback(() => {
    const ed = getEditor();
    if (!ed) return;
    if (ed === attachedEditorRef.current) return; // 同一实例，已 attach

    const isReattach = attachedEditorRef.current !== null;

    // 编辑器换实例了：先解绑旧订阅（旧编辑器可能已 dispose，try-catch 防御）
    if (detachRef.current) {
      try { detachRef.current(); } catch { /* old editor may already be disposed */ }
      detachRef.current = null;
    }

    if (isReattach) {
      console.warn('[edit-trace] 检测到编辑器实例更换，已重新挂载采集');
      // 全量方案下无 diff 链：换实例后直接落一条全量快照重启记录（不判基准，避免 guard 死代码）。
      // 卡壳/idle 也会在后续停顿结算时带 code_at_pause，不丢信息。
      try {
        pushSnapshot(ed, 'edit', undefined, false);
      } catch (err) {
        console.warn('[edit-trace] 重挂基线快照失败:', err);
      }
    } else {
      console.info('[edit-trace] 编辑器已挂载，轨迹采集生效');
    }

    try {
      detachRef.current = attachEditor(ed);
      attachedEditorRef.current = ed;
      statsRef.current.attachCount += 1;
    } catch (err) {
      console.warn('[edit-trace] attach 编辑器失败:', err);
    }
  }, [attachEditor, pushSnapshot]);

  // 仅把"已产生"的事件发出去。flush 不造 edit（避免 2s 轮询把小改动强行记成 edit）。
  // 失败自动重试：事件放回队列头部，最多 MAX_FLUSH_RETRY 次后丢弃并告警。
  // P1-1: pid 未就绪（冷启动窗口）时缓冲事件不落库，避免污染 default 桶；
  //       pid 就绪后回填正确 pid 一并发送。
  const flush = useCallback((): Promise<void> => {
    ensureAttached(); // 心跳补 attach：初始轮询错过也能被 2s 心跳救回（P0-1 实例校验也在此触发）

    // P0-1 回归保险丝：静默漂移检测——代码已变化但无累计增量 = 疑似采集通道断裂
    const ed = getEditor();
    if (ed && attachedEditorRef.current && !terminal.current) {
      if (ed.getValue() !== lastCode.current && pendingDelta.current === 0 && pendingLineDelta.current === 0) {
        console.warn('[edit-trace] 代码已变化但无累计增量 —— 疑似采集通道断裂（P0-1 回归保险丝）');
      }
    }

    if (!sessionId) return Promise.resolve();
    if (pending.current.length === 0) return Promise.resolve();

    const pid = currentPid();

    // P1-1: 冷启动窗口——pid 未就绪时缓冲事件，不落 default 桶
    if (pid === 'default') {
      bufferedEvents.current.push(...pending.current);
      pending.current = [];
      return Promise.resolve();
    }
    // pid 已就绪：若有缓冲事件，回填 pid 后一并发送
    if (bufferedEvents.current.length > 0) {
      for (const ev of bufferedEvents.current) {
        ev.problem_id = pid;
      }
      pending.current = [...bufferedEvents.current, ...pending.current];
      bufferedEvents.current = [];
    }
    // 回填 pending 中仍为 default 的事件级 pid
    //（冷启动窗口创建的事件可能跳过了缓冲阶段——问题在两次 flush 之间加载，
    //  事件已落 pending 且 pid 还是 'default'，但本次 flush 时 currentPid() 已就绪）
    for (const ev of pending.current) {
      if (!ev.problem_id || ev.problem_id === 'default') {
        ev.problem_id = pid;
      }
    }

    const events = pending.current;
    pending.current = [];
    statsRef.current.lastFlushTs = Date.now();
    statsRef.current.lastFlushCount = events.length;
    // 返回 Promise：调用方可 await，确保 run/submit 锚点落库后再放行（防刷新/关页丢事件）
    return fetch(`/session/${sessionId}/edit-trace`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events, problem_id: pid }),
    })
      .then((res) => {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        flushFailRef.current = 0;
      })
      .catch((e) => {
        flushFailRef.current += 1;
        if (flushFailRef.current < MAX_FLUSH_RETRY) {
          pending.current = [...events, ...pending.current]; // 放回队列头部，下个 tick 重试
          console.warn('[edit-trace] 轨迹上报失败，稍后重试（第 %d/%d 次）:', flushFailRef.current, MAX_FLUSH_RETRY, e);
        } else {
          flushFailRef.current = 0;
          // 静默失败会让人误以为"画像没数据=没弱项"。轨迹是 6 维错误画像的唯一
          // 多维来源，丢失即画像退化，因此至少打一条可见日志便于排查。
          console.warn('[edit-trace] 轨迹上报失败（已丢弃 %d 条事件）:', events.length, e);
        }
      });
  }, [sessionId, ensureAttached]);

  // 页面卸载/刷新前兜底：先结算停顿+补记零星改动，再用 sendBeacon 同步发走。
  // sendBeacon 不依赖 fetch 异步完成，浏览器保证在页面卸载前把请求发出（不丢 in-flight 事件）。
  // 注意：sendBeacon 单次 body ≤ 64KB；极端密集场景下超限会静默失败，由 mark 内 await flush() 兜底。
  const flushViaBeacon = useCallback(() => {
    const ed = getEditor();
    if (ed && !terminal.current) {
      flushIdleIfPaused();
      capturePending(ed);
    }
    if (bufferedEvents.current.length > 0) {
      pending.current = [...bufferedEvents.current, ...pending.current];
      bufferedEvents.current = [];
    }
    if (pending.current.length === 0 || !sessionId) return;
    const pid = currentPid();
    for (const ev of pending.current) {
      if (!ev.problem_id || ev.problem_id === 'default') ev.problem_id = pid;
    }
    const body = JSON.stringify({ events: pending.current, problem_id: pid });
    try {
      const blob = new Blob([body], { type: 'application/json' });
      navigator.sendBeacon(`/session/${sessionId}/edit-trace`, blob);
    } catch (e) {
      console.warn('[edit-trace] sendBeacon 失败:', e);
    }
    pending.current = [];
  }, [sessionId, capturePending, flushIdleIfPaused]);

  // 若有未结束的停顿，先记一条真实时长的 idle（停顿结束语义）——见 idleFire/flushIdleIfPaused 上方定义
  // 行为锚点：运行/提交时强制记一条当前代码快照，并立即落盘（不依赖阈值与 2s 周期）。
  // submit 同时置终态：其后编辑/停顿丢弃。
  //   - submit 必带 dialogue_before：先取一次"自锚点以来的导师轮次"，同时附给
  //     提交前的补记 edit 与 submit 各一份（同属最后这次修复），锚点只推进一次。
  //   - run 不带 dialogue_before（不推进锚点）。
  // 编辑器未挂载兜底：用户在别的 Tab（run/desc/tutor）时 CodeEditor 被卸载，
  // getEditor() 返回 undefined。此时用 codeRef.current 构造伪编辑器接口，
  // 保证 submit/run 仍能记录代码快照（光标缺失可接受）。
  const mark = useCallback(
    async (action: 'run' | 'submit') => {
      ensureAttached(); // 若此前 attach 失败，此刻补挂（其后编辑恢复采集）
      const realEd = getEditor();
      if (terminal.current) return;
      // 编辑器未挂载时，用 React state 中的代码构造伪编辑器接口
      const ed: MiniEditor | null = realEd ?? (opts.codeRef?.current !== undefined ? {
        getValue: () => opts.codeRef!.current,
        getPosition: () => null,
        onDidChangeModelContent: () => ({ dispose: () => {} }),
      } : null);
      if (!ed) return;

      flushIdleIfPaused(); // 提交前若有未结束的停顿，先记 idle（真实时长）
      if (action === 'submit') {
        const dlg = takeDialogue(); // 推进锚点（一次）
        saveAnchor(sessionId, currentPid(), consumedAnchor.current);
        if (pendingDelta.current > 0 || pendingLineDelta.current > 0) {
          pushSnapshot(ed, 'edit', diffChange(ed.getValue().length, lastSnapLen.current), false, dlg);
        }
        pushSnapshot(ed, 'submit', undefined, false, dlg);
      } else {
        capturePending(ed); // 补记零星改动（edit 带 dialogue）
        // 自检：代码相对上次快照已变化、但期间无 edit 事件 → 暴露未采集窗口
        if (realEd) {
          const cur = realEd.getValue();
          const stale = Date.now() - lastSnapTs.current;
          if (cur !== lastSnapCode.current && stale > 30000) {
            console.warn(
              '[edit-trace] 检测到未采集窗口：代码自上次快照（%ds 前）已变化，期间无 edit 事件，本次 run 前的过程未被记录',
              Math.round(stale / 1000),
            );
          }
        }
        pushSnapshot(ed, 'run'); // run 不带对话、不推锚点
      }
      pendingDelta.current = 0;
      pendingLineDelta.current = 0;
      if (action === 'submit') terminal.current = true; // 提交即终态
      // 立即落盘：mark 是行为锚点，await flush 确保 submit/run 事件发出（不依赖 2s 周期），
      // 防页面关闭/刷新导致 run/submit 锚点丢失；页面卸载时另有 flushViaBeacon 二次兜底。
      await flush();
    },
    [sessionId, ensureAttached, flushIdleIfPaused, capturePending, pushSnapshot, takeDialogue, flush],
  );

  useEffect(() => {
    if (!sessionId) return;

    // 新会话解冻：terminal 是组件级 ref，同一页面实例内跨会话存活。
    // 若沿用旧会话的"提交后冻结"，新会话的轨迹采集会被永久静默丢弃
    // （mark 不记快照、flush 空 → 零上传 → 后端 edit_traces 无记录）。
    // 冻结只对"提交时所在会话"有意义，会话切换必须重置。
    terminal.current = false;
    pauseStart.current = null;
    // 新会话：重置去重基准，使首条快照重新判定 same_as_prev（全量方案无 diff 链概念）
    lastSnapCode.current = '';
    // 会话切换：重置锚点基线（按当前题读持久化值），避免复用旧会话的锚点偏移；
    // sticky pid 也重置，避免跨会话沿用旧题。
    // P1-1: 冷启动窗口的事件由 bufferedEvents 缓冲，pid 就绪后回填，不再落 default 桶。
    lastPid.current = null;
    stickyPid.current = opts.problemIdRef.current ?? null;
    consumedAnchor.current = loadAnchor(sessionId, currentPid());
    // P0-2: 初始化停顿结算基线
    settledUpTo.current = Date.now();
    // P1-1: 清空冷启动缓冲
    bufferedEvents.current = [];
    // P2-2: 重置序号计数器（每会话独立）
    seqCounter.current = 0;

    // P0-2: tab 隐藏/恢复结算幂等化——单一结算基准 + settledUpTo 标志，
    // 保证任意 [pauseStart, now] 区间只被结算一次，消灭双发。
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        if (idleTimer.current) {
          clearTimeout(idleTimer.current);
          idleTimer.current = null;
        }
        // hidden：结算当前停顿（pauseStart → now），置 pauseStart=null，记录 settledUpTo=now
        flushIdleIfPaused();
      } else if (!terminal.current) {
        // visible：检查"隐藏期间"是否构成一次停顿（settledUpTo → now）
        // 核心：只结算 settledUpTo 之后的未结算部分，绝不重复结算已由 hidden 结算过的区间
        const now = Date.now();
        const hiddenDuration = now - settledUpTo.current;
        if (hiddenDuration >= IDLE_MS) {
          pushIdle(hiddenDuration); // 隐藏期间停顿（ts = settledUpTo = 隐藏时刻）
        }
        settledUpTo.current = now;
        pauseStart.current = null;
        lastActivity.current = now;
        if (idleTimer.current) clearTimeout(idleTimer.current);
        idleTimer.current = setTimeout(idleFire, IDLE_MS);
      }
    };

    // P2-3: 编辑器卸载前兜底——CodeEditor 卸载时派发 ct:editor-unmount 事件，
    // 此处捕获 pendingDelta + flushIdleIfPaused，零星改动不丢失（编辑器此刻仍可读）
    const onEditorUnmount = () => {
      const ed = getEditor();
      if (ed && !terminal.current) {
        flushIdleIfPaused();
        capturePending(ed);
      }
    };

    // 初始挂载尝试 + 200ms 轮询兜底（10s 上限）；之后由 flush/mark 的心跳继续补挂
    let tries = 0;
    const poll = { current: null as ReturnType<typeof setInterval> | null };
    ensureAttached();
    if (!attachedEditorRef.current) {
      poll.current = setInterval(() => {
        tries++;
        ensureAttached();
        if (attachedEditorRef.current || tries > 50) {
          if (poll.current) clearInterval(poll.current);
          if (tries > 50 && !attachedEditorRef.current) {
            console.warn('[edit-trace] 10s 内未拿到编辑器实例，编辑采集可能未生效（run/submit 快照仍正常，flush 心跳会继续尝试）');
          }
        }
      }, 200);
    }

    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('ct:editor-unmount', onEditorUnmount);
    window.addEventListener('pagehide', flushViaBeacon);
    window.addEventListener('beforeunload', flushViaBeacon);
    flushTimer.current = setInterval(flush, FLUSH_INTERVAL_MS);

    return () => {
      // 卸载前：先发未结束的停顿（真实 idleMs），再补记零星改动，最后发走（flush 已不造 edit）
      const ed = getEditor();
      if (ed && !terminal.current) {
        flushIdleIfPaused();
        capturePending(ed);
      }
      // P1-1: 卸载时若有未冲刷的缓冲事件，以当前 pid（或 default）发走，避免丢失
      if (bufferedEvents.current.length > 0) {
        pending.current = [...bufferedEvents.current, ...pending.current];
        bufferedEvents.current = [];
      }
      flush();
      if (idleTimer.current) clearTimeout(idleTimer.current);
      if (flushTimer.current) clearInterval(flushTimer.current);
      if (poll.current) clearInterval(poll.current);
      if (detachRef.current) {
        try { detachRef.current(); } catch { /* editor may already be disposed */ }
        detachRef.current = null;
      }
      attachedEditorRef.current = null;
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('ct:editor-unmount', onEditorUnmount);
      window.removeEventListener('pagehide', flushViaBeacon);
      window.removeEventListener('beforeunload', flushViaBeacon);
    };
  }, [sessionId, flush, ensureAttached, capturePending, flushIdleIfPaused, pushIdle, flushViaBeacon]);

  // P2-1: 采集健康度观测面——供页脚指示器 / admin debug Tab 读取
  const getStats = useCallback((): TraceStats => ({
    attachCount: statsRef.current.attachCount,
    eventCounts: { ...statsRef.current.eventCounts },
    lastFlushTs: statsRef.current.lastFlushTs,
    lastFlushCount: statsRef.current.lastFlushCount,
  }), []);

  return { flush, mark, getStats };
}