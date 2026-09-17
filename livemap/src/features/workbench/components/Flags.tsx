import { PAGE_LIMIT, activeFlagClass, activePage, navigate, rangeParams } from "../store";
import { FLAG_CLASSES, type FlagClass } from "../url";
import type { Counts } from "../wire";
import { fetchFlags } from "../data";
import { drawn, useResource } from "../resource";
import { Empty } from "./Empty";
import { InstanceList } from "./InstanceList";
import { Pager } from "./Pager";

// the wire/URL value is the mart's exact class name — only the label loses the underscores
const label = (c: string) => String(c || "").replaceAll("_", " ");

function Chips({ classes }: { classes: Counts }) {
  const active = activeFlagClass.value;
  // the all-chip total counts flag ROWS (a multi-class flight counts once per class), which is the
  // grain the pager below counts in — the overview strip's "flagged" counts distinct flights
  const rowTotal = Object.values(classes).reduce((a, n) => a + n, 0);
  const pick = (c: FlagClass | null) => navigate({ flagClass: c, page: 1 });
  return (
    <div class="wb-classes">
      <button type="button" class="wb-chip" data-cls="" aria-pressed={!active} onClick={() => pick(null)}>
        {`all ${rowTotal.toLocaleString()}`}
      </button>
      {FLAG_CLASSES.map((c) => (
        <button key={c} type="button" class="wb-chip" data-cls={c} aria-pressed={active === c}
          onClick={() => pick(c)}>
          {`${label(c)} ${(classes[c] || 0).toLocaleString()}`}
        </button>
      ))}
    </div>
  );
}

const flagParams = () => ({
  ...rangeParams.value,
  class: activeFlagClass.value,
  limit: PAGE_LIMIT,
  offset: (activePage.value - 1) * PAGE_LIMIT,
});
const flagFetch = (p: ReturnType<typeof flagParams>, signal: AbortSignal) => fetchFlags(p, signal);

export function Flags() {
  const st = useResource(flagParams, flagFetch);
  const { p, stale } = drawn(st);
  if (st.state === "outage" || st.state === "unreachable") return <Empty what="flags" state={st.state} />;
  if (!p || stale) return null;
  return (
    <>
      <Chips classes={p.classes} />
      {!p.available ? <div class="wb-empty">flags mart not deployed</div> : (
        <>
          <InstanceList rows={p.rows} flag />
          <Pager p={p} />
        </>
      )}
    </>
  );
}
