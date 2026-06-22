import { AMBER } from "./constants.js";

// Altitude lives inside the amber palette: deep orange on the deck → pale amber at cruise.
const ALT_RAMP = [
  [0, [224, 106, 0]],
  [20000, [255, 176, 0]],
  [40000, [255, 232, 176]],
];
export function parseAlt(alt_baro) {
  if (alt_baro === "ground") return 0;
  // the API serializes alt_baro as a string (mixed "ground"/number column upstream)
  const n = typeof alt_baro === "number" ? alt_baro : parseFloat(alt_baro);
  return Number.isFinite(n) ? Math.max(0, n) : null;
}
export const LABEL_ZOOM = 10.5;
export const LABEL_MAX = 40;
export function labelText(a) {
  const alt = parseAlt(a.alt_baro);
  const lvl = alt == null ? "" : alt >= 18000 ? ` FL${Math.round(alt / 100)}` : ` ${Math.round(alt)}ft`;
  return `${(a.flight || "").trim()}${lvl}`;
}
export function altTint(altFt) {
  if (altFt == null) return AMBER; // no baro alt → classic amber
  const x = Math.min(altFt, 40000);
  const i = x < 20000 ? 0 : 1;
  const [f0, c0] = ALT_RAMP[i];
  const [f1, c1] = ALT_RAMP[i + 1];
  const f = (x - f0) / (f1 - f0);
  return [0, 1, 2].map((k) => Math.round(c0[k] + f * (c1[k] - c0[k])));
}

// Pseudo-3D: the shadow walks toward screen-SE and shrinks as the aircraft climbs (sun fixed NW).
export const SHADOW_DIR = [0.45, 0.89];
const SHADOW_MAX_PX = 26;
export const shadowPx = (altFt) => Math.min(SHADOW_MAX_PX, (altFt ?? 0) / 1700);
