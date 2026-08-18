// Deep-link candidate selection, extracted pure for the node harness.
export const csNorm = (c) => String(c || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");

export function pickCandidate(rows, epoch, csKey) {
  // nearest |Δ| wins, not first-in-sort (2,766 live rows have a newer same-hex start inside ±900s)
  let cands = rows
    .filter((r) => r.startTs != null && Math.abs(r.startTs - epoch) <= 900)
    .sort((a, b) => Math.abs(a.startTs - epoch) - Math.abs(b.startTs - epoch) || a.startTs - b.startTs);
  if (csKey) {
    // a key that names a callsign only ever resolves to that callsign — a sole nonmatching
    // neighbor (2,885 live cases) is a wrong flight, not a fallback
    const named = cands.filter((r) => csNorm(r.callsign) === csNorm(csKey));
    cands = named;
  } else if (cands.length > 1
      && Math.abs(cands[0].startTs - epoch) === Math.abs(cands[1].startTs - epoch)
      && csNorm(cands[0].callsign) !== csNorm(cands[1].callsign)) {
    cands = []; // a legacy 2-part key aliasing two flights — reject over a repeatable wrong pick
  }
  return cands[0] || null;
}
