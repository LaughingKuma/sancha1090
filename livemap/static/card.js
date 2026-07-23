import { S, serverNow } from "./state.js?v=6.34";
import { finiteTs } from "./motion.js?v=6.34";
import { stationVector, routeEnd, classLabel, routeSuffix } from "./geo.js?v=6.34";
import { emergencyOf, sourceLabel, sourceKind, verticalRate, vsState, vsText, signalBars, signalText, navState } from "./telemetry.js?v=6.34";

// ADS-B callsigns/hex are attacker-transmittable and deck.gl renders `html` as innerHTML
const esc = (v) =>
  String(v ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

// Registry legal-form + filler words dropped before comparing owner to operator, so
// "United Airlines Inc" reads as an echo of "United Airlines", not a distinct owner.
const OWNER_STOP = new Set(["inc","incorporated","corp","corporation","co","company","llc","ltd",
  "limited","plc","gmbh","ag","sa","kk","pty","bv","nv","na","llp","lp","the","of"]);
const ownerTokens = (s) =>
  new Set((s || "").toLowerCase().replace(/\(.*?\)/g, " ").replace(/[^a-z0-9 ]/g, " ")
    .split(/\s+/).filter((t) => t && !OWNER_STOP.has(t)));
// echo = one name's meaningful tokens are a subset of the other's (same entity + extra legal words)
const isOwnerEcho = (own, op) => {
  const a = ownerTokens(own), b = ownerTokens(op);
  if (!a.size || !b.size) return true;
  const [small, big] = a.size <= b.size ? [a, b] : [b, a];
  for (const t of small) if (!big.has(t)) return false;
  return true;
};
// English filler words are never acronyms — keep them lowercase even though they're short + ALLCAPS
const OWNER_LOWER = new Set(["of", "the", "as", "for", "and"]);
// Aviation lessor/bank acronyms the registry stores as words ("Smbc", "Gecas") — force uppercase;
// length alone can't tell these from real 4+ char words like BANK/UTAH, so an explicit allowlist.
const OWNER_ACRONYM = new Set(["smbc", "gecas", "awas", "bbam", "orix", "icbc", "sasof"]);
// live own_op is ALLCAPS + admin noise; strip parentheticals + "DEPT NNN …", title-case,
// keep short acronyms (UMB, US, NA, DHL) uppercase
const cleanOwner = (s) =>
  s.replace(/\(.*?\)/g, " ").replace(/,?\s*dept\s+\d+.*$/i, "").replace(/\s+/g, " ").trim()
    .split(" ")
    .map((w) => {
      const lw = w.toLowerCase();
      return OWNER_LOWER.has(lw) ? lw                                 // filler → always lowercase
        : OWNER_ACRONYM.has(lw) ? w.toUpperCase()                     // known lessor acronym → uppercase
        : w.length <= 3 && w === w.toUpperCase() ? w                  // short ALLCAPS → acronym, keep
        : w.charAt(0).toUpperCase() + w.slice(1).toLowerCase();       // else title-case
    })
    .join(" ");
const ownerDistinct = (a) => {
  const own = (a.own_op || "").trim();
  const op = (a.airline_name || "").trim();
  if (!own || !op || isOwnerEcho(own, op)) return null; // absent airline OR echo → org already carries it
  return cleanOwner(own);
};

// shared with the lost-panel badge sync (interactions.js) — the chip stays one string in one place
export const PROV_BADGE = '<span class="badge badge-prov" title="full-res path settles in ~1–3 days">PROVISIONAL</span>';

// one builder feeds both the hover card and the spotlight so the two can never drift apart
export function cardData(a) {
  const rate = verticalRate(a);
  const vdir = vsState(rate);
  const fixTs = finiteTs(a.pos_ts, a.capture_ts);
  const fage = fixTs == null ? NaN : serverNow() - fixTs;
  const sv = stationVector(a.lon, a.lat);
  const model = a.aircraft_desc || a.typecode || "—";
  const catLabel = classLabel(a);
  return {
    callsign: a.flight || a.hex || "UNKNOWN",
    badges:
      (a.is_military === true ? '<span class="badge">MIL</span>' : "") +
      (a.is_helicopter ? '<span class="badge">HELI</span>' : "") +
      (catLabel ? `<span class="badge">${esc(catLabel)}</span>` : "") +
      // scoped to the selected aircraft's drawn path — hover cards for other aircraft can never inherit it
      (S.selected && a.hex === S.selected.hex && S.histProvisional ? PROV_BADGE : ""),
    state: vdir > 0 ? "▲ CLIMB" : vdir < 0 ? "▼ DESC" : null,
    vs: vsText(rate),
    vsClass: vdir > 0 ? "up" : vdir < 0 ? "dn" : "",
    signal: signalText(signalBars(a.rssi)),
    nav: navState(a),
    model: a.year ? `${model} · ${a.year}` : model,
    org: a.airline_name || a.own_op || "Unregistered callsign",
    owner: ownerDistinct(a),
    // Backstory ring (v5.1): latest known route for this callsign from the flights catalog.
    route: a.route ? `${routeEnd(a.route.origin_city, a.route.origin)} → ${routeEnd(a.route.dest_city, a.route.dest)}${routeSuffix(a.route)}` : null,
    alt: a.alt_baro == null ? "—" : a.alt_baro === "ground" ? "GROUND" : `${a.alt_baro} ft`,
    spd: a.gs == null ? "—" : `${Math.round(a.gs)} kt`,
    hdg: a.track == null ? "—" : `${Math.round(a.track)}°`,
    rng: sv ? `${sv.nm.toFixed(1)} nm` : "—",
    brg: sv ? `${Math.round(sv.brg)}°` : "—",
    reg: a.registration || "—",
    code: a.typecode || "—",
    hex: (a.hex || "—").toUpperCase(),
    origin: a.reg_country || "—",
    flagIso: a.reg_country && Object.hasOwn(S.countryIso2, a.reg_country) ? S.countryIso2[a.reg_country] : null,
    recv: a.recv || "—",
    contact: !Number.isFinite(fage) ? "—" : fage < 5 ? "live" : `last fix ${Math.round(fage)} s ago`,
    emergency: emergencyOf(a), // { code, label } | null — controlled-lookup value
    source: sourceLabel(a.position_source), // 'MLAT' | 'ADS-B' | '—'
    sourceClass: sourceKind(a.position_source) === "mlat" ? "src-mlat" : "", // teal only for MLAT (the rarer one)
  };
}

// Hover-card inner HTML for an aircraft; null when it shouldn't show. Positioned by a custom
// onHover handler (interactions.js) because deck's built-in tooltip can't flip/clamp at viewport edges.
export function hoverCardHTML(a) {
  if (!a) return null;
  // the spotlight panel already shows the focused aircraft — a hover card would be a duplicate
  if (S.selected && a.hex === S.selected.hex) return null;
  const c = cardData(a);
  const html =
    (c.emergency ? `<div class="tip-emerg">${esc(c.emergency.code)} · ${esc(c.emergency.label)}</div>` : "") +
    `<div class="flight ${a.is_military === true ? "mil" : ""}${c.emergency ? " emerg" : ""}">${c.flagIso ? `<span class="fi fi-${c.flagIso}"></span> ` : ""}${esc(c.callsign)}<span class="hdr-chips">${c.badges}${c.state ? `<span class="tip-state">${c.state}</span>` : ""}</span></div>` +
    `<div class="model">${esc(c.model)}</div>` +
    `<div class="org">${esc(c.org)}</div>` +
    (c.route ? `<div class="route">${esc(c.route)}</div>` : "") +
    "<dl>" +
    `<dt>Alt</dt><dd>${esc(c.alt)}</dd>` +
    `<dt>V/S</dt><dd class="${c.vsClass}">${esc(c.vs)}</dd>` +
    `<dt>Speed</dt><dd>${esc(c.spd)}</dd>` +
    `<dt>Heading</dt><dd>${esc(c.hdg)}</dd>` +
    (c.nav ? `<dt>Nav</dt><dd>${esc(c.nav)}</dd>` : "") +
    `<dt>Range</dt><dd>${esc(c.rng)}</dd>` +
    `<dt>Bearing</dt><dd>${esc(c.brg)}</dd>` +
    `<dt>Reg</dt><dd>${esc(c.reg)}</dd>` +
    `<dt>Code</dt><dd>${esc(c.code)}</dd>` +
    `<dt>ICAO</dt><dd>${esc(c.hex)}</dd>` +
    `<dt>Origin</dt><dd>${esc(c.origin)}</dd>` +
    `<dt>Recv</dt><dd>${esc(c.recv)}</dd>` +
    `<dt>Source</dt><dd class="${c.sourceClass}">${esc(c.source)}</dd>` +
    `<dt>Signal</dt><dd>${esc(c.signal)}</dd>` +
    `<dt>Contact</dt><dd>${esc(c.contact)}</dd>` +
    "</dl>";
  return { html, emerg: !!c.emergency };
}
