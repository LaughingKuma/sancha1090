// Vitest-only shared fixtures + per-case store reset (the node:test .mjs suites keep _support.mjs).
import { spyFacade } from "./_support.mjs";
import { readUrl } from "../../livemap/src/features/workbench/url";
import type { Instance } from "../../livemap/src/features/workbench/data";
import { dropFocus, flash, initStore, milAvailable, status, wb } from "../../livemap/src/features/workbench/store";

export { flush, restoreFetch, spyFacade, stubFetch } from "./_support.mjs";

export const EPOCH = 1785421800;
export const KEY = `86d3a1.${EPOCH}.ANA1`;

export const inst = (over: Partial<Instance> = {}): Instance => ({
  hex: "86d3a1", day: "2026-07-29", startTs: EPOCH, endTs: EPOCH + 3600, callsign: "ANA1",
  airline: "All Nippon Airways", reg: "JA801A", type: "B788",
  origin: { icao: "RJTT", iata: "HND", city: "Tokyo" }, dest: { icao: "RJOO", iata: "ITM", city: "Osaka" },
  o: "HND", d: "ITM", tier: "settled", gapS: 12, nPoints: 340, mil: false, flightId: "999", key: KEY,
  ...over,
});

// the one setup the component/store/legacy-view suites all hand-rolled: seed the URL, re-read wb, clear the
// runtime signals, tear down a leaked focus on its own facade, then wire a fresh one. Returns the facade.
export function resetStore(url = "/?wb=log&wb_d=all"): ReturnType<typeof spyFacade> {
  history.replaceState(null, "", url);
  wb.value = readUrl();
  flash.value = null;
  status.value = "";
  milAvailable.value = true;
  dropFocus();
  const api = spyFacade();
  initStore(api);
  return api;
}
