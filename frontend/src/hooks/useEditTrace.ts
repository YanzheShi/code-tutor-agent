/**
 * useEditTrace —— 前端代码编辑轨迹采集（仅采集 + 传输，不做后端处理）。
 *
 * 事件类型与触发：
 *  - edit  ：行为触发（Monaco onDidChangeModelContent）。按"字符改动量 / 行改动量"累积，
 *           跨过阈值（DELTA_THRESHOLD 字符 或 LINE_THRESHOLD 行）才记一条，带全量代码 + change 语义。
 *           既保留"写循环又删"的过程，又把事件数压到合理量级。
 *  - idle  ：纯停顿标记。用户停手 >= IDLE_MS 即视为一次停顿；idle 事件只在
 *           "停顿结束（下次敲键 / 提交 / 卸载 / tab 隐藏）"时发出一条，idleMs = 真实停顿时长。
 *           idle 事件 ts = 停顿起点（与后续 edit 不撞时间戳）。tab 隐藏/恢复由
 *           visibilitychange 切分：隐藏即结算当前停顿，恢复后重新计时，避免后台节流
 *           产生"42 分钟假卡壳"；idleMs > AWAY_MS 的极端值标记 away=true。
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
 * 快照存储（压缩传输/DB）：
 *  - 与上次快照相同 → 不存 code（same_as_prev: true）。
 *  - 否则存行级 diff（code_format: 'diff' + code_diff，相对上一快照的全文件行 diff）；
 *    每 CHECKPOINT_EVERY 条 diff 后落一个全量 code 检查点（submit 必为全量检查点）。
 *  - 后端 get_edit_trace 读取时按序重建全量快照，下游 LLM 分析端零改动；
 *    diff 链断裂（事件丢失）→ 该事件被丢弃并计数告警，缺口可见而非静默错位。
 *  - change 语义在存 diff 时直接由 diff 推导（增/删/替），不再依赖长度差猜测。
 *
 * 对话相关性（轨迹分析需要"独立改对 vs 被提示改对"）：
 *  - 维护**前端本地锚点** `consumedAnchor`（已消费导师消息数），作为书签，非 DB 记录。
 *  - 记 edit / submit 时：取"自锚点以来新增的导师轮次**原文**"作为 `dialogue_before` 附带，
 *    并推进锚点（= 当前导师消息数）；请求体同时带当前 `problem_id`。
 *  - run / idle：**不重置锚点、不带 `dialogue_before`**（否则丢对话）。
 *  - 锚点持久化 localStorage：键 `code-tutor:trace-anchor:<sid>:<pid>`，规避刷新后首条
 *    edit 重复抓对话。problem_id 变化（换题）时自动重置锚点。
 *  - dialogue_before 只附最近 DIALOGUE_MAX_PER_EVENT 条、每条截断 DIALOGUE_TRUNCATE 字符。
 *
 * 可靠性：
 *  - attach 支持心跳补挂：flush（2s）与 mark（run/submit）都会尝试 attach 编辑器，
 *    初始 10s 轮询错过也不至于整场静默；attach 成功只告警一次，失败打 warn。
 *  - mark 自检：run 时若代码相对上次快照已变化、但期间无 edit 事件 → warn 暴露未采集窗口。
 *  - flush 失败自动重试（最多 MAX_FLUSH_RETRY 次），不再一次失败永久丢事件。
 *  - problem_id 用 sticky 回退：problemIdRef 短暂为 null 时沿用最近已知题，
 *    避免编辑轨迹落进 "default" 垃圾桶（会话切换时重置）。
 *
 * 体量估算（50 字符 / 5 行阈值，30 分钟做题）：连续写 ~3000 字符 → ~20-40 条 edit；
 * 停顿若干 → 每条真实停顿一条 idle；diff 存储 + 20:1 检查点把 code 负载压到 ~1/5.7
 * （实测：66,996B 全量 → 11,834B，含 5 条检查点，后端按序重建无损）。
 *
 * 注意：验证采集质量的 spike，不做任何 LLM / 画像处理。
 */
