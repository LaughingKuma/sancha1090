import { esc, panel, navigate, rangeParams } from "../shell.js";
import { fetchSummary, fetchFlags } from "../data.js";
import { spark, stackedBars, HUE } from "../chart.js";
import { renderFlagRows } from "./flags.js";

const TIER_KEYS = ["settled", "estimated", "provisional", "none", "unknown"];
const TIER_HUE = {
  settled: HUE.settled, estimated: HUE.estimated, provisional: HUE.provisional,
  none: HUE.none, unknown: HUE.dim,
};
// A strip number counts the whole window, so its doorway must drop every scope the log and drill
// still carry — otherwise an unfiltered headline opens a filtered list.
const SCOPE_RESET = {
  airline: null, service: null, od: null, hex: null, apt: null, type: null, mil: false, page: 1,
};
const fmt = (n) => Number(n || 0).toLocaleString();
const daySecs = (d) => Date.parse(`${d}T00:00:00Z`) / 1000;
const deltaText = (d) =>
  d == null ? "—" : `${d > 0 ? "+" : d < 0 ? "−" : ""}${Math.abs(d).toFixed(1)}%`;

function microbarHTML(tiers) {
  const parts = Object.entries(tiers.mix || {}).filter(([, n]) => n);
  if (!tiers.available || !parts.length) return '<span class="wb-cell-v">—</span>';
  const tot = parts.reduce((a, [, n]) => a + n, 0);
  const title = parts.map(([k, n]) => `${k} ${fmt(n)}`).join(" · ");
  return (
    `<span class="wb-microbar" title="${esc(title)}">` +
    parts.map(([k, n]) => `<span class="t-${esc(k)}" style="width:${((n / tot) * 100).toFixed(2)}%"></span>`).join("") +
    "</span>"
  );
}

function strip(host, s) {
  const cells = [
    { k: "flights", v: fmt(s.flights), patch: { view: "log", ...SCOPE_RESET } },
    // services/aircraft stay plain: no view serves a window-scoped list of either, and a doorway
    // whose destination disagrees with its number is worse than no doorway (review round 2)
    { k: "services", v: fmt(s.services) },
    { k: "aircraft", v: fmt(s.aircraft) },
    { k: "flagged", v: s.flags.available ? fmt(s.flags.flagged) : "—", patch: { view: "flags", flagClass: null, page: 1 } },
    { k: "est err", v: s.est.available && s.est.errP50Km != null ? `${s.est.errP50Km.toFixed(2)} km` : "—" },
  ];
  const bar = document.createElement("div");
  bar.className = "wb-strip";
  bar.innerHTML =
    cells
      .map(
        (c, i) =>
          (c.patch ? `<button type="button" class="wb-cell wb-cell-go" data-i="${i}">` : '<span class="wb-cell">') +
          `<span class="wb-cell-v">${esc(c.v)}</span><span class="wb-cell-k">${esc(c.k)}</span>` +
          (c.patch ? "</button>" : "</span>"),
      )
      .join("") +
    `<span class="wb-cell">${microbarHTML(s.tiers)}<span class="wb-cell-k">tiers</span></span>`;
  bar.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-cell-go");
    if (btn) navigate(cells[Number(btn.dataset.i)].patch);
  });
  host.appendChild(bar);
}

function panelBody(host, title, patch) {
  const box = document.createElement("div");
  box.className = "wb-panel";
  box.innerHTML =
    `<button type="button" class="wb-panel-head">${esc(title)}<span class="wb-panel-go">▸</span></button>`;
  box.querySelector(".wb-panel-head").addEventListener("click", () => navigate(patch));
  const body = document.createElement("div");
  body.className = "wb-panel-body";
  box.appendChild(body);
  host.appendChild(box);
  return body;
}

function chartHost(host, cls) {
  const el = document.createElement("div");
  el.className = cls;
  host.appendChild(el);
  return el;
}

// a panel's numbers are the payload — a sketch that can't draw leaves an empty host, never an error
async function draw(fn) {
  try {
    await fn();
  } catch {
    /* uPlot unreachable or a degenerate window */
  }
}

