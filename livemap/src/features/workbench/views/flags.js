import { W, esc, panel, navigate, rangeParams, renderFlagRows, renderPager, PAGE_LIMIT } from "../shell.js";
import { fetchFlags } from "../data";
import { FLAG_CLASSES } from "../url";

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
    FLAG_CLASSES.map(
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
