/** EChart — 按需引入的 ECharts 包装组件。
 *
 * - 只打包用到的 Line/Pie + Grid/Tooltip/Title + CanvasRenderer,约 300KB。
 * - Canvas 不支持 CSS 变量:通过 chartTokens() 在每次渲染时从 getComputedStyle
 *   解析 --ct-* 主题色,随 .dark 切换自动刷新(setOption notMerge 重建)。
 * - ResizeObserver 自适应容器宽度;销毁时 dispose,避免内存泄漏。
 */

import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { LineChart, PieChart } from 'echarts/charts';
import { GridComponent, TitleComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { ComposeOption } from 'echarts/core';
import type { LineSeriesOption, PieSeriesOption } from 'echarts/charts';
import type { GridComponentOption, TitleComponentOption, TooltipComponentOption } from 'echarts/components';
import { useTheme } from '../hooks/useTheme';

echarts.use([LineChart, PieChart, GridComponent, TitleComponent, TooltipComponent, CanvasRenderer]);

export type ChartOption = ComposeOption<
  LineSeriesOption | PieSeriesOption | GridComponentOption | TooltipComponentOption | TitleComponentOption
>;

export type ChartTokens = {
  accent: string;
  text: string;
  muted: string;
  border: string;
  surface: string;
  infoBg: string;
};

/** 解析当前主题下的 --ct-* CSS 变量为 ECharts 可用的字面颜色。 */
export function chartTokens(): ChartTokens {
  const cs = getComputedStyle(document.documentElement);
  const v = (name: string) => cs.getPropertyValue(name).trim();
  return {
    accent: v('--ct-accent'),
    text: v('--ct-text'),
    muted: v('--ct-muted'),
    border: v('--ct-border'),
    surface: v('--ct-surface'),
    infoBg: v('--ct-info-bg'),
  };
}

export default function EChart({
  option,
  ariaLabel,
  height = 180,
}: {
  option: ChartOption;
  ariaLabel: string;
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const { theme } = useTheme();

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const chart = echarts.init(el);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(el);
    return () => {
      ro.disconnect();
      chart.dispose();
    };
  }, []);

  // theme 变化时 parent 会用新解析的主题色重建 option,这里 notMerge 整体替换
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    echarts.getInstanceByDom(el)?.setOption(option, { notMerge: true });
  }, [option, theme]);

  return <div ref={ref} style={{ height }} role="img" aria-label={ariaLabel} />;
}