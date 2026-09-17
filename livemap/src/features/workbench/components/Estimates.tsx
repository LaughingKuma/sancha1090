import { Fragment } from "preact";
import { useMemo } from "preact/hooks";
import { rangeParams } from "../store";
import type { MixRow } from "../wire";
import { fetchEstimates, type Estimates as EstimatesPage } from "../data";
import { drawn, useResource } from "../resource";
import { Chart, SERIES_HUES, lineData, lineOpts } from "../chart";
import { Empty } from "./Empty";

const MIX_DIMS: [string, string][] = [
  ["skip", "skips"],
  ["segment_kind", "segment kind"],
  ["uncertainty_bin", "uncertainty bin"],
];
// the rail is a column, not a report — a long tail scrolls the whole view out of reach
const MIX_ROWS = 12;
const fmt = (n: number | null) => Number(n || 0).toLocaleString();
const km = (v: number | null) => (v == null ? "—" : `${v.toFixed(2)} km`);
const daySecs = (d: string) => Date.parse(`${d}T00:00:00Z`) / 1000;
// the hash is an opaque 20-digit key — the head identifies the era, the title carries all of it
const shortCfg = (h: string) => String(h || "").slice(0, 8);

const Section = ({ title }: { title: string }) => <div class="wb-sect">{title}</div>;

function Eras({ eras }: { eras: EstimatesPage["headline"] }) {
  if (!eras.length) return <div class="wb-empty">no scored estimates in range</div>;
  return (
    <>
      {/* first row is the era still in force (the query orders latest-last-seen first) */}
      {eras.map((e, i) => (
        <div key={`${i}:${e.configHash}`} class={`wb-era${i === 0 ? " wb-era-cur" : ""}`}
          title={`config ${e.configHash}`}>
          <span class="wb-row-main">
            <span class="wb-name">{shortCfg(e.configHash)}</span>
            <span class="wb-era-p">{`p50 ${km(e.p50Km)}`}</span>
            <span class="wb-era-p wb-era-p90">{`p90 ${km(e.p90Km)}`}</span>
          </span>
          <span class="wb-row-sub">
            <span>{`${e.firstDay} → ${e.lastDay}`}</span>
            <span>{`n=${fmt(e.n)}`}</span>
          </span>
        </div>
      ))}
    </>
  );
}

function EraChart({ daily }: { daily: EstimatesPage["daily"] }) {
  const days = useMemo(() => [...new Set(daily.map((r) => r.day))].sort(), [daily]);
  const configs = useMemo(() => [...new Set(daily.map((r) => r.configHash))], [daily]);
  // the config hash is wire text, so the dep round-trips through JSON: a fetch that keeps the same
  // era set keeps one opts identity, which is what decides rebuild-vs-setData
  const seam = JSON.stringify(configs);
  const opts = useMemo(
    () => lineOpts((JSON.parse(seam) as string[]).flatMap((cfg, i) => {
      const color = SERIES_HUES[i % SERIES_HUES.length];
      return [{ label: `${shortCfg(cfg)} p50`, color }, { label: `${shortCfg(cfg)} p90`, color, dash: [4, 3] }];
    })),
    [seam],
  );
  const data = useMemo(() => {
    const at = new Map(daily.map((r) => [`${r.configHash}|${r.day}`, r]));
    // a config's series is null outside its own era, so an instrument change draws as a break
    const arm = (cfg: string, f: "p50Km" | "p90Km") => days.map((d) => at.get(`${cfg}|${d}`)?.[f] ?? null);
    return lineData(days.map(daySecs), configs.flatMap((cfg) => [arm(cfg, "p50Km"), arm(cfg, "p90Km")]));
  }, [daily, days, configs]);
  if (days.length < 2) return null;
  return (
    <>
      <Chart class="wb-chart" h={160} opts={opts} data={data} />
      <div class="wb-note">solid p50 · dashed p90 · one colour per config era</div>
    </>
  );
}

function Outcomes({ o, split }: { o: EstimatesPage["outcomes"]; split: EstimatesPage["inputSplit"] }) {
  // raw logged rows, not the deduped scored pool — the two counts answer different questions
  const cells = [
    { k: "settled", v: fmt(o.settled) },
    { k: "awaiting", v: fmt(o.awaiting) },
    { k: "ambiguous", v: fmt(o.ambiguous) },
    { k: "prov in", v: fmt(split.provisional) },
    { k: "settled in", v: fmt(split.settled) },
  ];
  return (
    <div class="wb-strip">
      {cells.map((c) => (
        <span key={c.k} class="wb-cell">
          <span class="wb-cell-v">{c.v}</span><span class="wb-cell-k">{c.k}</span>
        </span>
      ))}
    </div>
  );
}

function MixPanel({ dim, rows }: { dim: string; rows: MixRow[] }) {
  if (!rows.length) return <div class="wb-empty">nothing logged</div>;
  const shown = rows.slice(0, MIX_ROWS);
  return (
    <div class={`wb-mix wb-mix-${dim}`}>
      {shown.map((r, i) => (
        <div key={`${i}:${r.value}`} class="wb-kv">
          <span class="wb-kv-k">{r.value}</span>
          <span class="wb-kv-p">{r.producer || "—"}</span>
          <span class="wb-kv-n">{fmt(r.n)}</span>
        </div>
      ))}
      {rows.length > shown.length ? <div class="wb-note">{`+${fmt(rows.length - shown.length)} more`}</div> : null}
    </div>
  );
}

function Mix({ mix }: { mix: EstimatesPage["mix"] }) {
  if (!mix.available) {
    return (
      <>
        <Section title="mix" />
        <div class="wb-empty">breakdown mart not deployed</div>
      </>
    );
  }
  return (
    <>
      {MIX_DIMS.map(([dim, title]) => (
        <Fragment key={dim}>
          <Section title={title} />
          <MixPanel dim={dim} rows={(mix[dim] || []) as MixRow[]} />
        </Fragment>
      ))}
      {/* the breakdown mart is UTC-day grain, the series above is JST — say so rather than hide the skew */}
      <div class="wb-note">mix counts are UTC-day grain; the series above is JST</div>
    </>
  );
}

const estParams = () => ({ ...rangeParams.value });
const estFetch = (p: ReturnType<typeof estParams>, signal: AbortSignal) => fetchEstimates(p, signal);

export function Estimates() {
  const st = useResource(estParams, estFetch);
  const { p, stale } = drawn(st);
  if (st.state === "outage" || st.state === "unreachable") return <Empty what="estimates" state={st.state} />;
  if (!p) return null;
  if (!p.available) return stale ? null : <div class="wb-empty">estimate ledger not deployed</div>;
  return (
    <>
      <Section title="config eras" />
      {stale ? null : <Eras eras={p.headline} />}
      <EraChart daily={p.daily} />
      <Section title="logging stream" />
      {stale ? null : <Outcomes o={p.outcomes} split={p.inputSplit} />}
      {stale ? null : <Mix mix={p.mix} />}
    </>
  );
}
