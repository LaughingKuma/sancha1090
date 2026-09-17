import uPlot from "uplot";
import { useLayoutEffect, useRef } from "preact/hooks";

// HUD palette — charts must read as the same instrument as the rail, not a BI skin.
export const HUE = {
  settled: "#ffb000",
  estimated: "#b08ae6",
  provisional: "#4ea2ae",
  none: "#7e93a8",
  ink: "#e8eef5",
  dim: "#7e93a8",
  grid: "rgba(120,170,210,0.10)",
};
// tier key → series colour; "unknown" is the dim ink, never a fifth hue
export const TIER_HUE: Record<string, string> = {
  settled: HUE.settled, estimated: HUE.estimated, provisional: HUE.provisional,
  none: HUE.none, unknown: HUE.dim,
};
const AXIS: uPlot.Axis = {
  stroke: HUE.dim,
  grid: { stroke: HUE.grid, width: 1 },
  ticks: { stroke: HUE.grid, width: 1 },
  font: "10px 'Spline Sans Mono', ui-monospace, monospace",
};
export const SERIES_HUES = [HUE.settled, HUE.estimated, HUE.provisional, HUE.none, HUE.ink];

// the measured box is the component's, never the builder's: options that carry no size keep one identity
// across re-renders, and identity is what decides create-vs-setData
export type ChartOpts = Omit<uPlot.Options, "width" | "height">;
export interface SeriesMeta { label: string; color: string; dash?: number[] }
export type Values = (number | null)[];

// a panel's numbers are the payload — a sketch that can't draw leaves an empty host, never an error
function build(el: HTMLElement, opts: ChartOpts, h: number, data: uPlot.AlignedData): uPlot | null {
  try {
    return new uPlot(
      {
        legend: { show: false },
        cursor: { y: false },
        ...opts,
        axes: (opts.axes ?? [{}, {}]).map((a) => ({ ...AXIS, ...a })),
        width: el.clientWidth || 288,
        height: el.clientHeight || h,
      },
      data,
      el,
    );
  } catch {
    return null; // a degenerate window
  }
}

export function Chart(
  { class: cls, h, opts, data }: { class: string; h: number; opts: ChartOpts; data: uPlot.AlignedData },
) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  // the plot outlives a data-only change, so the newest data reaches a rebuild through this ref
  const latest = useRef(data);
  useLayoutEffect(() => {
    latest.current = data;
    const u = plot.current;
    if (u && u.data !== data) u.setData(data);
  }, [data]);
  useLayoutEffect(() => {
    const el = host.current!;
    const u = build(el, opts, h, latest.current);
    if (!u) return;
    plot.current = u;
    const ro = new ResizeObserver(() => {
      const width = el.clientWidth || 288;
      const height = el.clientHeight || h;
      if (width === u.width && height === u.height) return;
      u.setSize({ width, height });
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      u.destroy();
      plot.current = null;
    };
  }, [opts, h]);
  return <div class={cls} ref={host} />;
}

export const sparkOpts = (color: string): ChartOpts => ({
  scales: { x: { time: false } },
  axes: [{ show: false }, { show: false }],
  cursor: { show: false },
  series: [{}, { stroke: color, width: 1, points: { show: false } }],
});
export const sparkData = (ys: Values): uPlot.AlignedData => [ys.map((_, i) => i), ys];

// s.dash is optional (uPlot ignores undefined) — it carries the p90 arm of a paired p50/p90 series.
export const lineOpts = (series: SeriesMeta[]): ChartOpts => ({
  series: [
    {},
    ...series.map((s) => ({
      label: s.label, stroke: s.color, width: 1.25, dash: s.dash, points: { show: false },
    })),
  ],
});
export const lineData = (xs: number[], ys: Values[]): uPlot.AlignedData => [xs, ...ys];

// Categorical bars: x is a bin INDEX, so the axis prints the caller's labels rather than a scale.
export const barsOpts = (labels: string[], color: string): ChartOpts => ({
  scales: { x: { time: false } },
  axes: [{ values: (_u, vals) => vals.map((v) => labels[v] ?? "") }, {}],
  series: [
    {},
    {
      stroke: color, fill: `${color}55`, width: 1, points: { show: false },
      paths: uPlot.paths.bars!({ size: [0.82, 26] }),
    },
  ],
});
export const barsData = (ys: Values): uPlot.AlignedData => [ys.map((_, i) => i), ys];

export const stackedOpts = (series: SeriesMeta[]): ChartOpts => {
  const paths = uPlot.paths.bars!({ size: [0.86, 22] });
  return {
    // each band clips its series' fill down to the series below, so the cumulative lines read as a stack
    bands: series.slice(1).map((_, i) => ({ series: [i + 2, i + 1] as uPlot.Band.Bounds })),
    series: [
      {},
      ...series.map((s) => ({
        label: s.label, stroke: s.color, fill: `${s.color}55`, width: 1, points: { show: false }, paths,
      })),
    ],
  };
};
export const stackedData = (xs: number[], ys: number[][]): uPlot.AlignedData => {
  const acc = new Array(xs.length).fill(0);
  return [xs, ...ys.map((y) => xs.map((_, i) => (acc[i] += Number(y[i]) || 0)))];
};
