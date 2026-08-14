import { esc, panel, rangeParams } from "../shell.js?v=6.44";
import { fetchEstimates } from "../data.js?v=6.44";
import { line, SERIES_HUES } from "../chart.js?v=6.44";

const MIX_DIMS = [
  ["skip", "skips"],
  ["segment_kind", "segment kind"],
  ["uncertainty_bin", "uncertainty bin"],
];
// the rail is a column, not a report — a long tail scrolls the whole view out of reach
const MIX_ROWS = 12;
const fmt = (n) => Number(n || 0).toLocaleString();
const km = (v) => (v == null ? "—" : `${v.toFixed(2)} km`);
const daySecs = (d) => Date.parse(`${d}T00:00:00Z`) / 1000;
// the hash is an opaque 20-digit key — the head identifies the era, the title carries all of it
const shortCfg = (h) => String(h || "").slice(0, 8);

// a panel's numbers are the payload — a sketch that can't draw leaves an empty host, never an error
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

function headline(host, eras) {
  section(host, "config eras");
  if (!eras.length) {
    host.insertAdjacentHTML("beforeend", '<div class="wb-empty">no scored estimates in range</div>');
    return;
  }
  // first row is the era still in force (the query orders latest-last-seen first)
  const wrap = document.createElement("div");
  wrap.innerHTML = eras
    .map(
      (e, i) =>
        `<div class="wb-era${i === 0 ? " wb-era-cur" : ""}" title="${esc(`config ${e.configHash}`)}">` +
        `<span class="wb-row-main"><span class="wb-name">${esc(shortCfg(e.configHash))}</span>` +
        `<span class="wb-era-p">p50 ${esc(km(e.p50Km))}</span>` +
        `<span class="wb-era-p wb-era-p90">p90 ${esc(km(e.p90Km))}</span></span>` +
        `<span class="wb-row-sub"><span>${esc(`${e.firstDay} → ${e.lastDay}`)}</span>` +
        `<span>${esc(`n=${fmt(e.n)}`)}</span></span></div>`,
    )
    .join("");
  host.appendChild(wrap);
}

function chart(host, daily) {
  const days = [...new Set(daily.map((r) => r.day))].sort();
  if (days.length < 2) return;
  const configs = [...new Set(daily.map((r) => r.configHash))];
  const at = new Map(daily.map((r) => [`${r.configHash}|${r.day}`, r]));
  // a config's series is null outside its own era, so an instrument change draws as a break
  const arr = [];
  for (const [i, cfg] of configs.entries()) {
    const color = SERIES_HUES[i % SERIES_HUES.length];
    const pick = (f) => days.map((d) => (at.has(`${cfg}|${d}`) ? at.get(`${cfg}|${d}`)[f] : null));
    arr.push({ label: `${shortCfg(cfg)} p50`, ys: pick("p50Km"), color });
    arr.push({ label: `${shortCfg(cfg)} p90`, ys: pick("p90Km"), color, dash: [4, 3] });
  }
  const el = document.createElement("div");
  el.className = "wb-chart";
  host.appendChild(el);
  draw(() => line(el, days.map(daySecs), arr));
  host.insertAdjacentHTML(
    "beforeend",
    '<div class="wb-note">solid p50 · dashed p90 · one colour per config era</div>',
  );
}

function mixPanel(host, dim, title, rowsIn) {
  section(host, title);
  if (!rowsIn.length) {
    host.insertAdjacentHTML("beforeend", '<div class="wb-empty">nothing logged</div>');
    return;
  }
  const shown = rowsIn.slice(0, MIX_ROWS);
  const wrap = document.createElement("div");
  wrap.className = `wb-mix wb-mix-${esc(dim)}`;
  wrap.innerHTML =
    shown
      .map(
        (r) =>
          `<div class="wb-kv"><span class="wb-kv-k">${esc(r.value)}</span>` +
          `<span class="wb-kv-p">${esc(r.producer || "—")}</span>` +
          `<span class="wb-kv-n">${esc(fmt(r.n))}</span></div>`,
      )
      .join("") +
    (rowsIn.length > shown.length
      ? `<div class="wb-note">+${esc(fmt(rowsIn.length - shown.length))} more</div>`
      : "");
  host.appendChild(wrap);
}

function outcomes(host, o, split) {
  section(host, "logging stream");
  // raw logged rows, not the deduped scored pool — the two counts answer different questions
  const cells = [
    { k: "settled", v: fmt(o.settled) },
    { k: "awaiting", v: fmt(o.awaiting) },
    { k: "ambiguous", v: fmt(o.ambiguous) },
    { k: "prov in", v: fmt(split.provisional) },
    { k: "settled in", v: fmt(split.settled) },
  ];
  host.insertAdjacentHTML(
    "beforeend",
    '<div class="wb-strip">' +
      cells
        .map(
          (c) =>
            `<span class="wb-cell"><span class="wb-cell-v">${esc(c.v)}</span>` +
            `<span class="wb-cell-k">${esc(c.k)}</span></span>`,
        )
        .join("") +
      "</div>",
  );
}

export async function render(host) {
  const el = panel(host);
  const p = await fetchEstimates(rangeParams());
  if (!el.isConnected) return;
  if (!p) return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">estimates unavailable</div>');
  if (!p.available)
    return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">estimate ledger not deployed</div>');
  headline(el, p.headline);
  chart(el, p.daily);
  outcomes(el, p.outcomes, p.inputSplit);
  if (!p.mix.available) {
    section(el, "mix");
    el.insertAdjacentHTML("beforeend", '<div class="wb-empty">breakdown mart not deployed</div>');
    return;
  }
  for (const [dim, title] of MIX_DIMS) mixPanel(el, dim, title, p.mix[dim]);
  // the breakdown mart is UTC-day grain, the series above is JST — say so rather than hide the skew
  el.insertAdjacentHTML(
    "beforeend",
    '<div class="wb-note">mix counts are UTC-day grain; the series above is JST</div>',
  );
}
