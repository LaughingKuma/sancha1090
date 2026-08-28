import type { Counts } from "../wire";
import { TIER_GLYPH } from "../store";
import { Empty } from "./Empty";

export interface RowSpec {
  key: string;
  name: string;
  n: string;
  sub?: string;
  subClass?: string; // the service rows carry their O/D in the .wb-sub-od cell
  tiers?: Counts;
  delta?: string;
  cls?: string; // wb-rank on the trends ranking
}

// the mix reads as one chip: a glyph per tier with its count, the title spelling the pairs out
function TierMix({ tiers }: { tiers: Counts }) {
  const parts = Object.entries(tiers).filter(([, n]) => n);
  if (!parts.length) return null;
  return (
    <span class="wb-tmix" title={parts.map(([k, n]) => `${k} ${n}`).join(" · ")}>
      {parts.map(([k, n]) => (
        <span key={k} class={`t-${k}`}>{TIER_GLYPH[k] || "·"}{n.toLocaleString()}</span>
      ))}
    </span>
  );
}

// The generic two-line row list behind every non-instance feed (airlines, services, trend ranks).
export function RowList({ rows, onPick, empty }: { rows: RowSpec[]; onPick: (i: number) => void; empty: string }) {
  // the caller hands a whole sentence ("no trends in range"), never a noun — so it is the wording, not `what`
  if (!rows.length) return <Empty empty={empty} />;
  return (
    <>
      {rows.map((r, i) => (
        // the row key is the caller's name, which repeats and can be blank — position keeps it unique
        <button key={`${i}:${r.key}`} type="button" class={`wb-row wb-stack${r.cls ? ` ${r.cls}` : ""}`} onClick={() => onPick(i)}>
          <span class="wb-row-main">
            <span class="wb-name">{r.name || "—"}</span>
            <span class="wb-n">{r.n}</span>
          </span>
          <span class="wb-row-sub">
            <span class={r.subClass}>{r.sub}</span>
            {r.tiers && <TierMix tiers={r.tiers} />}
            {r.delta != null && <span class="wb-delta">{r.delta}</span>}
          </span>
        </button>
      ))}
    </>
  );
}
