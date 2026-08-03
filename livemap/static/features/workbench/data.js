// One increment-only sequence per endpoint (the trackFetchSeq pattern) so a stale response never lands;
// "deeplink" is a second instances lane, since a wb_inst resolve runs alongside the view's own list fetch.
const seq = { airlines: 0, services: 0, instances: 0, deeplink: 0, search: 0 };

const qs = (o) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o))
    if (v !== null && v !== undefined && v !== "" && v !== false) p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
};

async function call(kind, path) {
  const mine = ++seq[kind];
  try {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok || mine !== seq[kind]) return null;
    const j = await r.json();
    return mine === seq[kind] ? j : null; // superseded while the body was parsing
  } catch {
    return null; // an unreachable endpoint renders as empty, never as a broken rail
  }
}

const num = (v) => (v == null || v === "" || !Number.isFinite(Number(v)) ? null : Number(v));
const text = (v, max = 64) => (v == null ? "" : String(v).slice(0, max));
const csKeyOf = (c) => String(c || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 8);
// airline_name is a filter VALUE (exact match server-side), not just a label — a display-width cap
// would silently drill into nothing (longest live name is 101 chars); CSS does the visual truncating.
const NAME_MAX = 128;
const rows = (j, key) => (j && Array.isArray(j[key]) ? j[key] : []);
const TIERS = ["settled", "estimated", "provisional", "none"];
// the tier mart is deploy-order optional — a row without it serves "unknown", which is a real badge
const tierOf = (v) => (TIERS.includes(String(v)) ? String(v) : "unknown");
const tierMix = (t) => {
  const o = {};
  for (const k of TIERS) {
    const n = num(t && t[k]);
    if (n) o[k] = n;
  }
  return o;
};
const airport = (e) => ({
  icao: text(e && e.icao, 8),
  iata: text(e && e.iata, 4),
  city: text(e && e.city, 48),
});
const apCode = (e) => e.iata || e.icao || ""; // IATA-or-ICAO, the /flights coalesce
const odChip = (r) => ({ o: text(r.o, 8), d: text(r.d, 8), n: num(r.n) ?? 0 });
const page = (j, list) => ({
  rows: list,
  total: num(j.total) ?? list.length,
  limit: num(j.limit) ?? list.length,
  offset: num(j.offset) ?? 0,
});

function instance(r) {
  const hex = text(r.icao24, 6).toLowerCase();
  const startTs = num(r.start_ts);
  const origin = airport(r.origin);
  const dest = airport(r.dest);
  return {
    hex,
    day: text(r.day, 10),
    startTs,
    endTs: num(r.end_ts),
    callsign: text(r.callsign, 16),
    airline: text(r.airline, NAME_MAX),
    reg: text(r.registration, 12),
    type: text(r.typecode, 8),
    origin,
    dest,
    o: apCode(origin),
    d: apCode(dest),
    tier: tierOf(r.tier),
    gapS: num(r.effective_gap_s),
    nPoints: num(r.n_points),
    mil: r.is_military === true || num(r.is_military) === 1,
    // ephemeral click key only — the path fetch happens now, nothing durable is stored on it
    flightId: r.flight_id == null ? null : text(r.flight_id, 24),
    // callsign segment disambiguates real same-hex-same-second collisions (22 live keys, 21 on
    // distinct routes) — hex+epoch alone aliases two flights there
    key: hex && startTs != null
      ? `${hex}.${Math.round(startTs)}${csKeyOf(r.callsign) ? `.${csKeyOf(r.callsign)}` : ""}`
      : null,
  };
}

export async function fetchAirlines(params) {
  const j = await call("airlines", `/workbench/airlines${qs(params)}`);
  if (!j) return null;
  return page(
    j,
    rows(j, "airlines").map((r) => ({
      name: text(r.name, NAME_MAX),
      nFlights: num(r.n_flights) ?? 0,
      nServices: num(r.n_services) ?? 0,
      firstDay: text(r.first_day, 10),
      lastDay: text(r.last_day, 10),
      tiers: tierMix(r.tiers),
    })),
  );
}

export async function fetchServices(params) {
  const j = await call("services", `/workbench/services${qs(params)}`);
  if (!j) return null;
  return page(
    j,
    rows(j, "services").map((r) => ({
      callsign: text(r.callsign, 16),
      nInstances: num(r.n_instances) ?? 0,
      topOd: (Array.isArray(r.top_od) ? r.top_od : []).map(odChip),
      firstDay: text(r.first_day, 10),
      lastDay: text(r.last_day, 10),
      tiers: tierMix(r.tiers),
    })),
  );
}

export async function fetchInstances(params, lane = "instances") {
  const j = await call(lane, `/workbench/instances${qs(params)}`);
  if (!j) return null;
  return {
    ...page(j, rows(j, "instances").map(instance)),
    od: rows(j, "od_breakdown").map(odChip),
    // absent-not-hidden: only an explicit false (tier mart missing) disables the military control
    milAvailable: j.military_filter_available !== false,
  };
}

export async function fetchSearch(q, limit = 8) {
  const j = await call("search", `/workbench/search${qs({ q, limit })}`);
  if (!j) return null;
  return {
    airlines: rows(j, "airlines").map((r) => ({ name: text(r.name, NAME_MAX), n: num(r.n_flights) ?? 0 })),
    services: rows(j, "services").map((r) => ({
      callsign: text(r.callsign, 16),
      airline: text(r.airline, NAME_MAX),
      n: num(r.n_instances) ?? 0,
    })),
    airframes: rows(j, "airframes").map((r) => ({
      hex: text(r.icao24, 6).toLowerCase(),
      reg: text(r.registration, 12),
      type: text(r.typecode, 8),
      n: num(r.n_instances) ?? 0,
    })),
    airports: rows(j, "airports").map((r) => ({
      icao: text(r.icao, 8),
      iata: text(r.iata, 4),
      name: text(r.name, 64),
      city: text(r.city, 48),
    })),
  };
}