import { useCallback, useEffect, useRef } from 'react';
import type { Message } from '../types/session';

const DELTA_THRESHOLD = 50; // 自上次记录起累计改动字符数达到此值即记一条 edit（调细可改 30）
const LINE_THRESHOLD = 5; // 或累计改动行数达到此值即记一条 edit（与字符阈值取"或"；空行不计行数）
const IDLE_MS = 4000; // 停手超过该时长视为一次"停顿"（卡壳起点）
const FLUSH_INTERVAL_MS = 2000; // 批量上报间隔（仅影响落盘及时性，不影响事件数）
const AWAY_MS = 10 * 60 * 1000; // idleMs 超过该值视为"离开"（标记 away=true，非卡壳）
const CHECKPOINT_EVERY = 20; // 每 N 条 diff 快照落一个全量 code 检查点（diff 链容错上限）
const MAX_FLUSH_RETRY = 3; // flush 失败最多重试次数
const DIFF_CELL_GUARD = 1_000_000; // 行级 LCS 单元数上限，超限直接存全量（防大文件卡顿）
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
  ts: number; // epoch ms（idle 为停顿起点，其余为事件发生时刻）
  type: 'edit' | 'idle' | 'run' | 'submit';
  code?: string; // 全量代码快照（仅全量检查点 / 旧数据带；idle 不带）
  code_format?: 'full' | 'diff'; // 'diff' 时快照存于 code_diff；缺省=全量（兼容旧数据）
  code_diff?: string; // 行级 diff 文本（相对上一快照；# a0-a1 -> b0-b1 / -old / +new）
  same_as_prev?: boolean; // 与上一快照完全相同 → 未携带代码
  cursor?: { line: number; col: number } | null; // 光标（idle 不带）
  change?: TraceChange; // edit 专用：增/删/替（存 diff 时由 diff 推导）
  idleMs?: number; // idle 专用：真实停顿时长（ms）
  away?: boolean; // idle 专用：idleMs 超长（>AWAY_MS），疑似离开而非卡壳
  problem_id?: string; // 当前题（edit-trace 端点据此按题隔离）
  dialogue_before?: DialogueTurn[]; // edit/submit 专享：自锚点以来新增导师轮次原文
}

// 结构化最小接口，避免直接依赖 monaco-editor 类型包
interface MiniEditor {
  getValue(): string;
  getPosition(): { lineNumber: number; column: number } | null;
  // 回调拿到 Monaco 的 IModelContentChangedEvent（用 any 避开 monaco 类型依赖），
  // 其中 e.changes[].rangeLength / .text / .range 用于计算"真实改动字符/行数"。
  onDidChangeModelContent(cb: (e: any) => void): { dispose(): void };
}

export interface UseEditTraceOpts {
  /** 当前题 problem_id（"123"），换题时由 useSession 通过 ref 更新。 */
  problemIdRef: React.MutableRefObject<string | null>;
  /** 当前导师对话（tutorMessages），用于计算 dialogue_before。 */
  tutorMessagesRef: React.MutableRefObject<Message[]>;
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

// ── 行级 diff（相对上一快照的全文件 diff，后端按 # a0-a1 -> b0-b1 / -old / +new 重建）──

export interface DiffHunk {
  a0: number;
  a1: number;
  b0: number;
  b1: number;
  oldLines: string[];
  newLines: string[];
}

// LCS 行级 diff：返回变化的行区间（行号从 0 起，与 split('\n') 索引一致）
export function lineDiff(oldText: string, newText: string): DiffHunk[] {
  const a = oldText.split('\n');
  const b = newText.split('\n');
  const n = a.length;
  const m = b.length;
  if (n * m > DIFF_CELL_GUARD) return []; // 超限：调用方退回全量快照
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const hunks: DiffHunk[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      i++;
      j++;
      continue;
    }
    const a0 = i;
    const b0 = j;
    while (i < n && j < m && a[i] !== b[j]) {
      if (dp[i + 1][j] >= dp[i][j + 1]) i++;
      else j++;
    }
    hunks.push({ a0, a1: i, b0, b1: j, oldLines: a.slice(a0, i), newLines: b.slice(b0, j) });
  }
  if (i < n) hunks.push({ a0: i, a1: n, b0: j, b1: j, oldLines: a.slice(i), newLines: [] });
  if (j < m) hunks.push({ a0: i, a1: i, b0: j, b1: m, oldLines: [], newLines: b.slice(j) });
  return hunks;
}

