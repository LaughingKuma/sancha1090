import { useMemo } from "preact/hooks";
import { activeDim, activePage, doorway, navigate, openAirline, openAirport, rangeParams } from "../store";
import { DIMS, type Dim } from "../url";
import { fetchTrends, type RankRow, type Trends as TrendsPage } from "../data";
import { drawn, useResource } from "../resource";
import { Chart, SERIES_HUES, lineData, lineOpts } from "../chart";
import { Empty } from "./Empty";
import { Pager } from "./Pager";
import { RowList } from "./RowList";

// rank rows are wide (key · n · aircraft · Δ), so trends pages at 20 while the lists page at 50
const TREND_LIMIT = 20;
const daySecs = (d: string) => Date.parse(`${d}T00:00:00Z`) / 1000;
const deltaText = (d: number | null) =>
  d == null ? "—" : `${d > 0 ? "+" : d < 0 ? "−" : ""}${Math.abs(d).toFixed(1)}%`;

function Dims({ dim }: { dim: Dim }) {
  return (
    <div class="wb-dims">
      {DIMS.map((d) => (
        <button key={d} type="button" class="wb-chip" data-dim={d} aria-pressed={dim === d}
          onClick={() => d !== dim && navigate({ dim: d, page: 1 })}>{d}</button>
      ))}
    </div>
  );
}

function TrendChart({ series }: { series: TrendsPage["series"] }) {
  const top = useMemo(() => series.slice(0, 5), [series]);
  // the union of the drawn keys' days: a key idle on a day reads as a gap, not as a zero
  const days = useMemo(() => [...new Set(top.flatMap((s) => s.points.map(([d]) => d)))].sort(), [top]);
  // keys are free-form wire text, so the dep round-trips through JSON: a fetch that keeps the same
  // series set keeps one opts identity, which is what decides rebuild-vs-setData
  const seam = JSON.stringify(top.map((s) => s.key));
  const opts = useMemo(
    () => lineOpts((JSON.parse(seam) as string[]).map((label, i) => (
      { label, color: SERIES_HUES[i % SERIES_HUES.length] }
    ))),
    [seam],
  );
  const data = useMemo(
    () => lineData(days.map(daySecs), top.map((s) => {
      const by = new Map(s.points);
      return days.map((d) => (by.has(d) ? by.get(d)! : null));
    })),
    [days, top],
  );
  if (!days.length) return <div class="wb-chart" />;
  return <Chart class="wb-chart" h={160} opts={opts} data={data} />;
}

const rankRow = (r: RankRow) => ({
  key: r.key,
  name: r.key || "—",
  n: r.n.toLocaleString(),
  sub: `${r.distinctAircraft.toLocaleString()} aircraft`,
  delta: deltaText(r.deltaPct),
  cls: "wb-rank",
});

function pick(dim: Dim, r: RankRow) {
  if (dim === "airline") return openAirline(r.key);
  if (dim === "airport") return openAirport(r.key);
  // the rank row counts the whole window, so the doorway carries the route and nothing else
  doorway("log", { od: r.key });
}

const trendParams = () => ({
  ...rangeParams.value,
  dim: activeDim.value,
  limit: TREND_LIMIT,
  offset: (activePage.value - 1) * TREND_LIMIT,
});
const trendFetch = (p: ReturnType<typeof trendParams>, signal: AbortSignal) => fetchTrends(p, signal);

export function Trends() {
  const dim = activeDim.value;
  const st = useResource(trendParams, trendFetch);
  const { p, stale } = drawn(st);
  return (
    <>
      {/* the chips read the URL, not the envelope, so a fetch in flight can never make them stale */}
      <Dims dim={dim} />
      {st.state === "outage" || st.state === "unreachable" ? <Empty what="trends" state={st.state} /> : null}
      {p ? <TrendChart series={p.series} /> : null}
      {p && !stale ? (
        <>
          <RowList rows={p.rank.map(rankRow)} onPick={(i) => pick(dim, p.rank[i])} empty="no trends in range" />
          <Pager p={{ rows: p.rank, total: p.total, offset: p.offset }} />
        </>
      ) : null}
    </>
  );
}
