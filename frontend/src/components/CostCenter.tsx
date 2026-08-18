/** CostCenter — Token 成本中心(管理员向)。
 *
 * 5 个子视图,忠实还原 docs/token-cost-dashboard 设计稿:
 *   概览 / 用途分析 / 缓存命中 / 预算预警 / 调用明细
 *
 * 数据来自后端 /admin/token/*(旁路采集,零侵入)。单用户实例视角:
 * 不展示「用户」维度,预算为「你的日预算 + 单 Session」。
 */

import { useCallback, useEffect, useState } from 'react';
import { API_BASE } from '../api/config';
import { useTheme } from '../hooks/useTheme';
import EChart, { chartTokens, type ChartOption } from './EChart';

const BASE = API_BASE;

type CostTab = 'overview' | 'purpose' | 'cache' | 'budget' | 'detail';
type RangeKey = 'today' | '7d' | '30d';

const TABS: { id: CostTab; label: string }[] = [
  { id: 'overview', label: '概览' },
  { id: 'purpose', label: '用途分析' },
  { id: 'cache', label: '缓存命中' },
  { id: 'budget', label: '预算预警' },
  { id: 'detail', label: '调用明细' },
];

const RANGES: { id: RangeKey; label: string }[] = [
  { id: '30d', label: '近 30 天' },
  { id: '7d', label: '近 7 天' },
  { id: 'today', label: '今日' },
];

type ModelKey = '全部' | 'default' | 'secondary';
const MODELS: { id: ModelKey; label: string }[] = [
  { id: '全部', label: '全部' },
  { id: 'default', label: 'default' },
  { id: 'secondary', label: 'secondary' },
];

const PALETTE = ['#0969da', '#1a7f37', '#9a6700', '#cf222e', '#8250df', '#1b7c83', '#d4a72c', '#6e7781'];

// ── helpers ──
function fmtMoney(v: number): string {
  if (!isFinite(v)) return '¥0';
  return '¥' + (v >= 10 ? String(Math.round(v)) : v.toFixed(2));
}
function fmtInt(v: number): string {
  return Math.round(v).toLocaleString('en-US');
}
function fmtToken(v: number): string {
  if (!isFinite(v)) return '0';
  if (v >= 1e6) { const m = v / 1e6; return (m >= 10 ? String(Math.round(m)) : m.toFixed(1)) + 'M'; }
  if (v >= 1e3) return Math.round(v / 1e3) + 'K';
  return fmtInt(v);
}
function todayStr(): string {
  return new Date().toISOString().slice(0, 10);
}
function rangeDates(r: RangeKey): { from: string; to: string } {
  const to = todayStr();
  if (r === 'today') return { from: to, to };
  const days = r === '7d' ? 6 : 29;
  const d = new Date();
  d.setDate(d.getDate() - days);
  return { from: d.toISOString().slice(0, 10), to };
}

// ── small UI atoms ──
function DeltaBadge({ delta, goodWhenUp = false }: { delta: number; goodWhenUp?: boolean }) {
  if (delta === 0 || delta === undefined) return <span className="rounded bg-ct-hover px-2 py-0.5 text-xs text-ct-muted">—</span>;
  const up = delta > 0;
  const good = goodWhenUp ? up : !up;
  // 底色徽章:涨红/降绿/持平灰,与设计稿语义色一致
  const bgCls = good ? 'bg-ct-success-bg text-ct-success' :
    (delta === 0 ? 'bg-ct-hover text-ct-muted' : 'bg-ct-error-bg text-ct-error');
  return (
    <span className={`rounded px-2 py-0.5 text-xs font-medium ${bgCls}`}>
      {up ? '▲' : '▼'} {Math.abs(delta)}%
    </span>
  );
}

function Bar({ pct, color }: { pct: number; color: string }) {
  return (
    <div className="h-2 w-full rounded-full bg-ct-hover">
      <div className="h-2 rounded-full" style={{ width: Math.max(0, Math.min(100, pct)) + '%', backgroundColor: color }} />
    </div>
  );
}

