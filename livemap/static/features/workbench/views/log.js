import {
  W, esc, panel, navigate, rangeParams, renderInstances, renderPager, setStatus, PAGE_LIMIT,
} from "../shell.js?v=6.42";
import { fetchInstances } from "../data.js?v=6.42";

const dayValue = () => {
  const m = /^(\d{4}-\d{2}-\d{2})\.\.(\1)$/.exec(W.range || "");
  return m ? m[1] : "";
};

function filters(host) {
  const bar = document.createElement("div");
  bar.className = "wb-filters";
  bar.innerHTML =
    `<input type="date" class="wb-f-day" aria-label="Day" value="${esc(dayValue())}" />` +
    `<input type="text" class="wb-f-apt" aria-label="Airport" placeholder="airport" size="6" maxlength="4" value="${esc(W.apt || "")}" />` +
    `<input type="text" class="wb-f-type" aria-label="Type" placeholder="type" size="5" maxlength="4" value="${esc(W.type || "")}" />` +
    `<button type="button" class="wb-chip wb-f-mil" aria-pressed="${String(W.mil)}"${W.milAvailable ? "" : ' disabled title="military filter needs the tier mart"'}>mil</button>` +
    '<button type="button" class="wb-chip wb-f-clear">clear</button>';
  const day = bar.querySelector(".wb-f-day");
  const apt = bar.querySelector(".wb-f-apt");
  const type = bar.querySelector(".wb-f-type");
  const up = (v) => (v ? v.trim().toUpperCase() : null);
  // text filters are unserialized (the wire scheme is fixed), so they apply on commit and replace the entry
  const commit = () => navigate({ apt: up(apt.value), type: up(type.value), page: 1 }, true);
  for (const el of [apt, type]) {
    el.addEventListener("change", commit);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter") commit();
    });
  }
  day.addEventListener("change", () => navigate({ range: day.value ? `${day.value}..${day.value}` : "30d", page: 1 }));
  bar.querySelector(".wb-f-mil").addEventListener("click", (e) => {
    if (!e.currentTarget.disabled) navigate({ mil: !W.mil, page: 1 });
  });
  bar.querySelector(".wb-f-clear").addEventListener("click", () =>
    navigate({ apt: null, type: null, mil: false, od: null, hex: null, page: 1 }),
  );
  host.appendChild(bar);
}

export async function render(host) {
  const el = panel(host);
  filters(el);
  const p = await fetchInstances({
    ...rangeParams(),
    airline: W.airline,
    callsign: W.service,
    hex: W.hex,
    airport: W.apt,
    type: W.type,
    od: W.od,
    military: W.mil ? 1 : null,
    limit: PAGE_LIMIT,
    offset: (W.page - 1) * PAGE_LIMIT,
    sort: "day_desc",
  });
  if (!el.isConnected) return;
  if (!p) return el.insertAdjacentHTML("beforeend", '<div class="wb-empty">instances unavailable</div>');
  // the envelope only says so while MIL is on, and a disabled pressed toggle would strand an empty
  // list — drop the filter and re-render once (the reply then omits the flag, so this can't loop)
  if (!p.milAvailable && W.milAvailable) {
    W.milAvailable = false;
    setStatus("military filter unavailable — tier mart not deployed");
    return navigate({ mil: false, page: 1 }, true);
  }
  renderInstances(el, p.rows);
  renderPager(el, p);
}
