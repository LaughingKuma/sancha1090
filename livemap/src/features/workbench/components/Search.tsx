import { useEffect, useRef, useState } from "preact/hooks";
import { fetchSearch } from "../data";
import { openAirframe, openAirline, openAirport, openService } from "../store";

const MIN_Q = 2; // the endpoint serves an empty envelope below this — don't spend a request on it
const DEBOUNCE_MS = 180;

type Res = NonNullable<Awaited<ReturnType<typeof fetchSearch>>>;
type GroupKey = keyof Res;
interface Item { group: GroupKey; main: string; side: string; open: () => void }
interface Group<K extends GroupKey> {
  label: string;
  line: (r: Res[K][number]) => [string, string];
  open: (r: Res[K][number]) => void;
}
const n = (v: number) => (v ? v.toLocaleString() : "");
// one table per result bucket: how a row reads and where it leads, typed against data's own shapes
const GROUPS: { [K in GroupKey]: Group<K> } = {
  airlines: { label: "airlines", line: (r) => [r.name, n(r.n)], open: (r) => openAirline(r.name) },
  services: {
    label: "services",
    line: (r) => [r.callsign, [r.airline, n(r.n)].filter(Boolean).join(" · ")],
    open: (r) => openService(r.callsign, r.airline || null),
  },
  airframes: {
    label: "airframes",
    line: (r) => [r.reg || r.hex, [r.type, r.hex].filter(Boolean).join(" · ")],
    open: (r) => openAirframe(r.hex),
  },
  airports: {
    label: "airports",
    line: (r) => [r.iata || r.icao, [r.name, r.city].filter(Boolean).join(" · ")],
    open: (r) => openAirport(r.iata || r.icao),
  },
};
const ORDER = Object.keys(GROUPS) as GroupKey[];

function bucket<K extends GroupKey>(res: Res, group: K): Item[] {
  const g: Group<K> = GROUPS[group];
  return res[group].map((row) => {
    const [main, side] = g.line(row);
    return { group, main: main || "—", side: side || "", open: () => g.open(row) };
  });
}
const flatten = (res: Res): Item[] => ORDER.flatMap((k) => bucket(res, k));

export function Search() {
  const host = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  // one stable object so the unmount cleanup reads the LATEST timer/seq without touching a ref in cleanup
  const st = useRef({ timer: 0, seq: 0 });
  const [items, setItems] = useState<Item[] | null>(null); // null = closed; [] = open with "no matches"
  const [cursor, setCursor] = useState(-1);

  const close = () => {
    // dismissal is durable: cancel the pending debounce AND orphan any in-flight fetch, or either
    // could reopen the dropdown right after an Escape / outside click
    clearTimeout(st.current.timer);
    st.current.seq++;
    setItems(null);
    setCursor(-1);
  };
  const choose = (i: number) => {
    const it = items?.[i];
    if (!it) return;
    close();
    input.current!.value = "";
    it.open();
  };
  const run = async (q: string, my: number) => {
    const res = await fetchSearch(q);
    if (my !== st.current.seq || !res || input.current?.value.trim() !== q) return; // dismissed or moved on
    setItems(flatten(res));
    setCursor(-1);
  };
  const onInput = () => {
    clearTimeout(st.current.timer);
    const q = input.current!.value.trim();
    if (q.length < MIN_Q) return close();
    st.current.timer = window.setTimeout(() => run(q, st.current.seq), DEBOUNCE_MS);
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Escape") {
      // Esc inside the search box dismisses the search — it must never bubble into the global
      // focus-exit listener and tear down an active focus as a side effect
      e.stopPropagation();
      return close();
    }
    if (!items?.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((cursor + 1) % items.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((cursor - 1 + items.length) % items.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      choose(cursor < 0 ? 0 : cursor);
    }
  };

  // a click anywhere else is a dismissal — the dropdown must never outlive the question
  useEffect(() => {
    const s = st.current; // the live object, copied at setup so the cleanup reads no ref
    const onDoc = (e: MouseEvent) => {
      if (host.current && !host.current.contains(e.target as Node)) close();
    };
    document.addEventListener("click", onDoc);
    // unmount orphans the debounce and any in-flight answer too, so neither can touch a null ref later
    return () => {
      document.removeEventListener("click", onDoc);
      clearTimeout(s.timer);
      s.seq++;
    };
  }, []);
  useEffect(() => {
    if (cursor >= 0) host.current!.querySelectorAll(".wb-opt")[cursor]?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const open = items !== null;
  return (
    <div class="wb-search" ref={host}>
      <div class="wb-search-box">
        <span class="gl" aria-hidden="true">⌕</span>
        <input type="search" class="wb-q" ref={input} placeholder="search airline, flight, reg, airport"
          aria-label="Search" autocomplete="off" role="combobox" aria-expanded={open}
          aria-controls="wb-drop" aria-activedescendant={cursor >= 0 ? `wb-opt-${cursor}` : undefined}
          onInput={onInput} onKeyDown={onKey} />
      </div>
      <div class="wb-drop" id="wb-drop" role="listbox" hidden={!open}>
        {items && !items.length && <div class="wb-empty">no matches</div>}
        {items?.flatMap((it, i) => [
          // items arrive grouped in ORDER, so a group header sits wherever the bucket changes
          (i === 0 || items[i - 1].group !== it.group) && <div key={`g:${it.group}`} class="wb-group">{GROUPS[it.group].label}</div>,
          <button key={i} id={`wb-opt-${i}`} type="button" class="wb-opt" role="option" aria-selected={i === cursor} onClick={() => choose(i)}>
            <span class="wb-opt-main">{it.main}</span>
            <span class="wb-opt-side">{it.side}</span>
          </button>,
        ])}
      </div>
    </div>
  );
}
