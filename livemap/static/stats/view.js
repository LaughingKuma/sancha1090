// Pure helpers for the stats page: no DOM, so node:test imports this file as-is.
export const CONTRACT = 1;
const JST_MS = 9 * 3600 * 1000;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// Every key the page knows; anything else (a hex, a callsign) rejects the section instead of rendering it.
const TYPE = { typecode: 1, model: 1, body_class: 1, is_helicopter: 1, airframes: 1 };
const RECORD = { value: 1, typecode: 1 };
const DAY_ROWS = { day: 1, rows: 1 };
const SECTION = { status: 1, as_of: 1, stale: 1 };
// A plain-object slot may be null only where the server really sends null; a bare `null` elsewhere fails the section.
class Nullable { constructor(schema) { this.schema = schema; } }
const opt = (schema) => new Nullable(schema);
const SCHEMA = {
  rooftop: {
    ...SECTION, since: 1, airframes: 1, types: [TYPE], rarest_types: [TYPE], types_below_floor: 1,
    airlines: [{ name: 1, flights: 1, airframes: 1 }],
    routes: [{ o: 1, o_city: 1, d: 1, d_city: 1, flights: 1 }], routes_as_of: 1,
    hour_of_day: [{ hour: 1, avg_aircraft: 1 }], per_day: [{ day: 1, flights: 1 }],
    busiest_day: opt({ day: 1, flights: 1 }), peak_minute: opt({ at: 1, aircraft: 1 }),
    records: { highest_ft: RECORD, fastest_kt: RECORD, farthest_nmi: 1, farthest_km: 1 },
    military_airframes: 1,
  },
  database: {
    ...SECTION,
    layers: [{ layer: 1, rows: 1, bytes: 1, raw_bytes: 1, ratio: 1 }],
    freshness: [{ lane: 1, latest: 1 }],
    per_day: [{ lane: 1, days: [DAY_ROWS], avg_30d: 1, peak_30d: opt(DAY_ROWS), bytes_per_day: 1 }],
    messages_per_day: [{ day: 1, messages: 1 }],
  },
};

export function fits(value, schema) {
  if (schema instanceof Nullable) return value === null || fits(value, schema.schema);
  // a leaf holds a scalar or null; an object there could smuggle an unlisted key past the check
  if (schema === 1) return value === null || value === undefined || typeof value !== "object";
  if (Array.isArray(schema)) return Array.isArray(value) && value.every((v) => fits(v, schema[0]));
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  return Object.keys(value).every((k) => Object.hasOwn(schema, k) && fits(value[k], schema[k]));
}

export const UNAVAILABLE = Object.freeze({ status: "unavailable" });

// Each section stands or falls alone, so a bad rooftop never blanks the database numbers.
export function parseEnvelope(body) {
  const ok = body && typeof body === "object" && body.contract === CONTRACT;
  const pick = (name) => {
    const s = ok ? body[name] : null;
    return s && s.status === "ok" && fits(s, SCHEMA[name]) ? s : UNAVAILABLE;
  };
  return { rooftop: pick("rooftop"), database: pick("database") };
}

const n = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

export function fmtInt(v) {
  const x = n(v);
  return x === null ? "—" : Math.round(x).toLocaleString("en-US");
}

export function fmtDec(v, digits = 1) {
  const x = n(v);
  return x === null ? "—" : x.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

export function fmtCount(v) {
  const x = n(v);
  if (x === null) return "—";
  const steps = [[1e9, "billion"], [1e6, "million"], [1e3, "thousand"]];
  for (const [base, word] of steps) {
    if (Math.abs(x) >= base) return `${sig3(x / base)} ${word}`;
  }
  return fmtInt(x);
}

// Decimal units, as disk vendors and ClickHouse's formatReadableSize(…, decimal) print them.
export function fmtBytes(v) {
  const x = n(v);
  if (x === null) return "—";
  const units = ["B", "kB", "MB", "GB", "TB"];
  let i = 0;
  let y = x;
  while (Math.abs(y) >= 1000 && i < units.length - 1) { y /= 1000; i += 1; }
  return i === 0 ? `${Math.round(y)} B` : `${sig3(y)} ${units[i]}`;
}

function sig3(x) {
  const digits = Math.abs(x) >= 100 ? 0 : Math.abs(x) >= 10 ? 1 : 2;
  return x.toFixed(digits);
}

export function fmtAgo(seconds) {
  const s = n(seconds);
  if (s === null) return "—";
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 48) return m % 60 && h < 6 ? `${h} h ${m % 60} min ago` : `${h} h ago`;
  return `${Math.floor(h / 24)} days ago`;
}

