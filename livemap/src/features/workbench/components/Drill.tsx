import {
  PAGE_LIMIT, activeAirline, activeApt, activeHex, activeOd, activePage, activeService, doorway,
  drillLevel, navigate, openAirline, openService, rangeParams, wb,
} from "../store";
import type { OdChip } from "../wire";
import { fetchAirlines, fetchInstances, fetchServices, type AirlineRow, type ServiceRow } from "../data";
import { useResource } from "../resource";
import { InstanceList } from "./InstanceList";
import { RowList } from "./RowList";
import { Pager } from "./Pager";
import { Empty } from "./Empty";

// a crumb is a doorway into its own scope: the table drops everything deeper than the step clicked
function Crumbs() {
  const airline = activeAirline.value;
  const service = activeService.value;
  const hex = activeHex.value;
  const apt = activeApt.value;
  const trail: { label: string; go: (() => void) | null }[] = [{ label: "all", go: () => doorway("drill") }];
  if (airline) trail.push({ label: airline, go: service ? () => doorway("drill", { airline }) : null });
  if (service) trail.push({ label: service, go: null });
  if (hex) trail.push({ label: hex.toUpperCase(), go: null });
  if (apt) trail.push({ label: apt.toUpperCase(), go: null });
  return (
    <div class="wb-crumbs">
      {trail.flatMap((c, i) => [
        i ? <span key={`s${i}`} class="wb-crumb-sep">▸</span> : null,
        <button key={i} type="button" class="wb-crumb" aria-current={c.go ? undefined : "page"}
          onClick={() => c.go?.()}>{c.label}</button>,
      ])}
    </div>
  );
}

const airlineRow = (r: AirlineRow) => ({
  key: r.name,
  name: r.name || "—",
  n: r.nFlights.toLocaleString(),
  sub: `${r.nServices} svc · ${r.firstDay || "?"} → ${r.lastDay || "?"}`,
  tiers: r.tiers,
});
const serviceRow = (r: ServiceRow) => ({
  key: r.callsign,
  name: r.callsign || "—",
  n: r.nInstances.toLocaleString(),
  sub: r.topOd.map((o) => `${o.o || "?"}-${o.d || "?"} ${o.n}`).join(" · ") || "—",
  subClass: "wb-sub-od",
  tiers: r.tiers,
});

const airlineParams = () => ({ limit: PAGE_LIMIT, offset: (activePage.value - 1) * PAGE_LIMIT });
const airlineFetch = (p: ReturnType<typeof airlineParams>) => fetchAirlines(p);

function DrillAirlines() {
  const st = useResource(airlineParams, airlineFetch);
  return (
    <>
      <div class="wb-sect">airlines</div>
      {st.state === "outage" || st.state === "unreachable" ? (
        <Empty what="airlines" state={st.state} />
      ) : st.state === "ok" ? (
        <>
          <RowList rows={st.data.rows.map(airlineRow)} onPick={(i) => openAirline(st.data.rows[i].name)}
            empty="no airlines" />
          <Pager p={st.data} />
        </>
      ) : null}
    </>
  );
}

const serviceParams = () => ({
  airline: activeAirline.value, limit: PAGE_LIMIT, offset: (activePage.value - 1) * PAGE_LIMIT,
});
const serviceFetch = (p: ReturnType<typeof serviceParams>) => fetchServices(p);

function DrillServices() {
  const st = useResource(serviceParams, serviceFetch);
  return (
    <>
      <div class="wb-sect">services</div>
      {st.state === "outage" || st.state === "unreachable" ? (
        <Empty what="services" state={st.state} />
      ) : st.state === "ok" ? (
        <>
          <RowList rows={st.data.rows.map(serviceRow)}
            onPick={(i) => openService(st.data.rows[i].callsign, wb.peek().airline)} empty="no services" />
          <Pager p={st.data} />
        </>
      ) : null}
    </>
  );
}

function OdChips({ list }: { list: OdChip[] }) {
  const od = activeOd.value;
  if (!list.length) return null;
  return (
    <div class="wb-odchips">
      {list.map((c, i) => {
        const key = `${c.o || "?"}-${c.d || "?"}`;
        // a second click on the active chip loosens the filter again
        return (
          <button key={i} type="button" class="wb-chip" aria-pressed={od === key}
            onClick={() => navigate({ od: od === key ? null : key, page: 1 })}>{`${key} ${c.n}`}</button>
        );
      })}
    </div>
  );
}

const instParams = () => ({
  ...rangeParams.value,
  airline: activeAirline.value,
  callsign: activeService.value,
  hex: activeHex.value,
  airport: activeApt.value,
  od: activeOd.value,
  limit: PAGE_LIMIT,
  offset: (activePage.value - 1) * PAGE_LIMIT,
  sort: "day_desc",
});
const instFetch = (p: ReturnType<typeof instParams>, signal: AbortSignal) => fetchInstances(p, "instances", signal);

function DrillInstances() {
  const st = useResource(instParams, instFetch);
  return (
    <>
      <div class="wb-sect">instances</div>
      {st.state === "outage" || st.state === "unreachable" ? (
        <Empty what="instances" state={st.state} />
      ) : st.state === "ok" ? (
        <>
          <OdChips list={st.data.od} />
          <InstanceList rows={st.data.rows} />
          <Pager p={st.data} />
        </>
      ) : null}
    </>
  );
}

// airline → service → instance; the crumbs and the level machine subscribe per field, so a focus click
// (wb.inst) or an od toggle never re-runs a step it does not belong to
function DrillBody() {
  const lvl = drillLevel.value;
  if (lvl === "airlines") return <DrillAirlines />;
  if (lvl === "services") return <DrillServices />;
  return <DrillInstances />;
}

export function Drill() {
  return (
    <>
      <Crumbs />
      <DrillBody />
    </>
  );
}
