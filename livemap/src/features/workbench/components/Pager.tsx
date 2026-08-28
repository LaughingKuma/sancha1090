import { navigate, wb } from "../store";

export interface PageInfo { rows: unknown[]; total: number; offset: number }

// the bar describes the envelope it stands under (the URL may already be a page ahead while that page loads)
export function Pager({ p }: { p: PageInfo }) {
  if (p.total <= p.rows.length && p.offset === 0) return null;
  // an offset past the data (stale deep link) reads 0–0, prev stays live for recovery
  const from = p.rows.length ? p.offset + 1 : 0;
  const to = p.rows.length ? Math.min(p.offset + p.rows.length, p.total) : 0;
  const step = (d: number) => navigate({ page: Math.max(1, wb.peek().page + d) });
  return (
    <div class="wb-pager">
      <button type="button" class="wb-chip" data-step="-1" disabled={p.offset === 0} onClick={() => step(-1)}>◂ prev</button>
      <button type="button" class="wb-chip" data-step="1" disabled={p.offset + p.rows.length >= p.total}
        onClick={() => step(1)}>next ▸</button>
      <span class="wb-count">{from}–{to} of {p.total.toLocaleString()}</span>
    </div>
  );
}
