import type { ComponentChildren } from "preact";
import { useMemo } from "preact/hooks";
import { doorway, rangeParams } from "../store";
import type { FlagClass } from "../url";
import type { Counts } from "../wire";
import { TIER_KEYS, fetchFlags, fetchSummary, type FlagsPage, type Mover, type Summary } from "../data";
import { drawn, useResource, type Fetched } from "../resource";
import { Chart, HUE, TIER_HUE, sparkData, sparkOpts, stackedData, stackedOpts, type Values } from "../chart";
import { Empty } from "./Empty";
import { InstanceList } from "./InstanceList";

// A strip number counts the whole window, so its doorway carries no scope at all — the doorway table
// resets every key the target reads, or an unfiltered headline would open a filtered list.
const fmt = (n: number | null) => Number(n || 0).toLocaleString();
const daySecs = (d: string) => Date.parse(`${d}T00:00:00Z`) / 1000;
const deltaText = (d: number | null) =>
  d == null ? "—" : `${d > 0 ? "+" : d < 0 ? "−" : ""}${Math.abs(d).toFixed(1)}%`;

function Microbar({ tiers }: { tiers: Summary["tiers"] }) {
  const parts = Object.entries(tiers.mix).filter(([, n]) => n);
  if (!tiers.available || !parts.length) return <span class="wb-cell-v">—</span>;
  const tot = parts.reduce((a, [, n]) => a + n, 0);
  return (
    <span class="wb-microbar" title={parts.map(([k, n]) => `${k} ${fmt(n)}`).join(" · ")}>
      {parts.map(([k, n]) => <span key={k} class={`t-${k}`} style={`width:${((n / tot) * 100).toFixed(2)}%`} />)}
    </span>
  );
}

function Strip({ s }: { s: Summary }) {
  const cells: { k: string; v: string; go?: () => void }[] = [
    { k: "flights", v: fmt(s.flights), go: () => doorway("log") },
    // services/aircraft stay plain: no view serves a window-scoped list of either, and a doorway
    // whose destination disagrees with its number is worse than no doorway (review round 2)
    { k: "services", v: fmt(s.services) },
    { k: "aircraft", v: fmt(s.aircraft) },
    { k: "flagged", v: s.flags.available ? fmt(s.flags.flagged) : "—", go: () => doorway("flags") },
    { k: "est err", v: s.est.available && s.est.errP50Km != null ? `${s.est.errP50Km.toFixed(2)} km` : "—" },
  ];
  return (
    <div class="wb-strip">
      {cells.map((c) =>
        c.go ? (
          <button key={c.k} type="button" class="wb-cell wb-cell-go" onClick={c.go}>
            <span class="wb-cell-v">{c.v}</span><span class="wb-cell-k">{c.k}</span>
          </button>
        ) : (
          <span key={c.k} class="wb-cell">
            <span class="wb-cell-v">{c.v}</span><span class="wb-cell-k">{c.k}</span>
          </span>
        ))}
      <span class="wb-cell"><Microbar tiers={s.tiers} /><span class="wb-cell-k">tiers</span></span>
    </div>
  );
}

function Panel({ title, go, children }: { title: string; go: () => void; children: ComponentChildren }) {
  return (
    <div class="wb-panel">
      <button type="button" class="wb-panel-head" onClick={go}>{title}<span class="wb-panel-go">▸</span></button>
      <div class="wb-panel-body">{children}</div>
    </div>
  );
}

function Spark({ ys, color }: { ys: Values; color: string }) {
  const opts = useMemo(() => sparkOpts(color), [color]);
  const data = useMemo(() => sparkData(ys), [ys]);
  return <Chart class="wb-spark" h={28} opts={opts} data={data} />;
}

function ClassChips({ classes }: { classes: Counts }) {
  const entries = Object.entries(classes).filter(([, n]) => n);
  if (!entries.length) return null;
  return (
    <div class="wb-classes">
      {entries.map(([c, n]) => (
        <button key={c} type="button" class="wb-chip" onClick={() => doorway("flags", { flagClass: c as FlagClass })}>
          {`${c.replaceAll("_", " ")} ${fmt(n)}`}
        </button>
      ))}
    </div>
  );
}

