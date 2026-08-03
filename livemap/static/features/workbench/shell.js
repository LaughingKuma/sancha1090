import { readUrl, writeUrl } from "./url.js?v=6.41";
import { enterFocus, exitFocus, dropFocus, isFocused } from "./focus.js?v=6.41";
import { fetchInstances } from "./data.js?v=6.41";

// Callsigns, registrations and airport names are attacker-transmittable and rows are built as HTML.
export function esc(v) {
  return String(v ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Feature-internal state: everything the rail needs beyond the two fields S carries.
export const W = {
  S: null,
  view: "overview",
  range: "30d",
  airline: null,
  service: null,
  od: null,
  inst: null,
  mil: false,
  page: 1,
  hex: null, // airframe scope — set by search and by the instance deep-link fallback
  apt: null, // airport filter (log chip + search's airport bucket)
  type: null,
  milAvailable: true, // flipped by an instances envelope when the tier mart is absent
  rail: null,
  body: null,
  searchHost: null,
  status: null,
};

export const PAGE_LIMIT = 50;
const TIER_LABEL = { settled: "● STL", estimated: "◐ EST", provisional: "○ PRV", none: "· NONE", unknown: "· UNK" };
const TIER_GLYPH = { settled: "●", estimated: "◐", provisional: "○", none: "·" };
const PRESETS = { "7d": 7, "30d": 30, "90d": 90 };
const VIEW_SRC = {
  overview: "./views/overview.js?v=6.41",
  drill: "./views/drill.js?v=6.41",
  log: "./views/log.js?v=6.41",
};

// The server windows on JST calendar days — shift, then read the UTC date parts.
export const jstDay = (ms) => new Date(ms + 9 * 3600 * 1000).toISOString().slice(0, 10);
export const jstDayOf = (ts) => (ts == null ? "" : jstDay(ts * 1000));

export function rangeParams() {
  if (W.range === "all") return {};
  const custom = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/.exec(W.range);
  if (custom) return { day_from: custom[1], day_to: custom[2] };
  const days = PRESETS[W.range] || 30;
  return { day_from: jstDay(Date.now() - (days - 1) * 86400000), day_to: jstDay(Date.now()) };
}

export function setStatus(msg) {
  if (W.status) W.status.textContent = msg;
}

export function tierPill(tier) {
  return `<span class="wb-tier t-${tier}">${TIER_LABEL[tier] || TIER_LABEL.unknown}</span>`;
}

export function tierMixHTML(tiers) {
  const parts = Object.entries(tiers || {}).filter(([, n]) => n);
  if (!parts.length) return "";
  const title = parts.map(([k, n]) => `${k} ${n}`).join(" · ");
  return (
    `<span class="wb-tmix" title="${esc(title)}">` +
    parts.map(([k, n]) => `<span class="t-${k}">${TIER_GLYPH[k] || "·"}${esc(n.toLocaleString())}</span>`).join("") +
    "</span>"
  );
}

export function panel(host) {
  const el = document.createElement("div");
  host.appendChild(el);
  return el; // detached by the next renderView — every await checks isConnected before painting
}

function flashRow(el, msg) {
  setStatus(msg);
  const cell = el && el.isConnected ? el.querySelector(".wb-sub-od") : null;
  if (!cell) return;
  const prev = cell.textContent;
  cell.classList.add("wb-nopath");
  cell.textContent = msg;
  // transient: the row returns to its route after a beat, never growing a second line
  setTimeout(() => {
    if (!cell.isConnected) return;
    cell.textContent = prev;
    cell.classList.remove("wb-nopath");
  }, 2000);
}

// The drawn row keeps the amber active chrome (the ff-active pattern) — claimed on the click, not on
// the response, so the row and the focus bar can never disagree about which instance is the subject.
function markActive(rowEl) {
  for (const el of W.body.querySelectorAll(".wb-active")) {
    el.classList.remove("wb-active");
    el.setAttribute("aria-pressed", "false");
  }
  if (!rowEl) return;
  rowEl.classList.add("wb-active");
  rowEl.setAttribute("aria-pressed", "true");
}

export function focusInstance(inst, rowEl) {
  const flash = (msg) => {
    W.inst = null;
    writeUrl(W, true);
    markActive(null);
    flashRow(rowEl, msg);
  };
  if (inst.tier === "none" || !inst.flightId) return flashRow(rowEl, "no recorded path");
  W.inst = inst.key;
  markActive(rowEl);
  writeUrl(W); // a focus step is its own history entry, so Back leaves focus
  enterFocus(W.S, inst, {
    onExit: () => {
      W.inst = null;
      writeUrl(W, true);
      markActive(null);
    },
    onEmpty: flash,
  });
}

function instanceRowHTML(r, i) {
  const day = r.day || jstDayOf(r.startTs);
  const meta = [r.reg, r.type, r.nPoints == null ? "" : `${r.nPoints.toLocaleString()} pts`]
    .filter(Boolean)
    .join(" · ");
  const on = !!r.key && r.key === W.inst; // a re-render (pager, filter) must keep the focused row lit
  return (
    `<button type="button" class="wb-row wb-stack wb-inst${on ? " wb-active" : ""}" ` +
    `aria-pressed="${on}" data-idx="${i}">` +
    `<span class="wb-row-main"><span class="wb-date">${esc(day.slice(5) || "—")}</span>` +
    `<span class="wb-name">${esc(r.callsign || r.hex || "—")}</span>` +
    (r.mil ? '<span class="wb-tier t-mil">MIL</span>' : "") +
    `<span class="wb-n">${tierPill(r.tier)}</span></span>` +
    `<span class="wb-row-sub"><span class="wb-sub-od">${esc(`${r.o || "?"} → ${r.d || "?"}`)}</span>` +
    `<span>${esc(meta)}</span></span></button>`
  );
}

export function renderInstances(host, list) {
  if (!list.length) {
    host.insertAdjacentHTML("beforeend", '<div class="wb-empty">no instances match</div>');
    return;
  }
  const wrap = document.createElement("div");
  wrap.innerHTML = list.map(instanceRowHTML).join("");
  for (const btn of wrap.querySelectorAll(".wb-inst")) {
    const r = list[Number(btn.dataset.idx)];
    const title = [
      r.hex,
      r.airline,
      r.origin.city || r.origin.icao,
      r.dest.city || r.dest.icao,
      r.gapS == null ? "" : `gap ${r.gapS}s`,
    ]
      .filter(Boolean)
      .join(" · ");
    if (title) btn.title = title;
  }
  wrap.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-inst");
    if (btn) focusInstance(list[Number(btn.dataset.idx)], btn);
  });
  host.appendChild(wrap);
}

