import { test } from "node:test";
import assert from "node:assert/strict";
import { pickCandidate, csNorm } from "../../livemap/static/features/workbench/resolve.js";

const row = (startTs, callsign) => ({ startTs, callsign });

test("csNorm: trims, uppercases, strips non-alnum", () => {
  assert.equal(csNorm(" ana123 "), "ANA123");
  assert.equal(csNorm("jl-456"), "JL456");
  assert.equal(csNorm(null), "");
});

test("nearest |Δ| wins over first-in-sort", () => {
  const rows = [row(1050, "BBB"), row(940, "CCC"), row(1000, "AAA")]; // exact match listed last
  const hit = pickCandidate(rows, 1000, null);
  assert.equal(hit.callsign, "AAA"); // exact match, |Δ|=0, not the first row in the input
});

test("nearest |Δ| among off-epoch candidates", () => {
  const rows = [row(1300, "AAA"), row(1090, "BBB")]; // |Δ|=300 vs |Δ|=90 from epoch=1000
  const hit = pickCandidate(rows, 1000, null);
  assert.equal(hit.callsign, "BBB");
});

test("900s cutoff excludes anything farther, leaving null", () => {
  const rows = [row(1901, "AAA")]; // |Δ|=901, just past the cutoff
  assert.equal(pickCandidate(rows, 1000, null), null);
});

test("900s cutoff is inclusive at exactly 900", () => {
  const rows = [row(1900, "AAA")]; // |Δ|=900, exactly at the cutoff
  const hit = pickCandidate(rows, 1000, null);
  assert.equal(hit.callsign, "AAA");
});

test("csKey names a callsign — a sole nonmatching neighbor resolves to null, not a fallback", () => {
  const rows = [row(1010, "BBB")]; // in range, but wrong callsign
  assert.equal(pickCandidate(rows, 1000, "AAA"), null);
});

test("csKey exact-match-only: matches survive even when not nearest", () => {
  const rows = [row(1005, "BBB"), row(1200, "AAA")];
  const hit = pickCandidate(rows, 1000, "AAA");
  assert.equal(hit.callsign, "AAA"); // nearer candidate has the wrong callsign, filtered out
});

test("legacy 2-part key: tied |Δ| with differing callsigns and no csKey is ambiguous -> null", () => {
  const rows = [row(900, "AAA"), row(1100, "BBB")]; // both |Δ|=100
  assert.equal(pickCandidate(rows, 1000, null), null);
});

test("tied |Δ| with matching callsigns is not ambiguous — startTs-asc tiebreak picks the earlier", () => {
  const rows = [row(1100, "AAA"), row(900, "AAA")]; // both |Δ|=100, same callsign
  const hit = pickCandidate(rows, 1000, null);
  assert.equal(hit.startTs, 900); // earlier start wins the tiebreak
});

test("no rows within the window resolves null", () => {
  assert.equal(pickCandidate([], 1000, null), null);
  assert.equal(pickCandidate([row(5000, "AAA")], 1000, null), null);
});
