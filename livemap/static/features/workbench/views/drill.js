import {
  W, esc, panel, navigate, rangeParams, renderInstances, renderRows, renderPager,
  tierMixHTML, PAGE_LIMIT,
} from "../shell.js?v=6.42";
import { fetchAirlines, fetchServices, fetchInstances } from "../data.js?v=6.42";

// airline → service → instance; a search hit on an airframe or airport enters at the instance level
const level = () => (W.service || W.hex || W.apt ? "instances" : W.airline ? "services" : "airlines");

function crumbs(host) {
  const trail = [{ label: "all", patch: { airline: null, service: null, od: null, hex: null, apt: null, page: 1 } }];
  if (W.airline) trail.push({ label: W.airline, patch: W.service ? { service: null, od: null, page: 1 } : null });
  if (W.service) trail.push({ label: W.service, patch: null });
  if (W.hex) trail.push({ label: W.hex.toUpperCase(), patch: null });
  if (W.apt) trail.push({ label: W.apt.toUpperCase(), patch: null });
  const bar = document.createElement("div");
  bar.className = "wb-crumbs";
  bar.innerHTML = trail
    .map((c, i) =>
      (i ? '<span class="wb-crumb-sep">▸</span>' : "") +
      `<button type="button" class="wb-crumb" data-i="${i}"${c.patch ? "" : ' aria-current="page"'}>${esc(c.label)}</button>`,
    )
    .join("");
  bar.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-crumb");
    const c = btn && trail[Number(btn.dataset.i)];
    if (c && c.patch) navigate(c.patch);
  });
  host.appendChild(bar);
}

function section(host, text) {
  host.insertAdjacentHTML("beforeend", `<div class="wb-sect">${esc(text)}</div>`);
}

const airlineHTML = (r, i) =>
  `<button type="button" class="wb-row wb-stack" data-idx="${i}">` +
  `<span class="wb-row-main"><span class="wb-name">${esc(r.name || "—")}</span>` +
  `<span class="wb-n">${esc(r.nFlights.toLocaleString())}</span></span>` +
  `<span class="wb-row-sub"><span>${esc(`${r.nServices} svc · ${r.firstDay || "?"} → ${r.lastDay || "?"}`)}</span>` +
  `${tierMixHTML(r.tiers)}</span></button>`;

const serviceHTML = (r, i) =>
  `<button type="button" class="wb-row wb-stack" data-idx="${i}">` +
  `<span class="wb-row-main"><span class="wb-name">${esc(r.callsign || "—")}</span>` +
  `<span class="wb-n">${esc(r.nInstances.toLocaleString())}</span></span>` +
  `<span class="wb-row-sub"><span class="wb-sub-od">${esc(r.topOd.map((o) => `${o.o || "?"}-${o.d || "?"} ${o.n}`).join(" · ") || "—")}</span>` +
  `${tierMixHTML(r.tiers)}</span></button>`;

function odChips(host, list) {
  if (!list.length) return;
  const bar = document.createElement("div");
  bar.className = "wb-odchips";
  bar.innerHTML = list
    .map((c, i) => {
      const key = `${c.o || "?"}-${c.d || "?"}`;
      return `<button type="button" class="wb-chip" data-i="${i}" aria-pressed="${String(W.od === key)}">${esc(`${key} ${c.n}`)}</button>`;
    })
    .join("");
  bar.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-chip");
    if (!btn) return;
    const c = list[Number(btn.dataset.i)];
    const key = `${c.o || "?"}-${c.d || "?"}`;
    navigate({ od: W.od === key ? null : key, page: 1 }); // a second click loosens the filter again
  });
  host.appendChild(bar);
}

export async function render(host) {
  const el = panel(host);
  crumbs(el);
  const offset = (W.page - 1) * PAGE_LIMIT;
  const lvl = level();

  if (lvl === "airlines") {
    section(el, "airlines");
    const p = await fetchAirlines({ limit: PAGE_LIMIT, offset });
    if (!el.isConnected) return;
    if (!p) return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">airlines unavailable</div>');
    renderRows(el, p.rows, airlineHTML, (r) => navigate({ airline: r.name, service: null, od: null, page: 1 }), "no airlines");
    renderPager(el, p);
    return;
  }

  if (lvl === "services") {
    section(el, "services");
    const p = await fetchServices({ airline: W.airline, limit: PAGE_LIMIT, offset });
    if (!el.isConnected) return;
    if (!p) return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">services unavailable</div>');
    renderRows(el, p.rows, serviceHTML, (r) => navigate({ service: r.callsign, od: null, page: 1 }), "no services");
    renderPager(el, p);
    return;
  }

  section(el, "instances");
  const p = await fetchInstances({
    ...rangeParams(),
    airline: W.airline,
    callsign: W.service,
    hex: W.hex,
    airport: W.apt,
    od: W.od,
    limit: PAGE_LIMIT,
    offset,
    sort: "day_desc",
  });
  if (!el.isConnected) return;
  if (!p) return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">instances unavailable</div>');
  odChips(el, p.od);
  renderInstances(el, p.rows);
  renderPager(el, p);
}
