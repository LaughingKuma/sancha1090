import { esc, panel, rangeParams } from "../shell.js?v=6.44";
import { fetchCoverage } from "../data.js?v=6.44";
import { line, bars, stackedBars, HUE } from "../chart.js?v=6.44";

const TIER_KEYS = ["settled", "estimated", "provisional", "none", "unknown"];
const TIER_HUE = {
  settled: HUE.settled, estimated: HUE.estimated, provisional: HUE.provisional,
  none: HUE.none, unknown: HUE.dim,
};
const fmt = (n) => Number(n || 0).toLocaleString();
const daySecs = (d) => Date.parse(`${d}T00:00:00Z`) / 1000;
const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);

// Labels for the fixed bin edges (backend ruling: 900 s is the tier seam, so it is an exact edge).
const binLabel = (b) => {
  const unit = (s) => (s % 3600 === 0 ? "h" : "m");
  const val = (s) => (s % 3600 === 0 ? s / 3600 : s / 60);
  if (b.ge === 0) return `<${val(b.lt)}${unit(b.lt)}`;
  if (b.lt == null) return `≥${val(b.ge)}${unit(b.ge)}`; // ge is inclusive: exact-12h gaps land here
  // one unit on both sides reads as a single span: "1–5m", never "1m–5m"
  return unit(b.ge) === unit(b.lt)
    ? `${val(b.ge)}–${val(b.lt)}${unit(b.lt)}`
    : `${val(b.ge)}${unit(b.ge)}–${val(b.lt)}${unit(b.lt)}`;
};

async function draw(fn) {
  try {
    await fn();
  } catch {
    /* uPlot unreachable or a degenerate window */
  }
}

function section(host, title) {
  host.insertAdjacentHTML("beforeend", `<div class="wb-sect">${esc(title)}</div>`);
}

function chartHost(host, cls) {
  const el = document.createElement("div");
  el.className = cls;
  host.appendChild(el);
  return el;
}

function tierMix(host, daily) {
  section(host, "tier mix per day");
  if (!daily.length) {
    host.insertAdjacentHTML("beforeend", '<div class="wb-empty">no flights in range</div>');
    return;
  }
  const arr = TIER_KEYS.filter((k) => daily.some(([, m]) => m[k])).map((k) => ({
    label: k,
    ys: daily.map(([, m]) => m[k] || 0),
    color: TIER_HUE[k],
  }));
  const el = chartHost(host, "wb-chart");
  draw(() => stackedBars(el, daily.map(([d]) => daySecs(d)), arr));
  const totals = {};
  for (const [, m] of daily) for (const [k, n] of Object.entries(m)) totals[k] = (totals[k] || 0) + n;
  const legend = TIER_KEYS.filter((k) => totals[k]);
  host.insertAdjacentHTML(
    "beforeend",
    '<div class="wb-tmix wb-legend">' +
      legend.map((k) => `<span class="t-${esc(k)}">${esc(k)} ${esc(fmt(totals[k]))}</span>`).join("") +
      "</div>",
  );
}

function gapHist(host, binsIn) {
  section(host, "largest gap");
  if (!binsIn.some((b) => b.n)) {
    host.insertAdjacentHTML("beforeend", '<div class="wb-empty">no measured gaps in range</div>');
    return;
  }
  const el = chartHost(host, "wb-chart");
  draw(() => bars(el, binsIn.map((b) => b.n), binsIn.map(binLabel), HUE.settled));
  // the 15m edge is the settled/estimated tier seam — name it so the shape is readable, not decorative
  host.insertAdjacentHTML(
    "beforeend",
    '<div class="wb-note">15m is the settled/estimated tier seam</div>',
  );
}

function observed(host, obs) {
  section(host, "observed fraction (median)");
  if (!obs.length) {
    host.insertAdjacentHTML("beforeend", '<div class="wb-empty">no measured coverage in range</div>');
    return;
  }
  if (obs.length > 1) {
    const el = chartHost(host, "wb-chart");
    draw(() =>
      line(el, obs.map((r) => daySecs(r.day)), [
        { label: "median", ys: obs.map((r) => r.median), color: HUE.provisional },
      ]),
    );
  }
  const last = obs[obs.length - 1];
  host.insertAdjacentHTML(
    "beforeend",
    `<div class="wb-note">${esc(`${last.day} ${pct(last.median)} · n=${fmt(last.n)}`)}</div>`,
  );
}

export async function render(host) {
  const el = panel(host);
  const p = await fetchCoverage(rangeParams());
  if (!el.isConnected) return;
  if (!p) return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">coverage unavailable</div>');
  if (!p.available)
    return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">tier mart not deployed</div>');
  tierMix(el, p.tierDaily);
  gapHist(el, p.gapBins);
  observed(el, p.observed);
}
