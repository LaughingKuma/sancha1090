import { S, serverNow } from "./state.js";
import { finiteTs } from "./motion.js";
import { stationVector, routeEnd, classLabel, routeSuffix } from "./geo.js";
import { emergencyOf, sourceLabel, sourceKind, verticalRate, vsState, vsText, signalBars, signalText, navState } from "./telemetry.js";

// ADS-B callsigns/hex are attacker-transmittable and deck.gl renders `html` as innerHTML
const esc = (v) =>
  String(v ?? "—")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

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
      (catLabel ? `<span class="badge">${esc(catLabel)}</span>` : ""), // fixed lookup value, escaped as defense-in-depth
    state: vdir > 0 ? "▲ CLIMB" : vdir < 0 ? "▼ DESC" : null,
    vs: vsText(rate),
    vsClass: vdir > 0 ? "up" : vdir < 0 ? "dn" : "",
    signal: signalText(signalBars(a.rssi)),
    nav: navState(a),
    model: a.year ? `${model} · ${a.year}` : model,
    org: a.airline_name || a.own_op || "Unregistered callsign",
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