// A "YYYY-MM-DD" is already a JST calendar day from the server; read it as a plain date, never shifted.
export function fmtDay(day, { weekday = false, year = true } = {}) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day ?? ""));
  if (!m) return "—";
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
  const core = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}${year ? ` ${d.getUTCFullYear()}` : ""}`;
  return weekday ? `${WEEKDAYS[d.getUTCDay()]} ${core}` : core;
}

// JST has no daylight saving, so a fixed +9 h offset is exact and needs no Intl time-zone data.
export function jstParts(unixSeconds) {
  const d = new Date(unixSeconds * 1000 + JST_MS);
  return { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes() };
}

export function fmtMinuteJst(unixSeconds) {
  if (n(unixSeconds) === null) return "—";
  const p = jstParts(unixSeconds);
  const pad = (x) => String(x).padStart(2, "0");
  return `${p.d} ${MONTHS[p.mo]} ${p.y}, ${pad(p.h)}:${pad(p.mi)} JST`;
}

export const hourLabel = (h) => `${String(h).padStart(2, "0")}:00`;

export function niceScale(max, count = 4) {
  const top = n(max) !== null && max > 0 ? max : 1;
  const raw = top / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((f) => f * mag).find((s) => s >= raw);
  const ticks = [];
  for (let t = 0; t <= top + step * 1e-9 || ticks.length <= 1; t += step) ticks.push(+t.toFixed(10));
  if (ticks[ticks.length - 1] < top) ticks.push(+(ticks[ticks.length - 1] + step).toFixed(10));
  return { max: ticks[ticks.length - 1], ticks };
}

export function barGeometry(values, { width, height, gap = 1, max } = {}) {
  const count = values.length;
  if (!count || !(width > 0) || !(height > 0)) return [];
  const top = max ?? Math.max(1, ...values.map((v) => n(v) ?? 0));
  const slot = width / count;
  const w = Math.max(0.5, slot - Math.min(gap, slot / 2));
  return values.map((v, i) => {
    const h = Math.max(0, Math.min(1, (n(v) ?? 0) / top)) * height;
    return { x: i * slot, y: height - h, w, h };
  });
}

export function wedgePath(i, count, r0, r1, cx, cy, padDeg = 1.2) {
  const span = 360 / count;
  const a0 = ((i * span + padDeg / 2 - 90) * Math.PI) / 180;
  const a1 = (((i + 1) * span - padDeg / 2 - 90) * Math.PI) / 180;
  const pt = (r, a) => `${(cx + r * Math.cos(a)).toFixed(2)} ${(cy + r * Math.sin(a)).toFixed(2)}`;
  return `M${pt(r0, a0)} L${pt(r1, a0)} A${r1} ${r1} 0 0 1 ${pt(r1, a1)} L${pt(r0, a1)} A${r0} ${r0} 0 0 0 ${pt(r0, a0)} Z`;
}

export function dialPoint(fraction, r, cx, cy) {
  const a = (fraction * 360 - 90) * (Math.PI / 180);
  return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
}

// Warehouse days are UTC capture days; today's is still filling and would read as a sudden drop.
export function completeDays(rows, nowMs) {
  const today = new Date(nowMs).toISOString().slice(0, 10);
  return (rows || []).filter((r) => typeof r.day === "string" && r.day < today);
}

// A day the antenna was off has no row at all; it needs an empty slot or the time axis silently shrinks.
export function fillDays(rows, valueKey) {
  const out = [];
  const list = (rows || []).filter((r) => /^\d{4}-\d{2}-\d{2}$/.test(r.day));
  if (!list.length) return out;
  const have = new Map(list.map((r) => [r.day, r]));
  const end = Date.parse(`${list[list.length - 1].day}T00:00:00Z`);
  for (let t = Date.parse(`${list[0].day}T00:00:00Z`); t <= end; t += 86400000) {
    const day = new Date(t).toISOString().slice(0, 10);
    out.push(have.get(day) ?? { day, [valueKey]: null });
  }
  return out;
}

// Ages come from the viewer's clock against the server's stamp; a skewed clock never shows a negative age.
export const ageSeconds = (stamp, nowMs) => (n(stamp) === null ? null : Math.max(0, nowMs / 1000 - stamp));
