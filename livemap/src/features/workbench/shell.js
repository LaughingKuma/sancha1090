// The vanilla views' import surface while the adapter lasts: state, chrome helpers and the Preact-backed
// row/pager renderers. Every navigation entry point below is the store's — this file holds no state.
import { rangeParams as rangeParamsSignal } from "./store";

export { W, renderFlagRows, renderRows, renderPager } from "./adapter";
export { PAGE_LIMIT, doorway, navigate, openAirline, openAirport } from "./store";

// Callsigns, registrations and airport names are attacker-transmittable and view rows are built as HTML.
export function esc(v) {
  return String(v ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export const rangeParams = () => rangeParamsSignal.value;

export function panel(host) {
  const el = document.createElement("div");
  host.appendChild(el);
  return el; // detached by the next view render — every await checks isConnected before painting
}