export function renderRows(host, items, html, onPick, empty = "nothing here") {
  if (!items.length) {
    host.insertAdjacentHTML("beforeend", `<div class="wb-empty">${esc(empty)}</div>`);
    return;
  }
  const wrap = document.createElement("div");
  wrap.innerHTML = items.map(html).join("");
  wrap.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-row");
    if (btn) onPick(items[Number(btn.dataset.idx)]);
  });
  host.appendChild(wrap);
}

export function renderPager(host, p) {
  if (!p || (p.total <= p.rows.length && W.page === 1)) return;
  const from = p.offset + 1;
  const to = Math.min(p.offset + p.rows.length, p.total);
  const bar = document.createElement("div");
  bar.className = "wb-pager";
  bar.innerHTML =
    '<button type="button" class="wb-chip" data-step="-1">◂ prev</button>' +
    '<button type="button" class="wb-chip" data-step="1">next ▸</button>' +
    `<span class="wb-count">${from}–${to} of ${p.total.toLocaleString()}</span>`;
  const [prev, next] = bar.querySelectorAll("button");
  prev.disabled = W.page <= 1;
  next.disabled = p.offset + p.rows.length >= p.total;
  bar.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-step]");
    if (btn && !btn.disabled) navigate({ page: Math.max(1, W.page + Number(btn.dataset.step)) });
  });
  host.appendChild(bar);
}

const viewMods = new Map();
export async function renderView() {
  const name = VIEW_SRC[W.view] ? W.view : "overview";
  if (!viewMods.has(name)) {
    // a rejected import must not be cached — dropping it lets the next switch retry instead of
    // leaving the tab permanently blank until a full page reload
    viewMods.set(name, import(VIEW_SRC[name]).catch((e) => { viewMods.delete(name); throw e; }));
  }
  const mod = await viewMods.get(name).catch(() => null);
  if (!mod || W.view !== name || !W.body) return; // a faster switch superseded this import
  W.body.replaceChildren();
  mod.render(W.body);
}

