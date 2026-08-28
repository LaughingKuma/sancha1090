import { useEffect, useRef } from "preact/hooks";
import {
  PAGE_LIMIT, activeApt, activeMil, activeOd, activePage, activeService, activeType,
  activeAirline, activeHex, customRange, doorway, milAvailable, milUnavailable, navigate, rangeParams, wb,
} from "../store";
import { fetchInstances } from "../data";
import { useResource } from "../resource";
import { InstanceList } from "./InstanceList";
import { Pager } from "./Pager";
import { Empty } from "./Empty";

const up = (v: string) => (v.trim() ? v.trim().toUpperCase() : null);

// the log's fetch key: exactly the fields /instances reads, never inst — a focus click leaves the list alone
const logParams = () => ({
  ...rangeParams.value,
  airline: activeAirline.value,
  callsign: activeService.value,
  hex: activeHex.value,
  airport: activeApt.value,
  type: activeType.value,
  od: activeOd.value,
  military: activeMil.value ? 1 : null,
  limit: PAGE_LIMIT,
  offset: (activePage.value - 1) * PAGE_LIMIT,
  sort: "day_desc",
});
const logFetch = (p: ReturnType<typeof logParams>, signal: AbortSignal) => fetchInstances(p, "instances", signal);

// the bar reads only the fields it shows, each a per-field computed, so an unrelated wb write (a focus
// click, a pager step) never re-renders it and cannot re-apply value= over half-typed apt/type text
function FilterBar() {
  const cm = customRange.value; // the store's range→[from,to] match, reused so the day input shares one parse
  const day = cm && cm[1] === cm[2] ? cm[1] : "";
  const mil = activeMil.value;
  const milOk = milAvailable.value;
  const apt = useRef<HTMLInputElement>(null);
  const type = useRef<HTMLInputElement>(null);
  // text filters are unserialized (the wire scheme is fixed), so they apply on commit and replace the entry
  const commit = () => navigate({ apt: up(apt.current!.value), type: up(type.current!.value), page: 1 }, { replace: true });
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "Enter") commit();
  };
  return (
    <div class="wb-filters">
      <input type="date" class="wb-f-day" aria-label="Day" value={day}
        onChange={(e) => navigate({ range: e.currentTarget.value ? `${e.currentTarget.value}..${e.currentTarget.value}` : "30d", page: 1 })} />
      <input type="text" class="wb-f-apt" aria-label="Airport" placeholder="airport" size={6} maxLength={4}
        ref={apt} value={activeApt.value ?? ""} onChange={commit} onKeyDown={onKey} />
      <input type="text" class="wb-f-type" aria-label="Type" placeholder="type" size={5} maxLength={4}
        ref={type} value={activeType.value ?? ""} onChange={commit} onKeyDown={onKey} />
      <button type="button" class="wb-chip wb-f-mil" aria-pressed={mil} disabled={!milOk}
        title={milOk ? undefined : "military filter needs the tier mart"}
        onClick={() => navigate({ mil: !mil, page: 1 })}>mil</button>
      {/* clear is a doorway into the log's own scope: it keeps the drill path (airline · service), drops the rest */}
      <button type="button" class="wb-chip wb-f-clear"
        onClick={() => doorway("log", { airline: wb.peek().airline, service: wb.peek().service })}>clear</button>
    </div>
  );
}

function LogResults() {
  const st = useResource(logParams, logFetch);
  // an instances envelope that says the tier mart is absent drops the military filter once (it then re-fetches)
  useEffect(() => {
    if (st.state === "ok" && !st.data.milAvailable && milAvailable.peek()) milUnavailable();
  }, [st]);
  if (st.state === "outage" || st.state === "unreachable") return <Empty what="instances" state={st.state} />;
  if (st.state !== "ok") return null; // idle / superseded: nothing new is drawn
  if (!st.data.milAvailable && milAvailable.value) return null; // the effect above is dropping the filter
  return (
    <>
      <InstanceList rows={st.data.rows} />
      <Pager p={st.data} />
    </>
  );
}

export function Log() {
  // the bar and the list are siblings so the list's fetch re-render never touches the bar's inputs
  return (
    <>
      <FilterBar />
      <LogResults />
    </>
  );
}
