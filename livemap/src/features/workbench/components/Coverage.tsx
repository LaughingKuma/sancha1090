import { useMemo } from "preact/hooks";
import { rangeParams } from "../store";
import type { DayCounts, GapBin, Observed } from "../wire";
import { TIER_KEYS, fetchCoverage } from "../data";
import { drawn, useResource } from "../resource";
import {
  Chart, HUE, TIER_HUE, barsData, barsOpts, lineData, lineOpts, stackedData, stackedOpts,
} from "../chart";
import { Empty } from "./Empty";

const fmt = (n: number | null) => Number(n || 0).toLocaleString();
const daySecs = (d: string) => Date.parse(`${d}T00:00:00Z`) / 1000;
const pct = (v: number | null) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);

// Labels for the fixed bin edges (backend ruling: 900 s is the tier seam, so it is an exact edge).
const binLabel = (b: GapBin) => {
  const unit = (s: number) => (s % 3600 === 0 ? "h" : "m");
  const val = (s: number) => (s % 3600 === 0 ? s / 3600 : s / 60);
  // ge is inclusive: exact-12h gaps land here, but open at both ends spans everything, so no range names it
  if (b.lt == null) return b.ge === 0 ? "—" : `≥${val(b.ge)}${unit(b.ge)}`;
  if (b.ge === 0) return `<${val(b.lt)}${unit(b.lt)}`;
  // one unit on both sides reads as a single span: "1–5m", never "1m–5m"
  return unit(b.ge) === unit(b.lt)
    ? `${val(b.ge)}–${val(b.lt)}${unit(b.lt)}`
    : `${val(b.ge)}${unit(b.ge)}–${val(b.lt)}${unit(b.lt)}`;
};

const Section = ({ title }: { title: string }) => <div class="wb-sect">{title}</div>;

function TierMix({ daily, stale }: { daily: DayCounts[]; stale: boolean }) {
  const keys = useMemo(() => TIER_KEYS.filter((k) => daily.some(([, m]) => m[k])), [daily]);
  const seam = keys.join(",");
  const opts = useMemo(
    () => stackedOpts(seam === "" ? [] : seam.split(",").map((k) => ({ label: k, color: TIER_HUE[k] }))), [seam]);
  const data = useMemo(
    () => stackedData(daily.map(([d]) => daySecs(d)), keys.map((k) => daily.map(([, m]) => m[k] || 0))),
    [daily, keys],
  );
  const totals = useMemo(() => {
    const t: Record<string, number> = {};
    for (const [, m] of daily) for (const [k, n] of Object.entries(m)) t[k] = (t[k] || 0) + n;
    return t;
  }, [daily]);
  return (
    <>
      <Section title="tier mix per day" />
      {daily.length ? <Chart class="wb-chart" h={160} opts={opts} data={data} /> : null}
      {stale ? null : !daily.length ? <div class="wb-empty">no flights in range</div> : (
        <div class="wb-tmix wb-legend">
          {TIER_KEYS.filter((k) => totals[k]).map((k) => (
            <span key={k} class={`t-${k}`}>{`${k} ${fmt(totals[k])}`}</span>
          ))}
        </div>
      )}
    </>
  );
}

function GapHist({ bins, stale }: { bins: GapBin[]; stale: boolean }) {
  const labels = useMemo(() => bins.map(binLabel), [bins]);
  const seam = labels.join("\n");
  const opts = useMemo(() => barsOpts(seam === "" ? [] : seam.split("\n"), HUE.settled), [seam]);
  const data = useMemo(() => barsData(bins.map((b) => b.n)), [bins]);
  const drawable = bins.some((b) => b.n);
  return (
    <>
      <Section title="largest gap" />
      {drawable ? <Chart class="wb-chart" h={130} opts={opts} data={data} /> : null}
      {stale ? null : !drawable ? <div class="wb-empty">no measured gaps in range</div> : (
        <>
          {/* uPlot paints the bin labels on canvas, so the same binLabel() spells them in the DOM too */}
          <div class="wb-tmix wb-legend wb-gapbins">
            {bins.map((b, i) => <span key={b.ge}>{`${labels[i]} ${fmt(b.n)}`}</span>)}
          </div>
          {/* the 15m edge is the settled/estimated tier seam — name it so the shape is readable, not decorative */}
          <div class="wb-note">15m is the settled/estimated tier seam</div>
        </>
      )}
    </>
  );
}

function ObservedFraction({ obs, stale }: { obs: Observed[]; stale: boolean }) {
  const opts = useMemo(() => lineOpts([{ label: "median", color: HUE.provisional }]), []);
  const data = useMemo(() => lineData(obs.map((r) => daySecs(r.day)), [obs.map((r) => r.median)]), [obs]);
  const last = obs[obs.length - 1];
  return (
    <>
      <Section title="observed fraction (median)" />
      {obs.length > 1 ? <Chart class="wb-chart" h={160} opts={opts} data={data} /> : null}
      {stale ? null : !obs.length ? <div class="wb-empty">no measured coverage in range</div> : (
        <div class="wb-note">{`${last.day} ${pct(last.median)} · n=${fmt(last.n)}`}</div>
      )}
    </>
  );
}

const coverageParams = () => ({ ...rangeParams.value });
const coverageFetch = (p: ReturnType<typeof coverageParams>, signal: AbortSignal) => fetchCoverage(p, signal);

export function Coverage() {
  const st = useResource(coverageParams, coverageFetch);
  const { p, stale } = drawn(st);
  if (st.state === "outage" || st.state === "unreachable") return <Empty what="coverage" state={st.state} />;
  if (!p) return null;
  if (!p.available) return stale ? null : <div class="wb-empty">tier mart not deployed</div>;
  return (
    <>
      <TierMix daily={p.tierDaily} stale={stale} />
      <GapHist bins={p.gapBins} stale={stale} />
      <ObservedFraction obs={p.observed} stale={stale} />
    </>
  );
}
