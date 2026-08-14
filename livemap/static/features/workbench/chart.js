// The ONE vendored dep (uPlot 1.6.32), injected on first use so the map path never pays for it.
let loading = null;

export function ensureUPlot() {
  if (loading) return loading;
  loading = new Promise((resolve, reject) => {
    if (globalThis.uPlot) return resolve(globalThis.uPlot);
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = "vendor/uplot/uPlot.min.css";
    const s = document.createElement("script");
    s.src = "vendor/uplot/uPlot.iife.min.js";
    s.onload = () => (globalThis.uPlot ? resolve(globalThis.uPlot) : reject(new Error("uplot missing")));
    s.onerror = () => reject(new Error("uplot unreachable"));
    document.head.append(link, s);
  });
  return loading;
}

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
const AXIS = {
  stroke: HUE.dim,
  grid: { stroke: HUE.grid, width: 1 },
  ticks: { stroke: HUE.grid, width: 1 },
  font: "10px 'Spline Sans Mono', ui-monospace, monospace",
};

// uPlot holds every live instance in a module-global rect-sync Set fed by window resize/scroll
// listeners — only .destroy() removes it, so a discarded reference leaks the whole plot.
const live = new Set();

export function destroyCharts() {
  for (const u of live) {
    try {
      u.destroy();
    } catch {
      /* already torn down */
    }
  }
  live.clear();
}

export async function makeChart(host, opts, data) {
  const uPlot = await ensureUPlot();
  // a view switch during the vendor load already ran destroyCharts() — a plot built now would sit
  // on a detached host, registered but invisible, until the next navigation
  if (!host.isConnected) return null;
  const axes = (opts.axes || [{}, {}]).map((a) => ({ ...AXIS, ...a }));
  const u = new uPlot({ legend: { show: false }, cursor: { y: false }, ...opts, axes }, data, host);
  live.add(u);
  return u;
}

// The rail is a fixed-width column, so a chart sizes off its host rather than off the viewport.
const box = (host, h) => ({ width: host.clientWidth || 288, height: host.clientHeight || h });
export const SERIES_HUES = [HUE.settled, HUE.estimated, HUE.provisional, HUE.none, HUE.ink];

export async function spark(host, xs, ys, color) {
  return makeChart(
    host,
    {
      ...box(host, 28),
      scales: { x: { time: false } },
      axes: [{ show: false }, { show: false }],
      cursor: { show: false },
      series: [{}, { stroke: color || HUE.settled, width: 1, points: { show: false } }],
    },
    [xs, ys],
  );
}

// s.dash is optional (uPlot ignores undefined) — it carries the p90 arm of a paired p50/p90 series.
export async function line(host, xs, seriesArr) {
  return makeChart(
    host,
    {
      ...box(host, 160),
      series: [
        {},
        ...seriesArr.map((s) => ({
          label: s.label, stroke: s.color, width: 1.25, dash: s.dash, points: { show: false },
        })),
      ],
    },
    [xs, ...seriesArr.map((s) => s.ys)],
  );
}

// Categorical bars: x is a bin INDEX, so the axis prints the caller's labels rather than a scale.
export async function bars(host, ys, labels, color) {
  const uPlot = await ensureUPlot();
  const paths = uPlot.paths.bars({ size: [0.82, 26] });
  return makeChart(
    host,
    {
      ...box(host, 130),
      scales: { x: { time: false } },
      axes: [{ values: (_u, vals) => vals.map((v) => labels[v] ?? "") }, {}],
      series: [{}, { stroke: color, fill: `${color}55`, width: 1, paths, points: { show: false } }],
    },
    [ys.map((_, i) => i), ys],
  );
}

export async function stackedBars(host, xs, seriesArr) {
  const uPlot = await ensureUPlot();
  const acc = new Array(xs.length).fill(0);
  const cum = seriesArr.map((s) => xs.map((_, i) => (acc[i] += Number(s.ys[i]) || 0)));
  // each band clips its series' fill down to the series below, so the cumulative lines read as a stack
  const bands = seriesArr.slice(1).map((_, i) => ({ series: [i + 2, i + 1] }));
  const bars = uPlot.paths.bars({ size: [0.86, 22] });
  return makeChart(
    host,
    {
      ...box(host, 160),
      bands,
      series: [
        {},
        ...seriesArr.map((s) => ({
          label: s.label, stroke: s.color, fill: `${s.color}55`, width: 1, paths: bars, points: { show: false },
        })),
      ],
    },
    [xs, ...cum],
  );
}
