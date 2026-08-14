import { esc, openAirline, openService, openAirframe, openAirport } from "./shell.js?v=6.44";
import { fetchSearch } from "./data.js?v=6.44";

const MIN_Q = 2; // the endpoint serves an empty envelope below this — don't spend a request on it
const DEBOUNCE_MS = 180;

const GROUPS = [
  { key: "airlines", label: "airlines", line: (r) => [r.name, r.n ? r.n.toLocaleString() : ""] },
  { key: "services", label: "services", line: (r) => [r.callsign, [r.airline, r.n ? r.n.toLocaleString() : ""].filter(Boolean).join(" · ")] },
  { key: "airframes", label: "airframes", line: (r) => [r.reg || r.hex, [r.type, r.hex].filter(Boolean).join(" · ")] },
  { key: "airports", label: "airports", line: (r) => [r.iata || r.icao, [r.name, r.city].filter(Boolean).join(" · ")] },
];

function dispatch(group, r) {
  if (group === "airlines") return openAirline(r.name);
  if (group === "services") return openService(r.callsign, r.airline || null);
  if (group === "airframes") return openAirframe(r.hex);
  return openAirport(r.iata || r.icao);
}

export function mountSearch(host) {
  host.innerHTML =
    '<div class="wb-search-box"><span class="gl" aria-hidden="true">⌕</span>' +
    '<input type="search" class="wb-q" placeholder="search airline, flight, reg, airport" ' +
    'aria-label="Search" autocomplete="off" role="combobox" aria-expanded="false" aria-controls="wb-drop" /></div>' +
    '<div class="wb-drop" id="wb-drop" role="listbox" hidden></div>';
  const input = host.querySelector(".wb-q");
  const drop = host.querySelector(".wb-drop");
  let items = [];
  let cursor = -1;
  let timer = 0;
  let runSeq = 0;

  const close = () => {
    // dismissal is durable: cancel the pending debounce AND orphan any in-flight fetch, or either
    // could reopen the dropdown right after an Escape / outside click
    clearTimeout(timer);
    runSeq++;
    drop.hidden = true;
    drop.replaceChildren();
    input.setAttribute("aria-expanded", "false");
    items = [];
    cursor = -1;
  };
  const mark = () => {
    const nodes = drop.querySelectorAll(".wb-opt");
    nodes.forEach((n, i) => n.setAttribute("aria-selected", String(i === cursor)));
    if (cursor >= 0 && nodes[cursor]) nodes[cursor].scrollIntoView({ block: "nearest" });
  };
  const choose = (i) => {
    const it = items[i];
    if (!it) return;
    close();
    input.value = "";
    dispatch(it.group, it.row);
  };

  function paint(res) {
    items = [];
    const parts = [];
    for (const g of GROUPS) {
      const rowsOf = res[g.key] || [];
      if (!rowsOf.length) continue;
      parts.push(`<div class="wb-group">${g.label}</div>`);
      for (const r of rowsOf) {
        const [main, side] = g.line(r);
        parts.push(
          `<button type="button" class="wb-opt" role="option" aria-selected="false" data-i="${items.length}">` +
            `<span class="wb-opt-main">${esc(main || "—")}</span>` +
            `<span class="wb-opt-side">${esc(side || "")}</span></button>`,
        );
        items.push({ group: g.key, row: r });
      }
    }
    if (!items.length) parts.push('<div class="wb-empty">no matches</div>');
    drop.innerHTML = parts.join("");
    drop.hidden = false;
    input.setAttribute("aria-expanded", "true");
    cursor = -1;
  }

  async function run(q, my) {
    const res = await fetchSearch(q);
    if (my !== runSeq || !res || input.value.trim() !== q) return; // dismissed or moved on meanwhile
    paint(res);
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < MIN_Q) return close();
    timer = setTimeout(() => run(q, runSeq), DEBOUNCE_MS);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      // Esc inside the search box dismisses the search — it must never bubble into the global
      // focus-exit listener and tear down an active focus as a side effect
      e.stopPropagation();
      return close();
    }
    if (!items.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      cursor = (cursor + 1) % items.length;
      mark();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      cursor = (cursor - 1 + items.length) % items.length;
      mark();
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(cursor < 0 ? 0 : cursor);
    }
  });
  drop.addEventListener("click", (e) => {
    const btn = e.target.closest(".wb-opt");
    if (btn) choose(Number(btn.dataset.i));
  });
  // a click anywhere else is a dismissal — the dropdown must never outlive the question
  document.addEventListener("click", (e) => {
    if (!host.contains(e.target)) close();
  });
}
