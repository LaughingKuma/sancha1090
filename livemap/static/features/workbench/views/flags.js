import {
  W, esc, panel, navigate, rangeParams, renderPager, focusInstance, jstDayOf, PAGE_LIMIT,
} from "../shell.js?v=6.43";
import { fetchFlags } from "../data.js?v=6.43";

const CLASSES = [
  "tiebreak_endpoint", "single_source", "one_sided_intl", "feasibility_snap", "diversion",
  "same_endpoint", "military",
];
// the wire/URL value is the mart's exact class name — only the label loses the underscores
const label = (c) => String(c || "").replaceAll("_", " ");

function chips(host, classes) {
  // the all-chip total counts flag ROWS (a multi-class flight counts once per class), which is the
  // grain the pager below counts in — the overview strip's "flagged" counts distinct flights
  const rowTotal = Object.values(classes).reduce((a, n) => a + n, 0);
  const bar = document.createElement("div");
  bar.className = "wb-classes";
  bar.innerHTML =
    `<button type="button" class="wb-chip" data-cls="" aria-pressed="${String(!W.flagClass)}">` +
    `all ${esc(rowTotal.toLocaleString())}</button>` +
    CLASSES.map(
      (c) =>
        `<button type="button" class="wb-chip" data-cls="${esc(c)}" aria-pressed="${String(W.flagClass === c)}">` +
        `${esc(label(c))} ${esc((classes[c] || 0).toLocaleString())}</button>`,
    ).join("");
  bar.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-chip[data-cls]");
    if (btn) navigate({ flagClass: btn.dataset.cls || null, page: 1 });
  });
  host.appendChild(bar);
}

const flagRowHTML = (r, i) => {
  const day = r.day || jstDayOf(r.startTs);
  const on = !!r.key && r.key === W.inst; // a re-render (pager, chip) must keep the focused row lit
  return (
    `<button type="button" class="wb-row wb-stack wb-inst${on ? " wb-active" : ""}" ` +
    `aria-pressed="${on}" data-idx="${i}">` +
    `<span class="wb-row-main"><span class="wb-date">${esc(day.slice(5) || "—")}</span>` +
    `<span class="wb-name">${esc(r.callsign || r.hex || "—")}</span>` +
    `<span class="wb-flagcls">${esc(label(r.flagClass))}</span>` +
    `<span class="wb-n">${esc(`${r.o || "?"} → ${r.d || "?"}`)}</span></span>` +
    `<span class="wb-row-sub"><span class="wb-detail">${esc(r.detail || "—")}</span></span></button>`
  );
};

export function renderFlagRows(host, list) {
  if (!list.length) {
    host.insertAdjacentHTML("beforeend", '<div class="wb-empty">no flagged instances</div>');
    return;
  }
  const wrap = document.createElement("div");
  wrap.innerHTML = list.map(flagRowHTML).join("");
  wrap.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-inst");
    if (btn) focusInstance(list[Number(btn.dataset.idx)], btn);
  });
  host.appendChild(wrap);
}

export async function render(host) {
  const el = panel(host);
  const p = await fetchFlags({
    ...rangeParams(),
    class: W.flagClass,
    limit: PAGE_LIMIT,
    offset: (W.page - 1) * PAGE_LIMIT,
  });
  if (!el.isConnected) return;
  if (!p) return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">flags unavailable</div>');
  chips(el, p.classes);
  if (!p.available)
    return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">flags mart not deployed</div>');
  renderFlagRows(el, p.rows);
  renderPager(el, p);
}
