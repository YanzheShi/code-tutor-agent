import { describe, expect, it } from 'vitest';
import { buildCeMarkers, CE_MARKER_OWNER } from '../src/components/LeftPanel/CodeEditor';

/** CE 编辑器行内红标：marker 构造纯函数（2026-09-09 编译错误指针 → 编辑器标注）。 */
describe('buildCeMarkers', () => {
  it('null / 无效详情 → 空数组（清除语义）', () => {
    expect(buildCeMarkers(null, 100)).toEqual([]);
    expect(buildCeMarkers({ line: 0, column: 1, message: 'x' }, 100)).toEqual([]);
    expect(buildCeMarkers({ line: -3, column: 1, message: 'x' }, 100)).toEqual([]);
  });

  it('正常详情 → 单个 Error 级 marker，位置与 payload 对齐', () => {
    const markers = buildCeMarkers({ line: 4, column: 3, message: 'unexpected EOF' }, 50);
    expect(markers).toHaveLength(1);
    const m = markers[0];
    expect(m.startLineNumber).toBe(4);
    expect(m.startColumn).toBe(3);
    expect(m.endLineNumber).toBe(4);
    expect(m.endColumn).toBe(4);
    expect(m.message).toBe('unexpected EOF');
    expect(m.severity).toBe(8); // MarkerSeverity.Error
  });

  it('column 缺省/非法 → 回退 1；message 缺省 → 兜底文案', () => {
    const m = buildCeMarkers({ line: 2, column: 0, message: '' }, 10)[0];
    expect(m.startColumn).toBe(1);
    expect(m.message).toBe('编译错误');
  });

  it('行号超出模型行数 → clamp 到最后一行（防御用户已改代码）', () => {
    const m = buildCeMarkers({ line: 999, column: 1, message: 'x' }, 30)[0];
    expect(m.startLineNumber).toBe(30);
    expect(m.endLineNumber).toBe(30);
  });

  it('marker owner 常量稳定（同 owner 重复 set 即覆盖）', () => {
    expect(CE_MARKER_OWNER).toBe('cta-compile-error');
  });
});
