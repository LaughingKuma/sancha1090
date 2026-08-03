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

export async function makeChart(host, opts, data) {
  const uPlot = await ensureUPlot();
  const axes = (opts.axes || [{}, {}]).map((a) => ({ ...AXIS, ...a }));
  return new uPlot({ legend: { show: false }, cursor: { y: false }, ...opts, axes }, data, host);
}