export function diffToText(hunks: DiffHunk[]): string {
  const parts: string[] = [];
  for (const h of hunks) {
    parts.push(`# ${h.a0}-${h.a1} -> ${h.b0}-${h.b1}`);
    for (const ln of h.oldLines) parts.push('-' + ln);
    for (const ln of h.newLines) parts.push('+' + ln);
  }
  return parts.join('\n');
}

// 由 diff 推导 change 语义（比长度差猜测更准：能区分"删+插"与"等长替换"）
function changeFromDiff(diff: string): TraceChange {
  let hasOld = false;
  let hasNew = false;
  for (const ln of diff.split('\n')) {
    if (ln.startsWith('# ')) continue;
    if (ln.startsWith('-')) hasOld = true;
    else if (ln.startsWith('+')) hasNew = true;
  }
  if (hasOld && hasNew) return 'replace';
  if (hasNew) return 'insert';
  if (hasOld) return 'delete';
  return null;
}

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
  const lastSnapCode = useRef<string>(''); // 上一次快照的全量代码（diff 基准 + same_as_prev 判定）
  const lastSnapTs = useRef<number>(0); // 上一次快照时间（mark 自检用）
  const snapCount = useRef<number>(0); // 自上个全量检查点以来的 diff 快照数
  const chainStarted = useRef<boolean>(false); // 本会话是否已发出首个全量基准（后端无 attach 时初始代码）
  const pendingDelta = useRef<number>(0); // 自上次快照累计的字符改动量
  const pendingLineDelta = useRef<number>(0); // 自上次快照累计的改动行数
  const lastActivity = useRef<number>(Date.now()); // 最后一次敲键时间
  const pauseStart = useRef<number | null>(null); // 当前停顿起点（null=活动中）；idle 真实时长基准
  const terminal = useRef<boolean>(false); // 提交后冻结：其后编辑/停顿一律丢弃
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flushTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  // attach 生命周期：与 mark/flush 的心跳补挂联动，避免"10s 轮询错过 → 整场静默"
  const attachedRef = useRef<boolean>(false);
  const detachRef = useRef<(() => void) | null>(null);
  const flushFailRef = useRef<number>(0); // 连续失败次数（>=MAX_FLUSH_RETRY 丢弃）

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
        ts: Date.now(),
        type,
        cursor: getCursor(ed),
        change,
        problem_id: pid,
      };

      // 代码存储策略：会话首个快照必为全量（后端没有 attach 时的初始代码作基准，
      // diff 链必须从全量检查点开始）；与上次快照相同 → 不存；submit → 必为全量检查点；
      // 否则存行级 diff，每 CHECKPOINT_EVERY 条落一个全量检查点（含 diff 超限退回）。
      if (!chainStarted.current) {
        ev.code = code; // 链起点：全量基准
        snapCount.current = 0;
        chainStarted.current = true;
      } else if (code === lastSnapCode.current) {
        ev.same_as_prev = true;
      } else if (type === 'submit') {
        ev.code = code;
        snapCount.current = 0;
      } else {
        const hunks = lineDiff(lastSnapCode.current, code);
        const tooBig = hunks.length === 0 && lastSnapCode.current !== code;
        if (tooBig || snapCount.current >= CHECKPOINT_EVERY) {
          ev.code = code; // 全量检查点（diff 链容错上限 / 大文件直存）
          snapCount.current = 0;
        } else {
          const diffText = diffToText(hunks);
          ev.code_format = 'diff';
          ev.code_diff = diffText;
          snapCount.current += 1;
          if (type === 'edit') ev.change = changeFromDiff(diffText);
        }
      }

      if (dialogueOverride) {
        // 显式传入（submit 场景）：锚点已由调用方 takeDialogue 推进过一次
        ev.dialogue_before = dialogueOverride;
      } else if (withDialogue) {
        ev.dialogue_before = takeDialogue();
        saveAnchor(sessionId, pid, consumedAnchor.current);
      }
      pending.current.push(ev);
      lastSnapLen.current = code.length;
      lastCode.current = code;
      lastSnapCode.current = code;
      lastSnapTs.current = Date.now();
    },
    [sessionId, takeDialogue],
  );

  // idle 只落一条"纯停顿"事件（只带真实 idleMs，不带代码/光标/对话）。
  // ts = 停顿起点（= 现在 - idleMs），与后续 edit 不撞时间戳，时间线可直接对齐。
  const pushIdle = useCallback((idleMs: number) => {
    const ev: TraceEvent = {
      ts: Date.now() - idleMs,
      type: 'idle',
      idleMs,
      problem_id: currentPid(),
    };
    if (idleMs > AWAY_MS) ev.away = true; // 超长停顿大概率是"离开"，不当作卡壳
    pending.current.push(ev);
  }, []);

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
  const flushIdleIfPaused = useCallback(() => {
    if (terminal.current) return;
    if (pauseStart.current !== null) {
      pushIdle(Date.now() - pauseStart.current);
      pauseStart.current = null;
    }
  }, [pushIdle]);

  // attach 编辑器并订阅改动（返回 detach）。attach 由 ensureAttached 统一触发，
  // mark / flush / 初始轮询都会尝试——编辑器晚挂载或组件重挂载都不再整场静默。
  const attachEditor = useCallback(
    (ed: MiniEditor) => {
      lastCode.current = ed.getValue();
      lastSnapLen.current = lastCode.current.length;
      lastSnapCode.current = lastCode.current;
      lastSnapTs.current = Date.now();
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
      return () => sub.dispose();
    },
    [pushSnapshot, capturePending, flushIdleIfPaused, idleFire],
  );

  // 心跳补挂：编辑器已存在但尚未 attach → 立即 attach（mark / flush / 轮询共用）
  const ensureAttached = useCallback(() => {
    const ed = getEditor();
    if (!ed || attachedRef.current) return;
    try {
      detachRef.current = attachEditor(ed);
      attachedRef.current = true;
      console.info('[edit-trace] 编辑器已挂载，轨迹采集生效');
    } catch (err) {
      console.warn('[edit-trace] attach 编辑器失败:', err);
    }
  }, [attachEditor]);

  // 仅把"已产生"的事件发出去。flush 不造 edit（避免 2s 轮询把小改动强行记成 edit）。
  // 失败自动重试：事件放回队列头部，最多 MAX_FLUSH_RETRY 次后丢弃并告警。
  const flush = useCallback(() => {
    ensureAttached(); // 心跳补 attach：初始轮询错过也能被 2s 心跳救回
    if (!sessionId) return;
    if (pending.current.length === 0) return;
    const events = pending.current;
    pending.current = [];
    // fire-and-forget：实时上报到后端接收 API（UPSERT 累加落 edit_traces 表）。
    fetch(`/session/${sessionId}/edit-trace`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events, problem_id: currentPid() }),
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

  // 若有未结束的停顿，先记一条真实时长的 idle（停顿结束语义）——见 idleFire/flushIdleIfPaused 上方定义
  // 行为锚点：运行/提交时强制记一条当前代码快照，并立即落盘（不依赖阈值与 2s 周期）。
  // submit 同时置终态：其后编辑/停顿丢弃。
  //   - submit 必带 dialogue_before：先取一次"自锚点以来的导师轮次"，同时附给
  //     提交前的补记 edit 与 submit 各一份（同属最后这次修复），锚点只推进一次。
  //   - run 不带 dialogue_before（不推进锚点）。
  const mark = useCallback(
    (action: 'run' | 'submit') => {
      ensureAttached(); // 若此前 attach 失败，此刻补挂（其后编辑恢复采集）
      const ed = getEditor();
      if (ed && !terminal.current) {
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
          const cur = ed.getValue();
          const stale = Date.now() - lastSnapTs.current;
          if (cur !== lastSnapCode.current && stale > 30000) {
            console.warn(
              '[edit-trace] 检测到未采集窗口：代码自上次快照（%ds 前）已变化，期间无 edit 事件，本次 run 前的过程未被记录',
              Math.round(stale / 1000),
            );
          }
          pushSnapshot(ed, 'run'); // run 不带对话、不推锚点
        }
        pendingDelta.current = 0;
        pendingLineDelta.current = 0;
        if (action === 'submit') terminal.current = true; // 提交即终态
      }
    },
    [sessionId, ensureAttached, flushIdleIfPaused, capturePending, pushSnapshot, takeDialogue],
  );

  useEffect(() => {
    if (!sessionId) return;

    // 新会话解冻：terminal 是组件级 ref，同一页面实例内跨会话存活。
    // 若沿用旧会话的"提交后冻结"，新会话的轨迹采集会被永久静默丢弃
    // （mark 不记快照、flush 空 → 零上传 → 后端 edit_traces 无记录）。
    // 冻结只对"提交时所在会话"有意义，会话切换必须重置。
    terminal.current = false;
    pauseStart.current = null;
    // 新会话 diff 链重启：首个快照必为全量基准（链从后端可见的全量开始）
    chainStarted.current = false;
    // 会话切换：重置锚点基线（按当前题读持久化值），避免复用旧会话的锚点偏移；
    // sticky pid 也重置，避免跨会话沿用旧题（problemIdRef 就绪前的事件会短暂落 default，
    // 由后端按时间窗归并兜底）。
    lastPid.current = null;
    stickyPid.current = opts.problemIdRef.current ?? null;
    consumedAnchor.current = loadAnchor(sessionId, currentPid());

    // tab 隐藏：结算当前停顿（后台定时器节流会让 idle 计时失真，靠 visibility 切分）；
    // tab 恢复：若离开前无停顿（<4s 内切走），把隐藏时长按一次停顿结算，再重新计时。
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        if (idleTimer.current) {
          clearTimeout(idleTimer.current);
          idleTimer.current = null;
        }
        flushIdleIfPaused();
      } else if (!terminal.current) {
        const away = Date.now() - lastActivity.current;
        if (pauseStart.current === null && away > IDLE_MS) {
          pushIdle(away); // 隐藏期间的停顿（起点 ≈ lastActivity）
        }
        lastActivity.current = Date.now();
        if (idleTimer.current) clearTimeout(idleTimer.current);
        idleTimer.current = setTimeout(idleFire, IDLE_MS);
      }
    };

    // 初始挂载尝试 + 200ms 轮询兜底（10s 上限）；之后由 flush/mark 的心跳继续补挂
    let tries = 0;
    const poll = { current: null as ReturnType<typeof setInterval> | null };
    ensureAttached();
    if (!attachedRef.current) {
      poll.current = setInterval(() => {
        tries++;
        ensureAttached();
        if (attachedRef.current || tries > 50) {
          if (poll.current) clearInterval(poll.current);
          if (tries > 50 && !attachedRef.current) {
            console.warn('[edit-trace] 10s 内未拿到编辑器实例，编辑采集可能未生效（run/submit 快照仍正常，flush 心跳会继续尝试）');
          }
        }
      }, 200);
    }

    document.addEventListener('visibilitychange', onVisibility);
    flushTimer.current = setInterval(flush, FLUSH_INTERVAL_MS);

    return () => {
      // 卸载前：先发未结束的停顿（真实 idleMs），再补记零星改动，最后发走（flush 已不造 edit）
      const ed = getEditor();
      if (ed && !terminal.current) {
        flushIdleIfPaused();
        capturePending(ed);
      }
      flush();
      if (idleTimer.current) clearTimeout(idleTimer.current);
      if (flushTimer.current) clearInterval(flushTimer.current);
      if (poll.current) clearInterval(poll.current);
      if (detachRef.current) {
        detachRef.current();
        detachRef.current = null;
      }
      attachedRef.current = false;
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [sessionId, flush, ensureAttached, capturePending, flushIdleIfPaused, pushIdle]);

  return { flush, mark };
}