function classChips(host, classes) {
  const entries = Object.entries(classes || {}).filter(([, n]) => n);
  if (!entries.length) return;
  const bar = document.createElement("div");
  bar.className = "wb-classes";
  bar.innerHTML = entries
    .map(
      ([c, n], i) =>
        `<button type="button" class="wb-chip" data-i="${i}">` +
        `${esc(c.replaceAll("_", " "))} ${esc(fmt(n))}</button>`,
    )
    .join("");
  bar.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-chip");
    if (btn) navigate({ view: "flags", flagClass: entries[Number(btn.dataset.i)][0], page: 1 });
  });
  host.appendChild(bar);
}

function flagsPanel(host, f) {
  const body = panelBody(host, "flags", { view: "flags", flagClass: null, page: 1 });
  if (!f) return body.insertAdjacentHTML("beforeend", '<div class="wb-empty">flags unavailable</div>');
  if (!f.available) return body.insertAdjacentHTML("beforeend", '<div class="wb-empty">flags mart not deployed</div>');
  renderFlagRows(body, f.rows);
  classChips(body, f.classes);
}

function moverRows(host, movers) {
  const top = movers.slice(0, 3);
  if (!top.length) return host.insertAdjacentHTML("beforeend", '<div class="wb-empty">no routes in range</div>');
  const wrap = document.createElement("div");
  wrap.innerHTML = top
    .map(
      (r, i) =>
        `<button type="button" class="wb-row wb-rank" data-idx="${i}">` +
        `<span class="wb-name">${esc(r.key || "—")}</span>` +
        `<span class="wb-n">${esc(fmt(r.n))}</span>` +
        `<span class="wb-delta">${esc(deltaText(r.deltaPct))}</span></button>`,
    )
    .join("");
  wrap.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-row");
    if (!btn) return;
    const r = top[Number(btn.dataset.idx)];
    navigate({ view: "log", ...SCOPE_RESET, od: r.key });
  });
  host.appendChild(wrap);
}

function trendsPanel(host, s) {
  const body = panelBody(host, "trends", { view: "trends", page: 1 });
  const ys = s.daily.map(([, n]) => n);
  if (ys.length > 1) {
    const el = chartHost(body, "wb-spark");
    draw(() => spark(el, ys.map((_, i) => i), ys, HUE.settled));
  }
  moverRows(body, s.movers);
}

function estPanel(host, s) {
  // view navigation only — the two new views carry the rail's range, never a list scope
  const body = panelBody(host, "estimates", { view: "estimates", page: 1 });
  if (!s.est.available || s.est.errP50Km == null)
    return body.insertAdjacentHTML("beforeend", '<div class="wb-empty">—</div>');
  const ys = s.est.daily.map(([, p]) => p);
  if (ys.length > 1) {
    const el = chartHost(body, "wb-spark");
    draw(() => spark(el, ys.map((_, i) => i), ys, HUE.estimated));
  }
  body.insertAdjacentHTML(
    "beforeend",
    `<div class="wb-note">${esc(`p50 ${s.est.errP50Km.toFixed(2)} km · n=${fmt(s.est.n)}`)}</div>`,
  );
}

function coveragePanel(host, s) {
  const body = panelBody(host, "coverage", { view: "coverage", page: 1 });
  if (!s.tiers.available || !s.tiers.daily.length)
    return body.insertAdjacentHTML("beforeend", '<div class="wb-empty">—</div>');
  const el = chartHost(body, "wb-chart");
  const arr = TIER_KEYS.filter((k) => s.tiers.daily.some(([, m]) => m[k])).map((k) => ({
    label: k,
    ys: s.tiers.daily.map(([, m]) => m[k] || 0),
    color: TIER_HUE[k],
  }));
  draw(() => stackedBars(el, s.tiers.daily.map(([d]) => daySecs(d)), arr));
}

export async function render(host) {
  const el = panel(host);
  const range = rangeParams();
  const [s, f] = await Promise.all([fetchSummary(range), fetchFlags({ ...range, limit: 5 })]);
  if (!el.isConnected) return;
  if (!s) return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">overview unavailable</div>');
  strip(el, s);
  flagsPanel(el, f);
  trendsPanel(el, s);
  estPanel(el, s);
  coveragePanel(el, s);
}