const flagParams = () => ({ ...rangeParams.value, limit: 5 });
const flagFetch = (p: ReturnType<typeof flagParams>, signal: AbortSignal) => fetchFlags(p, signal);

function FlagsPanel({ st }: { st: Fetched<FlagsPage> }) {
  const { p, stale } = drawn(st);
  return (
    <Panel title="flags" go={() => doorway("flags")}>
      {st.state === "outage" || st.state === "unreachable" ? <Empty what="flags" state={st.state} /> : null}
      {!p || stale ? null : !p.available ? (
        <div class="wb-empty">flags mart not deployed</div>
      ) : (
        <>
          <InstanceList rows={p.rows} flag />
          <ClassChips classes={p.classes} />
        </>
      )}
    </Panel>
  );
}

function MoverRows({ movers }: { movers: Mover[] }) {
  const top = movers.slice(0, 3);
  if (!top.length) return <div class="wb-empty">no routes in range</div>;
  return (
    <>
      {top.map((r, i) => (
        <button key={`${i}:${r.key}`} type="button" class="wb-row wb-rank"
          onClick={() => doorway("log", { od: r.key })}>
          <span class="wb-name">{r.key || "—"}</span>
          <span class="wb-n">{fmt(r.n)}</span>
          <span class="wb-delta">{deltaText(r.deltaPct)}</span>
        </button>
      ))}
    </>
  );
}

function TrendsPanel({ s, stale }: { s: Summary; stale: boolean }) {
  const ys = useMemo(() => s.daily.map(([, n]) => n), [s.daily]);
  return (
    <Panel title="trends" go={() => doorway("trends")}>
      {ys.length > 1 && <Spark ys={ys} color={HUE.settled} />}
      {stale ? null : <MoverRows movers={s.movers} />}
    </Panel>
  );
}

function EstPanel({ s, stale }: { s: Summary; stale: boolean }) {
  const est = s.est;
  const ys = useMemo(() => (est.available ? est.daily.map(([, p]) => p) : []), [est]);
  const p50 = est.available ? est.errP50Km : null;
  return (
    // view navigation only — the two new views carry the rail's range, never a list scope
    <Panel title="estimates" go={() => doorway("estimates")}>
      {p50 != null && ys.length > 1 && <Spark ys={ys} color={HUE.estimated} />}
      {stale ? null : p50 == null ? (
        <div class="wb-empty">—</div>
      ) : (
        <div class="wb-note">{`p50 ${p50.toFixed(2)} km · n=${fmt(est.n)}`}</div>
      )}
    </Panel>
  );
}

function CoveragePanel({ s, stale }: { s: Summary; stale: boolean }) {
  const daily = s.tiers.daily;
  const keys = useMemo(() => TIER_KEYS.filter((k) => daily.some(([, m]) => m[k])), [daily]);
  const seam = keys.join(",");
  const opts = useMemo(
    () => stackedOpts(seam ? seam.split(",").map((k) => ({ label: k, color: TIER_HUE[k] })) : []), [seam]);
  const data = useMemo(
    () => stackedData(daily.map(([d]) => daySecs(d)), keys.map((k) => daily.map(([, m]) => m[k] || 0))),
    [daily, keys],
  );
  const drawable = s.tiers.available && daily.length > 0;
  return (
    <Panel title="coverage" go={() => doorway("coverage")}>
      {drawable && <Chart class="wb-chart" h={160} opts={opts} data={data} />}
      {stale || drawable ? null : <div class="wb-empty">—</div>}
    </Panel>
  );
}

const summaryParams = () => ({ ...rangeParams.value });
const summaryFetch = (p: ReturnType<typeof summaryParams>, signal: AbortSignal) => fetchSummary(p, signal);

export function Overview() {
  // both feeds are claimed before the summary is read, so they fly together as the vanilla Promise.all did
  const st = useResource(summaryParams, summaryFetch);
  const flags = useResource(flagParams, flagFetch);
  const { p, stale } = drawn(st);
  if (st.state === "outage" || st.state === "unreachable") return <Empty what="overview" state={st.state} />;
  if (!p) return null;
  return (
    <>
      {stale ? null : <Strip s={p} />}
      <FlagsPanel st={flags} />
      <TrendsPanel s={p} stale={stale} />
      <EstPanel s={p} stale={stale} />
      <CoveragePanel s={p} stale={stale} />
    </>
  );
}
