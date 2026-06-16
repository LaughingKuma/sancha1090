// One home for the telemetry decoders so the card and the map never disagree on a contact's state.

// ICAO emergency squawks — always-on alert set. squawk arrives as a STRING in /aircraft.
const EMERGENCY_SQUAWKS = { "7700": "General", "7600": "Radio Fail", "7500": "Hijack" };

// emergency squawk → { code, label } for a contact, else null
export function emergencyOf(a) {
  const code = a && a.squawk != null ? String(a.squawk).trim() : "";
  // own-property guard: a spoofed squawk like "toString"/"constructor" must not match a prototype member
  const label = Object.hasOwn(EMERGENCY_SQUAWKS, code) ? EMERGENCY_SQUAWKS[code] : null;
  return label ? { code, label } : null;
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