export function navigate(patch, replace = false) {
  Object.assign(W, patch);
  writeUrl(W, replace);
  syncChrome();
  renderView();
}

// Search dispatches land here so every entry point produces the same drill state; MIL is Log-only.
export function openAirline(name) {
  navigate({ view: "drill", airline: name, service: null, od: null, hex: null, apt: null, mil: false, page: 1 });
}
export function openService(callsign, airline = null) {
  navigate({ view: "drill", airline, service: callsign, od: null, hex: null, apt: null, mil: false, page: 1 });
}
export function openAirframe(hex) {
  navigate({ view: "drill", airline: null, service: null, od: null, hex, apt: null, mil: false, page: 1 });
}
export function openAirport(code) {
  navigate({ view: "drill", airline: null, service: null, od: null, hex: null, apt: code, mil: false, page: 1 });
}

// <icao24>.<epoch>[.<callsign>] deep link: nearest start within ±15 min (the key is a start time,
// not an identity — a rebuilt mart may shift it by seconds); the callsign segment breaks real ties.
async function resolveDeepLink() {
  const want = W.inst;
  const [hex, epochStr, csKey] = String(W.inst).split(".");
  const epoch = Number(epochStr);
  if (!hex || !Number.isFinite(epoch)) {
    W.inst = null;
    return;
  }
  const day = jstDayOf(epoch); // the fallback day-list range below still keys on the epoch's own day
  // a different flight already in focus must clear NOW, silently — if this lookup fails, B's path
  // must not sit drawn under A's URL, and A's URL must survive B's teardown
  if (W.S && W.S.focus && W.S.focus.key !== want) dropFocus();
  // range both JST days: 3,212 live starts sit within 15 min of midnight and a rebuild can cross it
  const p = await fetchInstances(
    { hex, day_from: jstDayOf(epoch - 900), day_to: jstDayOf(epoch + 900), limit: 200 }, "deeplink");
  if (W.inst !== want) return; // a newer navigation owns the state — this resolve is obsolete
  if (!p) return setStatus("deep link: lookup unavailable"); // superseded/failed ≠ not-found: keep the URL
  const norm = (c) => String(c || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
  // nearest |Δ| wins, not first-in-sort (2,766 live rows have a newer same-hex start inside ±900s)
  let cands = p.rows
    .filter((r) => r.startTs != null && Math.abs(r.startTs - epoch) <= 900)
    .sort((a, b) => Math.abs(a.startTs - epoch) - Math.abs(b.startTs - epoch) || a.startTs - b.startTs);
  if (csKey) {
    // a key that names a callsign only ever resolves to that callsign — a sole nonmatching
    // neighbor (2,885 live cases) is a wrong flight, not a fallback
    const named = cands.filter((r) => norm(r.callsign) === norm(csKey));
    cands = named;
  } else if (cands.length > 1
      && Math.abs(cands[0].startTs - epoch) === Math.abs(cands[1].startTs - epoch)
      && norm(cands[0].callsign) !== norm(cands[1].callsign)) {
    cands = []; // a legacy 2-part key aliasing two flights — reject over a repeatable wrong pick
  }
  const hit = cands[0];
  if (hit) {
    if (hit.key && hit.key !== W.inst) {
      // canonicalize to the resolved key: a rebuilt start shifts it, and row lighting compares keys
      W.inst = hit.key;
      writeUrl(W, true);
      renderView();
    }
    enterFocus(W.S, hit, {
      onExit: () => {
        W.inst = null;
        writeUrl(W, true);
        markActive(null);
      },
      // a deep link that draws nothing must not survive the reload it just failed
      onEmpty: (msg) => {
        W.inst = null;
        writeUrl(W, true);
        setStatus(msg);
      },
    });
    return;
  }
  // no match: fall back to the hex's day list rather than guessing at a neighbouring flight
  W.inst = null;
  navigate({ view: "drill", hex, airline: null, service: null, od: null, apt: null, range: `${day}..${day}`, page: 1 }, true);
  setStatus("instance not found — showing that day's flights for the airframe");
}

export function syncChrome() {
  for (const b of W.rail.querySelectorAll(".wb-view[data-view]"))
    b.setAttribute("aria-selected", String(b.dataset.view === W.view));
  for (const b of W.rail.querySelectorAll(".wb-chip[data-range]"))
    b.setAttribute("aria-pressed", String(b.dataset.range === W.range));
  const custom = /^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$/.exec(W.range);
  const row = W.rail.querySelector(".wb-custom");
  if (custom) {
    const [f, t] = row.querySelectorAll("input");
    row.hidden = false;
    f.value = custom[1];
    t.value = custom[2];
  } else {
    row.hidden = true; // a preset is now the range — an open editor would read as still-pending input
  }
  W.rail.querySelector(".wb-chip[data-custom]").setAttribute("aria-pressed", String(!!custom));
}

export function buildRail() {
  const rail = document.createElement("aside");
  rail.className = "wb-rail";
  rail.id = "wb-rail";
  rail.setAttribute("aria-label", "Workbench");
  rail.innerHTML =
    '<div class="wb-head"><span class="wb-title">WORKBENCH</span>' +
    '<button type="button" class="wb-collapse" aria-label="Collapse workbench">◂</button></div>' +
    '<div class="wb-views" role="tablist" aria-label="Workbench views">' +
    '<button type="button" class="wb-view" role="tab" aria-controls="wb-body" data-view="overview">home</button>' +
    '<button type="button" class="wb-view" role="tab" aria-controls="wb-body" data-view="drill">drill</button>' +
    '<button type="button" class="wb-view" role="tab" aria-controls="wb-body" data-view="log">log</button>' +
    '<button type="button" class="wb-view wb-ghost" role="tab" aria-selected="false" disabled title="slice 3">est</button>' +
    '<button type="button" class="wb-view wb-ghost" role="tab" aria-selected="false" disabled title="slice 3">cov</button></div>' +
    '<div class="wb-range" title="date range applies to instance lists">' +
    '<button type="button" class="wb-chip" data-range="7d">7d</button>' +
    '<button type="button" class="wb-chip" data-range="30d">30d</button>' +
    '<button type="button" class="wb-chip" data-range="90d">90d</button>' +
    '<button type="button" class="wb-chip" data-range="all">all</button>' +
    '<button type="button" class="wb-chip" data-custom="1">custom</button>' +
    '<span class="wb-custom" hidden><input type="date" aria-label="Range start" />' +
    '<input type="date" aria-label="Range end" />' +
    '<button type="button" class="wb-chip" data-apply="1">apply</button></span></div>' +
    '<div class="wb-search"></div><div class="wb-body" id="wb-body" role="tabpanel"></div>' +
    '<div class="wb-sr" role="status" aria-live="polite"></div>';
  document.body.appendChild(rail);

  const tab = document.createElement("button");
  tab.type = "button";
  tab.className = "wb-tab";
  tab.hidden = true;
  tab.textContent = "WORKBENCH ▸";
  document.body.appendChild(tab);

  W.rail = rail;
  W.body = rail.querySelector(".wb-body");
  W.searchHost = rail.querySelector(".wb-search");
  W.status = rail.querySelector(".wb-sr");

  rail.querySelector(".wb-collapse").addEventListener("click", () => {
    rail.hidden = true;
    tab.hidden = false;
    tab.focus();
  });
  tab.addEventListener("click", () => {
    tab.hidden = true;
    rail.hidden = false;
    rail.querySelector(".wb-collapse").focus();
  });
  rail.querySelector(".wb-views").addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-view[data-view]");
    // MIL is Log's control, so it never survives into a view with no toggle to undo it
    if (btn && btn.dataset.view !== W.view)
      navigate({ view: btn.dataset.view, page: 1, mil: btn.dataset.view === "log" && W.mil });
  });
  const custom = rail.querySelector(".wb-custom");
  rail.querySelector(".wb-range").addEventListener("click", (e) => {
    const preset = e.target.closest(".wb-chip[data-range]");
    if (preset) return navigate({ range: preset.dataset.range, page: 1 });
    if (e.target.closest(".wb-chip[data-custom]")) custom.hidden = !custom.hidden;
    if (e.target.closest(".wb-chip[data-apply]")) {
      const [f, t] = custom.querySelectorAll("input");
      if (f.value && t.value) navigate({ range: `${f.value}..${t.value}`, page: 1 });
    }
  });
}

export function applyUrl() {
  Object.assign(W, readUrl());
  syncChrome();
  renderView();
  if (W.inst) resolveDeepLink();
  else if (isFocused()) exitFocus();
}
