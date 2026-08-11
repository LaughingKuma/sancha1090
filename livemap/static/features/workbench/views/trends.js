import {
  W, esc, panel, navigate, rangeParams, renderRows, renderPager, openAirline, openAirport,
} from "../shell.js?v=6.42";
import { fetchTrends } from "../data.js?v=6.42";
import { line, SERIES_HUES } from "../chart.js?v=6.42";

const DIMS = ["route", "airline", "airport"];
// rank rows are wide (key · n · aircraft · Δ), so trends pages at 20 while the lists page at 50
const TREND_LIMIT = 20;
const daySecs = (d) => Date.parse(`${d}T00:00:00Z`) / 1000;
const deltaText = (d) =>
  d == null ? "—" : `${d > 0 ? "+" : d < 0 ? "−" : ""}${Math.abs(d).toFixed(1)}%`;

function dims(host) {
  const bar = document.createElement("div");
  bar.className = "wb-dims";
  bar.innerHTML = DIMS.map(
    (d) => `<button type="button" class="wb-chip" data-dim="${d}" aria-pressed="${String(W.dim === d)}">${d}</button>`,
  ).join("");
  bar.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-chip[data-dim]");
    if (btn && btn.dataset.dim !== W.dim) navigate({ dim: btn.dataset.dim, page: 1 });
  });
  host.appendChild(bar);
}

async function chart(host, series) {
  try {
    const top = series.slice(0, 5);
    // the union of the drawn keys' days: a key idle on a day reads as a gap, not as a zero
    const days = [...new Set(top.flatMap((s) => s.points.map(([d]) => d)))].sort();
    if (!days.length) return;
    const arr = top.map((s, i) => {
      const by = new Map(s.points);
      return {
        label: s.key,
        ys: days.map((d) => (by.has(d) ? by.get(d) : null)),
        color: SERIES_HUES[i % SERIES_HUES.length],
      };
    });
    await line(host, days.map(daySecs), arr);
  } catch {
    // uPlot unreachable or a degenerate window — the rank table below is the real payload
  }
}

const rankHTML = (r, i) =>
  `<button type="button" class="wb-row wb-stack wb-rank" data-idx="${i}">` +
  `<span class="wb-row-main"><span class="wb-name">${esc(r.key || "—")}</span>` +
  `<span class="wb-n">${esc(r.n.toLocaleString())}</span></span>` +
  `<span class="wb-row-sub"><span>${esc(`${r.distinctAircraft.toLocaleString()} aircraft`)}</span>` +
  `<span class="wb-delta">${esc(deltaText(r.deltaPct))}</span></span></button>`;

function pick(r) {
  if (W.dim === "airline") return openAirline(r.key);
  if (W.dim === "airport") return openAirport(r.key);
  // the rank row counts the whole window, so every leftover log scope has to go with it
  navigate({ view: "log", od: r.key, airline: null, service: null, hex: null, apt: null,
             type: null, mil: false, page: 1 });
}

export async function render(host) {
  const el = panel(host);
  dims(el);
  const p = await fetchTrends({
    ...rangeParams(),
    dim: W.dim,
    limit: TREND_LIMIT,
    offset: (W.page - 1) * TREND_LIMIT,
  });
  if (!el.isConnected) return;
  if (!p) return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">trends unavailable</div>');
  const chartHost = document.createElement("div");
  chartHost.className = "wb-chart";
  el.appendChild(chartHost);
  chart(chartHost, p.series);
  renderRows(el, p.rank, rankHTML, pick, "no trends in range");
  renderPager(el, { rows: p.rank, total: p.total, limit: p.limit, offset: p.offset });
}
