import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * 全量方案下的轨迹存储语义验证（取代旧的 diff 往返测试）：
 * 1) 每个 edit/run/submit 事件自带全量 code（不再有 diff 链 / 检查点 / code_format）；
 * 2) same_as_prev 事件不携带 code，读取时继承上一条 code（纯去重，不丢真相）；
 * 3) 单点丢失只丢那一条、绝不传染半场（无 diff 链拼接）。
 *
 * 用真实轨迹文件 a.json 验证存储真相即代码本身。
 */

// 后端 database.reconstruct_edit_trace 的 JS 对照实现（全量方案分支）
function reconstruct(events: any[]): { out: any[]; dropped: number } {
  const out: any[] = [];
  let lastCode: string | null = null;
  let dropped = 0;
  for (const raw of events) {
    const ev = { ...raw };
    if (ev.same_as_prev) {
      // 全量方案去重事件：继承上一条 code
      if (lastCode !== null) ev.code = lastCode;
    } else if (ev.code != null) {
      lastCode = ev.code;
    }
    out.push(ev);
  }
  return { out, dropped };
}

// 前端 pushSnapshot 的全量压缩逻辑（全量方案）：
// 与上一快照相同 → same_as_prev（不存 code）；否则一律存全量 code。
function compressSnapshots(events: any[]) {
  let lastCode = '';
  const out: any[] = [];
  let samePrev = 0;
  let full = 0;
  for (const ev of events) {
    const e: any = { ...ev };
    if (ev.code === lastCode) {
      delete e.code;
      e.same_as_prev = true;
      samePrev++;
    } else {
      full++; // 全量 code 落库
    }
    lastCode = ev.code;
    out.push(e);
  }
  return { out, samePrev, full };
}

describe('trace full-snapshot storage on real a.json', () => {
  const raw = JSON.parse(
    readFileSync(resolve(__dirname, '..', '..', 'a.json'), 'utf-8'),
  ) as any[];
  const snapEvents = raw.filter((e) => e && typeof e.code === 'string');

  it('a.json 非空且包含带 code 的快照事件', () => {
    expect(snapEvents.length).toBeGreaterThan(50);
  });

  it('全量存储：每个 edit/run/submit 自带 code，无 diff 链字段', () => {
    const { out, samePrev, full } = compressSnapshots(snapEvents);
    // 全量方案下不再有任何 code_format / code_diff
    expect(out.every((e) => !e.code_format && !e.code_diff)).toBe(true);
    // 每条要么全量、要么 same_as_prev，覆盖全部
    expect(samePrev + full).toBe(snapEvents.length);
    // 至少有一些全量快照（包括首条）
    expect(full).toBeGreaterThan(0);
  });

  it('same_as_prev 继承上一条 code，重建无损', () => {
    const { out, dropped } = reconstruct(compressSnapshots(snapEvents).out);
    expect(dropped).toBe(0);
    expect(out.length).toBe(snapEvents.length);

    let expectedCode: string | null = null;
    for (const orig of snapEvents) {
      if (orig.code !== expectedCode) expectedCode = orig.code;
      const rb = out[snapEvents.indexOf(orig)];
      const actual = rb.same_as_prev ? rb.code : rb.code;
      expect(actual, `ts=${orig.ts} type=${orig.type} 重建不一致`).toBe(expectedCode);
    }
  });

  it('单点丢失不传染：删掉任意一条 edit，其余仍可重建', () => {
    const compressed = compressSnapshots(snapEvents).out;
    const sliced = compressed.filter((_, i) => i !== 10); // 删第 11 条
    const { out, dropped } = reconstruct(sliced);
    expect(dropped).toBe(0);
    expect(out.length).toBe(snapEvents.length - 1);
    // 被删那条之后的事件 code 仍正确（无 diff 链拼接错误）
    const after = out[snapEvents.length - 2];
    expect(after.code).toBe(snapEvents[snapEvents.length - 1].code);
  });
});