// ── ECharts 图表 ──
// 主题色由 chartTokens() 解析 --ct-* CSS 变量;useTheme 订阅使主题切换时重建 option。

function Donut({ data, fmt, centerLabel }: {
  data: { purpose: string; value: number; pct: number }[];
  fmt: (v: number) => string;
  centerLabel: string;
}) {
  useTheme(); // 主题切换时触发重渲染,chartTokens 重新取值
  const t = chartTokens();
  const total = data.reduce((s, d) => s + d.value, 0) || 0;
  const option: ChartOption = {
    tooltip: {
      trigger: 'item',
      formatter: (p) => {
        const it = p as { name: string; value: number; percent: number };
        return `${it.name} ${fmt(it.value)} (${it.percent}%)`;
      },
    },
    title: {
      text: fmt(total),
      subtext: centerLabel,
      left: 'center', top: '40%',
      textAlign: 'center',
      textStyle: { fontSize: 15, fontWeight: 600, color: t.text },
      subtextStyle: { fontSize: 11, color: t.muted },
      itemGap: 2,
    },
    series: [{
      type: 'pie',
      radius: ['62%', '90%'],
      center: ['50%', '50%'],
      avoidLabelOverlap: true,
      label: { show: false },
      emphasis: { scale: true, scaleSize: 4 },
      itemStyle: { borderColor: t.surface, borderWidth: 2, borderRadius: 3 },
      data: data.map((d, i) => ({
        name: d.purpose,
        value: d.value,
        itemStyle: { color: PALETTE[i % PALETTE.length] },
      })),
    }],
  };
  return (
    <div className="flex items-center gap-4">
      <div className="h-[160px] w-[160px] shrink-0">
        <EChart option={option} ariaLabel={centerLabel} />
      </div>
      <div className="flex-1 space-y-1">
        {data.map((d, i) => (
          <div key={d.purpose} className="flex items-center gap-2 text-xs">
            <span className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ backgroundColor: PALETTE[i % PALETTE.length] }} />
            <span className="flex-1 truncate font-mono text-ct-text">{d.purpose}</span>
            <span className="text-ct-muted">{d.pct.toFixed(1)}%</span>
            <span className="w-16 text-right font-mono text-ct-text">{fmt(d.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// 成本面积图:单 series + areaStyle,边界不闭合(boundaryGap:false)
function AreaChart({ trend }: { trend: { day: string; cost: number }[] }) {
  useTheme();
  const t = chartTokens();
  if (trend.length === 0) return <div className="text-xs text-ct-muted">暂无数据</div>;
  const max = Math.max(...trend.map((d) => d.cost), 1);
  // 成本 Y 轴:整数 ¥(¥0 / ¥5 / ¥10),避免 ¥0.00 这种细度
  const fmtCostAxis = (v: number) => v === 0 ? '¥0' : '¥' + (v >= 10 ? Math.round(v).toString() : v.toFixed(1));
  const option: ChartOption = {
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v) => fmtMoney(Number(v)),
    },
    grid: { left: 4, right: 4, top: 8, bottom: 2, containLabel: true },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: trend.map((d) => d.day),
      axisLabel: { color: t.muted, fontSize: 10, formatter: (d: string) => d.slice(5) },
      axisLine: { lineStyle: { color: t.border } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      max, interval: max / 2,
      axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => fmtCostAxis(v) },
      splitLine: { lineStyle: { type: 'dashed', color: t.border, opacity: 0.5 } },
    },
    series: [{
      type: 'line',
      data: trend.map((d) => d.cost),
      symbol: 'circle', symbolSize: 4, showSymbol: trend.length <= 15,
      lineStyle: { color: t.accent, width: 1.5 },
      itemStyle: { color: t.accent },
      areaStyle: { color: t.infoBg },
    }],
  };
  return <EChart option={option} ariaLabel="成本趋势" />;
}

// 堆叠面积:输出 / 输入(非缓存) / 缓存读,stack 总和 = 总 Token
// (缓存写不展示:SenseNova/DeepSeek 网关不返回 cache_creation,恒为 0)
function StackedAreaChart({ trend }: { trend: {
  day: string; prompt: number; completion: number; cache_read: number; cache_creation: number;
}[] }) {
  useTheme();
  const t = chartTokens();
  if (trend.length === 0) return <div className="text-xs text-ct-muted">暂无数据</div>;
  const layers = [
    { key: '输出', color: '#1a7f37', get: (d: typeof trend[number]) => d.completion },
    { key: '输入(非缓存)', color: '#0969da', get: (d: typeof trend[number]) => Math.max(0, d.prompt - d.cache_read - d.cache_creation) },
    { key: '缓存读', color: '#9a6700', get: (d: typeof trend[number]) => d.cache_read },
  ];
  const max = Math.max(...trend.map((d) => d.prompt + d.completion), 1);
  const option: ChartOption = {
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v) => fmtToken(Number(v)),
    },
    grid: { left: 4, right: 4, top: 8, bottom: 2, containLabel: true },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: trend.map((d) => d.day),
      axisLabel: { color: t.muted, fontSize: 10, formatter: (d: string) => d.slice(5) },
      axisLine: { lineStyle: { color: t.border } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      max, interval: max / 2,
      axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => fmtToken(v) },
      splitLine: { lineStyle: { type: 'dashed', color: t.border, opacity: 0.5 } },
    },
    series: layers.map((l) => ({
      type: 'line',
      name: l.key,
      stack: 'total',
      data: trend.map((d) => l.get(d)),
      symbol: 'circle', symbolSize: 4, showSymbol: trend.length <= 15,
      lineStyle: { color: l.color, width: 1 },
      itemStyle: { color: l.color },
      areaStyle: { color: l.color, opacity: 0.85 },
    })),
  };
  return (
    <div>
      <EChart option={option} ariaLabel="Token 量趋势" />
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ct-muted">
        {layers.map((l) => (
          <span key={l.key} className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-sm" style={{ backgroundColor: l.color }} />{l.key}
          </span>
        ))}
      </div>
    </div>
  );
}

