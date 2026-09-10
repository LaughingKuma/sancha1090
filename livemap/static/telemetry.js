// One home for the telemetry decoders so the card and the map never disagree on a contact's state.

// ICAO emergency squawks — always-on alert set. squawk arrives as a STRING in /aircraft.
const EMERGENCY_SQUAWKS = { "7700": "General", "7600": "Radio Fail", "7500": "Hijack" };
// DO-260B emergency/priority status — transmitted on its own, no 7x00 squawk needed. readsb spells
// medical/no-comm as lifeguard/nordo on the wire, so both spellings map; anything else non-'none' still alerts.
const EMERGENCY_STATES = {
  general: "General", medical: "Medical", lifeguard: "Medical", minfuel: "Min Fuel", nocomm: "No Comm",
  nordo: "No Comm", unlawful: "Unlawful", downed: "Downed",
};
const titleCase = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// The squawk outranks the transmitted status for the code slot: a 7x00 is the crew's deliberate signal.
export function emergencyOf(a) {
  const code = a && a.squawk != null ? String(a.squawk).trim() : "";
  // own-property guard: a spoofed squawk like "toString"/"constructor" must not match a prototype member
  const label = Object.hasOwn(EMERGENCY_SQUAWKS, code) ? EMERGENCY_SQUAWKS[code] : null;
  if (label) return { code, label };
  const raw = a && a.emergency;
  if (raw == null || raw === "none") return null; // readsb's default for nearly every airframe, per frame
  const st = String(raw).trim().toLowerCase();
  if (!st || st === "none") return null;
  return { code: "EMERG", label: Object.hasOwn(EMERGENCY_STATES, st) ? EMERGENCY_STATES[st] : titleCase(st) };
}

// position_source → normalized kind ('mlat'/'adsb') or null. The ONE normalizer, so the label and
// the teal styling can't drift apart on backend casing/whitespace.
export const sourceKind = (s) => {
  const v = String(s ?? "").trim().toLowerCase();
  return v === "mlat" || v === "adsb" ? v : null;
};
export const sourceLabel = (s) => {
  const kind = sourceKind(s);
  return kind === "mlat" ? "MLAT" : kind === "adsb" ? "ADS-B" : "—";
};

// Real vertical rate: barometric preferred, geometric fallback (~95% combined coverage). ft/min or null.
export const verticalRate = (a) => a?.baro_rate ?? a?.geom_rate ?? null;
const VS_THRESH_FPM = 300; // matches the retired trail-derived threshold
export const vsState = (fpm) => (fpm == null ? 0 : fpm > VS_THRESH_FPM ? 1 : fpm < -VS_THRESH_FPM ? -1 : 0);
// Card row text, e.g. "▲ 1,472 fpm" / "▼ 1,900 fpm" / "120 fpm" (level) / "—"
export const vsText = (fpm) => {
  if (fpm == null) return "—";
  // arrow off vsState (raw fpm), not the rounded display value — so the row never disagrees with the pill/chevron
  const dir = vsState(fpm);
  const arrow = dir > 0 ? "▲ " : dir < 0 ? "▼ " : "";
  return `${arrow}${Math.abs(Math.round(fpm)).toLocaleString()} fpm`;
};

// rssi (dBFS, negative) → 1–4 bar buckets + label. Valid because the live MV is rooftop-only.
export const signalBars = (rssi) => {
  const v = Number(rssi);
  if (rssi == null || !Number.isFinite(v)) return { bars: 0, label: "—" };
  const bars = v >= -20 ? 4 : v >= -24 ? 3 : v >= -28 ? 2 : 1;
  return { bars, label: `${v.toFixed(1)} dBFS` };
};
// Text meter, e.g. signalText(signalBars(-25)) → "▮▮▮▯ −25.0 dBFS" (textContent-safe, no HTML)
export const signalText = (sig) =>
  sig.bars === 0 ? "—" : `${"▮".repeat(sig.bars)}${"▯".repeat(4 - sig.bars)} ${sig.label}`;

// Nav-state: nav_altitude_mcp backbone (selected alt) + nav_modes tags when present. Null when neither.
const NAV_MODE_TAGS = { autopilot: "AP", approach: "APP", lnav: "LNAV", vnav: "VNAV", althold: "ALT", tcas: "TCAS" };
export const navState = (a) => {
  const parts = [];
  const modes = Array.isArray(a?.nav_modes) ? a.nav_modes : [];
  for (const m of modes) if (Object.hasOwn(NAV_MODE_TAGS, m)) parts.push(NAV_MODE_TAGS[m]);
  const mcp = Number(a?.nav_altitude_mcp);
  if (a?.nav_altitude_mcp != null && Number.isFinite(mcp)) {
    const ft = Math.round(mcp);
    parts.push(ft >= 18000 ? `sel FL${Math.round(ft / 100)}` : `sel ${ft.toLocaleString()}`);
  }
  return parts.length ? parts.join(" · ") : null;
};
