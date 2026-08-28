import { activeKey, flash, focusInstance, jstDayOf, TIER_LABEL } from "../store";
import type { FlagInstance, Instance } from "../data";
import { Empty } from "./Empty";

// the one place the tier pill, the day and the O/D are spelled, so a row and the focus bar can never disagree
export const TierPill = ({ tier }: { tier: string }) => (
  <span class={`wb-tier t-${tier}`}>{TIER_LABEL[tier] || TIER_LABEL.unknown}</span>
);
export const instDay = (r: { day: string; startTs: number | null }) => r.day || jstDayOf(r.startTs);
export const instOd = (r: { o: string; d: string }) => `${r.o || "?"} → ${r.d || "?"}`;

const label = (c: string) => String(c || "").replaceAll("_", " ");

// a flag row is a (flight, class) pair: a multi-class flight must not share one key or one flash
const rowKey = (r: Instance | FlagInstance, flag: boolean): string | null => {
  const id = r.key ?? r.flightId;
  return id != null && flag ? `${id}:${(r as FlagInstance).flagClass}` : id;
};

function Row({ r, flag }: { r: Instance | FlagInstance; flag: boolean }) {
  const on = !!r.key && r.key === activeKey.value; // a re-render (pager, filter) must keep the focused row lit
  const fl = flash.value;
  const rk = rowKey(r, flag);
  const nopath = fl && fl.key === rk ? fl.msg : null;
  const day = instDay(r);
  const od = instOd(r);
  const meta = [r.reg, r.type, r.nPoints == null ? "" : `${r.nPoints.toLocaleString()} pts`]
    .filter(Boolean)
    .join(" · ");
  const title = [r.hex, r.airline, r.origin.city || r.origin.icao, r.dest.city || r.dest.icao,
    r.gapS == null ? "" : `gap ${r.gapS}s`].filter(Boolean).join(" · ");
  return (
    <button type="button" class={`wb-row wb-stack wb-inst${on ? " wb-active" : ""}`} aria-pressed={on}
      title={flag ? undefined : title || undefined} onClick={() => focusInstance(r, rk)}>
      <span class="wb-row-main">
        <span class="wb-date">{day.slice(5) || "—"}</span>
        <span class="wb-name">{r.callsign || r.hex || "—"}</span>
        {flag
          ? <span class="wb-flagcls">{label((r as FlagInstance).flagClass)}</span>
          : r.mil && <span class="wb-tier t-mil">MIL</span>}
        <span class="wb-n">{flag ? od : <TierPill tier={r.tier} />}</span>
      </span>
      <span class="wb-row-sub">
        {flag
          ? <span class={nopath ? "wb-detail wb-nopath" : "wb-detail"}>{nopath ?? ((r as FlagInstance).detail || "—")}</span>
          : <span class={nopath ? "wb-sub-od wb-nopath" : "wb-sub-od"}>{nopath ?? od}</span>}
        {!flag && <span>{meta}</span>}
      </span>
    </button>
  );
}

// One list for both instance feeds: the flag feed swaps the middle cells but keeps the click, the
// lighting and the transient no-path line.
export function InstanceList({ rows, flag = false }: { rows: (Instance | FlagInstance)[]; flag?: boolean }) {
  if (!rows.length) {
    return flag
      ? <Empty what="flags" empty="no flagged instances" />
      : <Empty what="instances" empty="no instances match" />;
  }
  return <>{rows.map((r, i) => <Row key={rowKey(r, flag) ?? `i${i}`} r={r} flag={flag} />)}</>;
}