function useFetch<T>(url: string, body: object, deps: unknown[]): { data: T | null; loading: boolean; error: string } {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {
    let cancelled = false;
    setLoading(true); setError('');
    fetch(BASE + url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status))))
      .then((j) => { if (!cancelled) setData(j); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, loading, error };
}

// ── Tab: 概览 ──
function OverviewTab({ password, range, model }: { password: string; range: RangeKey; model: string }) {
  const { from, to } = rangeDates(range);
  const { data, loading, error } = useFetch<{
    kpis: { label: string; value: number; delta: number }[];
    trend: { day: string; cost: number }[];
    tokenTrend: { day: string; prompt: number; completion: number; cache_read: number; cache_creation: number }[];
    moduleShare: { purpose: string; cost: number; pct: number }[];
    moduleTokenShare: { purpose: string; tokens: number; pct: number }[];
    topPurposes: { purpose: string; cost: number }[];
    range: { from: string; to: string; model: string };
  }>('/admin/token/overview', { password, from_date: from, to_date: to, model_alias: model }, [password, range, model]);

  if (loading) return <Center><Spinner />加载概览中…</Center>;
  if (error) return <Center><ErrorMsg>{error}</ErrorMsg></Center>;
  if (!data) return <Center><Empty>暂无数据</Empty></Center>;

  const goodUp: Record<string, boolean> = { '缓存命中率': true, '缓存读': true };
  const maxTop = Math.max(...data.topPurposes.map((d) => d.cost), 1);

  const kpiFmt = (k: { label: string; value: number }) =>
    k.label.includes('命中率') ? `${k.value}%`
      : k.label.includes('Token') || k.label.includes('缓存') ? fmtToken(k.value)
        : k.label.includes('月费') || k.label.includes('成本') ? fmtMoney(k.value)
          : fmtInt(k.value);

  const kpiCard = (k: { label: string; value: number; delta: number }) => (
    <div key={k.label} className="rounded-xl border border-ct-border bg-ct-surface p-4">
      <div className="flex items-center justify-between">
        <span className="text-xs text-ct-muted">{k.label}</span>
        <DeltaBadge delta={k.delta} goodWhenUp={goodUp[k.label]} />
      </div>
      <div className="mt-1 font-mono text-2xl font-semibold text-ct-text">{kpiFmt(k)}</div>
    </div>
  );

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {data.kpis.slice(0, 4).map(kpiCard)}
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {data.kpis.slice(4).map(kpiCard)}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1.3fr_1fr]">
        <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
          <div className="mb-1 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-ct-text">成本趋势</h3>
            <span className="rounded-full border border-ct-border px-2 py-0.5 text-[10px] text-ct-muted">日成本 ¥</span>
          </div>
          <AreaChart trend={data.trend} />
        </section>
        <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-ct-text">各模块成本占比</h3>
            <span className="rounded-full border border-ct-border px-2 py-0.5 text-[10px] text-ct-muted">随筛选</span>
          </div>
          <Donut data={data.moduleShare.map((d) => ({ purpose: d.purpose, value: d.cost, pct: d.pct }))}
            fmt={fmtMoney} centerLabel="总成本" />
        </section>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1.3fr_1fr]">
        <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
          <div className="mb-1 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-ct-text">Token 量趋势</h3>
            <span className="rounded-full border border-ct-border px-2 py-0.5 text-[10px] text-ct-muted">日 Token · 堆叠</span>
          </div>
          <StackedAreaChart trend={data.tokenTrend} />
        </section>
        <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-ct-text">各模块 Token 占比</h3>
            <span className="rounded-full border border-ct-border px-2 py-0.5 text-[10px] text-ct-muted">随筛选</span>
          </div>
          <Donut data={data.moduleTokenShare.map((d) => ({ purpose: d.purpose, value: d.tokens, pct: d.pct }))}
            fmt={fmtToken} centerLabel="总 Token" />
        </section>
      </div>

      <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
        <h3 className="mb-3 text-sm font-semibold text-ct-text">烧钱 Top 5 用途</h3>
        <div className="space-y-3">
          {data.topPurposes.map((p) => (
            <div key={p.purpose}>
              <div className="mb-1 flex items-center justify-between text-xs">
                <span className="font-mono text-ct-text">{p.purpose}</span>
                <span className="font-mono text-ct-text">{fmtMoney(p.cost)}</span>
              </div>
              <Bar pct={(p.cost / maxTop) * 100} color="var(--ct-accent)" />
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

// ── Tab: 用途分析 ──
function PurposeTab({ password, range, model }: { password: string; range: RangeKey; model: string }) {
  const { from, to } = rangeDates(range);
  const { data, loading, error } = useFetch<{ rows: {
    purpose: string; category: string; calls: number; promptK: number; completionK: number;
    cacheReadK: number; hit: number; cost: number; delta: number;
  }[] }>('/admin/token/purposes', { password, from_date: from, to_date: to, model_alias: model }, [password, range, model]);

  if (loading) return <Center><Spinner />加载中…</Center>;
  if (error) return <Center><ErrorMsg>{error}</ErrorMsg></Center>;
  if (!data || data.rows.length === 0) return <Center><Empty>暂无数据</Empty></Center>;

  return (
    <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
      <h3 className="mb-3 text-sm font-semibold text-ct-text">按业务用途统计</h3>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-xs text-ct-muted">
              <th className="py-2 pr-3 font-medium">用途</th><th className="py-2 pr-3 font-medium">分类</th>
              <th className="py-2 pr-3 font-medium">调用次数</th><th className="py-2 pr-3 font-medium">输入(K)</th>
              <th className="py-2 pr-3 font-medium">输出(K)</th><th className="py-2 pr-3 font-medium">缓存读(K)</th>
              <th className="py-2 pr-3 font-medium">命中率</th><th className="py-2 pr-3 font-medium">成本</th>
              <th className="py-2 pr-3 font-medium">环比</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ct-border/50">
            {data.rows.map((r) => (
              <tr key={r.purpose} className="hover:bg-ct-surface-secondary">
                <td className="py-2 pr-3 font-mono font-medium text-ct-text">{r.purpose}</td>
                <td className="py-2 pr-3"><span className="rounded bg-ct-hover px-1.5 py-0.5 text-xs text-ct-muted">{r.category}</span></td>
                <td className="py-2 pr-3 font-mono text-ct-text">{fmtInt(r.calls)}</td>
                <td className="py-2 pr-3 font-mono text-ct-text">{r.promptK}</td>
                <td className="py-2 pr-3 font-mono text-ct-text">{r.completionK}</td>
                <td className="py-2 pr-3 font-mono text-ct-success">{r.cacheReadK}</td>
                <td className="py-2 pr-3" style={{ minWidth: 120 }}>
                  <div className="flex items-center gap-2">
                    <div className="flex-1"><Bar pct={r.hit} color={r.hit < 40 ? 'var(--ct-error)' : 'var(--ct-success)'} /></div>
                    <span className="font-mono text-xs text-ct-muted">{r.hit}%</span>
                  </div>
                </td>
                <td className="py-2 pr-3 font-mono font-medium text-ct-text">{fmtMoney(r.cost)}</td>
                <td className="py-2 pr-3"><DeltaBadge delta={r.delta} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ── Tab: 缓存命中 ──
function CacheTab({ password, range, model }: { password: string; range: RangeKey; model: string }) {
  const { from, to } = rangeDates(range);
  const { data, loading, error } = useFetch<{ rows: {
    purpose: string; category: string; hit: number; tip: string | null;
  }[] }>('/admin/token/cache', { password, from_date: from, to_date: to, model_alias: model }, [password, range, model]);

  if (loading) return <Center><Spinner />加载中…</Center>;
  if (error) return <Center><ErrorMsg>{error}</ErrorMsg></Center>;
  if (!data || data.rows.length === 0) return <Center><Empty>暂无数据</Empty></Center>;

  const diag = data.rows.filter((r) => r.tip);

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
        <h3 className="mb-3 text-sm font-semibold text-ct-text">各用途缓存命中率</h3>
        <div className="space-y-3">
          {data.rows.map((r) => (
            <div key={r.purpose}>
              <div className="mb-1 flex items-center justify-between text-xs">
                <span className="font-mono text-ct-text">{r.purpose}</span>
                <span className="font-mono" style={{ color: r.hit < 40 ? 'var(--ct-error)' : 'var(--ct-success)' }}>{r.hit}%</span>
              </div>
              <Bar pct={r.hit} color={r.hit < 40 ? 'var(--ct-error)' : 'var(--ct-success)'} />
            </div>
          ))}
        </div>
      </section>
      <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
        <h3 className="mb-1 text-sm font-semibold text-ct-text">失效诊断与优化建议</h3>
        <p className="mb-3 text-xs text-ct-muted">DeepSeek 为前缀缓存:稳定内容前置、动态内容后置才能命中。</p>
        <div className="space-y-2">
          {diag.length === 0 && <p className="text-xs text-ct-success">全部用途命中率健康(≥40%) ✓</p>}
          {diag.map((d) => (
            <div key={d.purpose} className="rounded-lg border border-ct-border border-l-[3px] border-l-ct-warn bg-ct-surface-secondary p-3">
              <div className="flex items-center gap-2">
                <span className="font-mono text-xs font-medium text-ct-text">{d.purpose}</span>
                <span className="rounded bg-ct-error-bg px-1.5 py-0.5 text-[10px] text-ct-error">命中 {d.hit}%</span>
              </div>
              <p className="mt-1 text-xs leading-relaxed text-ct-text">{d.tip}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

// ── Tab: 预算预警 ──
function BudgetTab({ password }: { password: string }) {
  const { data, loading, error } = useFetch<{
    budgets: { name: string; used: number; limit: number }[];
    alerts: { level: string; title: string; detail: string }[];
  }>('/admin/token/budget', { password }, [password]);

  if (loading) return <Center><Spinner />加载中…</Center>;
  if (error) return <Center><ErrorMsg>{error}</ErrorMsg></Center>;
  if (!data) return <Center><Empty>暂无数据</Empty></Center>;

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
        <h3 className="mb-3 text-sm font-semibold text-ct-text">预算使用</h3>
        <div className="space-y-4">
          {data.budgets.map((b) => {
            const pct = b.limit ? Math.round((b.used / b.limit) * 100) : 0;
            const color = pct >= 90 ? 'var(--ct-error)' : pct >= 75 ? 'var(--ct-warn)' : 'var(--ct-accent)';
            return (
              <div key={b.name}>
                <div className="mb-1 flex items-center justify-between text-xs">
                  <span className="text-ct-text">{b.name}</span>
                  <span className="font-mono text-ct-muted">{fmtMoney(b.used)} / {fmtMoney(b.limit)} · {pct}%</span>
                </div>
                <Bar pct={pct} color={color} />
              </div>
            );
          })}
        </div>
        <p className="mt-3 text-xs text-ct-muted">本实例为单用户,所有用量均来自你本人。阈值超限后将在 get_llm 入口熔断,出题降级至 static_pool。</p>
      </section>
      <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
        <h3 className="mb-3 text-sm font-semibold text-ct-text">预警事件</h3>
        <div className="space-y-0">
          {data.alerts.length === 0 && <p className="text-xs text-ct-success">无预警,成本控制健康 ✓</p>}
          {data.alerts.map((a, i) => (
            <div key={i} className="flex items-start gap-2 border-b border-ct-border/50 py-2.5">
              <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${a.level === 'error' ? 'bg-ct-error-bg text-ct-error' : 'bg-ct-warn-bg text-ct-warn'}`}>
                {a.level === 'error' ? '严重' : '提醒'}
              </span>
              <div>
                <div className="text-xs text-ct-text">{a.title}</div>
                <div className="mt-0.5 text-xs text-ct-muted">{a.detail}</div>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

// ── Tab: 调用明细 ──
function DetailTab({ password, range }: { password: string; range: RangeKey }) {
  const { from, to } = rangeDates(range);
  const { data, loading, error } = useFetch<{ rows: {
    ts: string; session_id: string; purpose: string; model_alias: string;
    prompt_tokens: number; completion_tokens: number; cache_read_tokens: number; cost: number; latency_ms: number;
  }[] }>('/admin/token/usage', { password, from_date: from, to_date: to, limit: 200 }, [password, range]);

  // 导出走 POST(密码在 body,不进 URL/日志/Referer);收到 blob 后触发下载。
  const handleExport = useCallback(async () => {
    try {
      const res = await fetch(BASE + '/admin/token/usage/export', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password, from_date: from, to_date: to, limit: 5000 }),
      });
      if (!res.ok) throw new Error('导出失败(' + res.status + ')');
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'token_usage.csv'; document.body.appendChild(a);
      a.click(); a.remove(); URL.revokeObjectURL(url);
    } catch (_e) {
      // 导出失败不影响明细浏览
    }
  }, [password, from, to]);

  if (loading) return <Center><Spinner />加载中…</Center>;
  if (error) return <Center><ErrorMsg>{error}</ErrorMsg></Center>;
  if (!data || data.rows.length === 0) return <Center><Empty>暂无数据</Empty></Center>;

  return (
    <section className="rounded-xl border border-ct-border bg-ct-surface p-5">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-ct-text">调用明细(token_usage 明细表)</h3>
        <button type="button" onClick={handleExport} className="rounded-lg bg-ct-accent px-3 py-1.5 text-xs font-medium text-white hover:opacity-90">⬇ 导出 CSV</button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-xs text-ct-muted">
              <th className="py-2 pr-3 font-medium">时间</th><th className="py-2 pr-3 font-medium">Session</th>
              <th className="py-2 pr-3 font-medium">用途</th><th className="py-2 pr-3 font-medium">模型</th>
              <th className="py-2 pr-3 font-medium">输入</th><th className="py-2 pr-3 font-medium">输出</th>
              <th className="py-2 pr-3 font-medium">缓存读</th><th className="py-2 pr-3 font-medium">成本</th>
              <th className="py-2 pr-3 font-medium">延迟(ms)</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ct-border/50">
            {data.rows.map((r, i) => (
              <tr key={i} className="hover:bg-ct-surface-secondary">
                <td className="py-2 pr-3 font-mono text-xs text-ct-muted">{r.ts}</td>
                <td className="py-2 pr-3 font-mono text-ct-text">{r.session_id}</td>
                <td className="py-2 pr-3 font-mono font-medium text-ct-text">{r.purpose}</td>
                <td className="py-2 pr-3"><span className="rounded bg-ct-hover px-1.5 py-0.5 text-xs text-ct-muted">{r.model_alias}</span></td>
                <td className="py-2 pr-3 font-mono text-ct-text">{fmtInt(r.prompt_tokens)}</td>
                <td className="py-2 pr-3 font-mono text-ct-text">{fmtInt(r.completion_tokens)}</td>
                <td className="py-2 pr-3 font-mono text-ct-success">{fmtInt(r.cache_read_tokens)}</td>
                <td className="py-2 pr-3 font-mono font-medium text-ct-text">{fmtMoney(r.cost)}</td>
                <td className="py-2 pr-3 font-mono text-xs text-ct-muted">{fmtInt(r.latency_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ── layout atoms ──
function Center({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-1 items-center justify-center p-6 text-sm text-ct-muted">{children}</div>;
}
function Spinner() {
  return <span className="mr-2 inline-block h-4 w-4 animate-spin rounded-full border-2 border-ct-accent border-t-transparent" />;
}
function ErrorMsg({ children }: { children: React.ReactNode }) {
  return <span className="text-ct-error">{children}</span>;
}
function Empty({ children }: { children: React.ReactNode }) {
  return <span className="text-ct-muted">{children}</span>;
}

// ── main ──
export default function CostCenter({ adminToken }: { adminToken: string }) {
  const [tab, setTab] = useState<CostTab>('overview');
  const [range, setRange] = useState<RangeKey>('30d');
  const [model, setModel] = useState<ModelKey>('全部');

  const renderTab = useCallback(() => {
    const p = model === '全部' ? '' : model; // 后端:空=全部
    switch (tab) {
      case 'overview': return <OverviewTab password={adminToken} range={range} model={p} />;
      case 'purpose': return <PurposeTab password={adminToken} range={range} model={p} />;
      case 'cache': return <CacheTab password={adminToken} range={range} model={p} />;
      case 'budget': return <BudgetTab password={adminToken} />;
      case 'detail': return <DetailTab password={adminToken} range={range} />;
    }
  }, [tab, range, model, adminToken]);

  return (
    <div className="flex h-full flex-col">
      {/* 子标签 + 范围选择 + 模型选择 */}
      <div className="flex flex-wrap items-center gap-3 border-b border-ct-border px-4 py-3">
        <div className="flex gap-1 rounded bg-ct-input p-0.5">
          {TABS.map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`rounded px-3 py-1 text-xs font-medium transition ${tab === t.id ? 'bg-ct-accent text-white' : 'text-ct-muted hover:text-ct-text'}`}>
              {t.label}
            </button>
          ))}
        </div>
        {tab !== 'budget' && (
          <div className="flex gap-2">
            <div className="flex gap-1 rounded bg-ct-input p-0.5">
              {RANGES.map((r) => (
                <button key={r.id} onClick={() => setRange(r.id)}
                  className={`rounded px-2.5 py-1 text-xs font-medium transition ${range === r.id ? 'bg-ct-accent text-white' : 'text-ct-muted hover:text-ct-text'}`}>
                  {r.label}
                </button>
              ))}
            </div>
            <div className="flex gap-1 rounded bg-ct-input p-0.5">
              {MODELS.map((m) => (
                <button key={m.id} onClick={() => setModel(m.id)}
                  className={`rounded px-2.5 py-1 text-xs font-medium transition ${model === m.id ? 'bg-ct-accent text-white' : 'text-ct-muted hover:text-ct-text'}`}>
                  {m.label}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {renderTab()}
      </div>
    </div>
  );
}
