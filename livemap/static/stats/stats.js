import { silShape } from "../silhouettes.js?v=6.51";
import {
  UNAVAILABLE, ageSeconds, barGeometry, completeDays, dialPoint, fillDays, fmtAgo, fmtBytes, fmtCount, fmtDay, fmtDec, fmtInt,
  fmtMinuteJst, hourLabel, jstParts, niceScale, parseEnvelope, wedgePath,
} from "./view.js?v=6.51";

const SVG_NS = "http://www.w3.org/2000/svg";
const REFRESH_MS = 5 * 60 * 1000;
const TICK_MS = 30 * 1000;
const LANE_NAMES = { rooftop: "Rooftop antenna", opensky: "OpenSky", "adsb.lol": "adsb.lol", swim: "FAA SWIM" };
const LAYER_NOTES = {
  bronze: "raw rows as they landed", silver: "cleaned and joined", gold: "tables the pages read",
  dim: "reference lists",
};

// Every node is built with createElement and text nodes: airline and city names come from outside data.
function put(node, attrs, kids) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid !== null && kid !== undefined && kid !== false) node.append(kid instanceof Node ? kid : String(kid));
  }
  return node;
}
const el = (tag, attrs, ...kids) => put(document.createElement(tag), attrs, kids);
const sv = (tag, attrs, ...kids) => put(document.createElementNS(SVG_NS, tag), attrs, kids);
const $ = (id) => document.getElementById(id);

// A chart draws at its measured width so its text stays legible on a phone instead of scaling down.
const drawers = new WeakMap();
const resizer = new ResizeObserver((entries) => entries.forEach((e) => drawers.get(e.target)?.()));
function mountChart(host, draw) {
  let lastWidth = -1;
  const run = () => {
    const w = Math.floor(host.clientWidth);
    if (w <= 0 || w === lastWidth) return;
    lastWidth = w;
    host.replaceChildren(draw(w));
  };
  drawers.set(host, run);
  resizer.observe(host);
  return host;
}

function barChart({ values, titles, unit, label, hi = -1, callout, xLabel, axisFmt = fmtInt, height = 190 }) {
  return mountChart(el("div", { class: "chart" }), (width) => {
    const ml = 46, mr = 6, mt = 30, mb = 24;
    const iw = Math.max(10, width - ml - mr), ih = height - mt - mb;
    const scale = niceScale(Math.max(1, ...values), 4);
    const bars = barGeometry(values, { width: iw, height: ih, gap: iw / values.length > 5 ? 1.5 : 0.5, max: scale.max });
    const svg = sv("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label });
    svg.append(sv("text", { x: 0, y: 11, class: "axis-unit" }, unit));
    for (const t of scale.ticks) {
      const y = mt + ih - (t / scale.max) * ih;
      svg.append(sv("line", { x1: ml, x2: width - mr, y1: y, y2: y, class: "gl" }),
        sv("text", { x: ml - 7, y: y + 3.5, "text-anchor": "end" }, axisFmt(t)));
    }
    let labelEnd = -Infinity;
    bars.forEach((b, i) => {
      svg.append(sv("rect", {
        x: (ml + b.x).toFixed(2), y: (mt + b.y).toFixed(2), width: b.w.toFixed(2), height: b.h.toFixed(2),
        class: i === hi ? "bar hi" : "bar",
      }, sv("title", {}, titles[i])));
      const xl = xLabel?.(i);
      if (!xl) return;
      // a label that would run past the right edge on a phone ends at its bar instead
      const textW = 7 * xl.length;
      const end = ml + b.x + textW > width;
      const x0 = end ? ml + b.x + b.w - textW : ml + b.x;
      if (x0 < labelEnd + 8) return;
      labelEnd = x0 + textW;
      svg.append(sv("text", {
        x: (ml + b.x + (end ? b.w : 0)).toFixed(1), y: height - 6, "text-anchor": end ? "end" : "start",
      }, xl));
    });
    if (hi >= 0 && bars[hi] && callout) {
      const cx = Math.min(width - mr - 4, Math.max(ml + 4, ml + bars[hi].x + bars[hi].w / 2));
      const anchor = cx > width * 0.75 ? "end" : cx < width * 0.25 ? "start" : "middle";
      svg.append(sv("text", { x: cx, y: mt + bars[hi].y - 6, "text-anchor": anchor, class: "callout" }, callout));
    }
    return svg;
  });
}

function spark(values) {
  return mountChart(el("div", { class: "chart spark", "aria-hidden": "true" }), (width) => {
    const height = 46;
    const svg = sv("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
    for (const b of barGeometry(values, { width, height, gap: 1 })) {
      svg.append(sv("rect", { x: b.x.toFixed(2), y: b.y.toFixed(2), width: b.w.toFixed(2), height: b.h.toFixed(2), class: "bar" }));
    }
    return svg;
  });
}

function dial(hours, nowMs) {
  const size = 360, c = size / 2, r0 = 60, r1 = 146;
  const vals = Array.from({ length: 24 }, (_, h) => hours.find((x) => x.hour === h)?.avg_aircraft ?? 0);
  const scale = niceScale(Math.max(1, ...vals), 3);
  const radius = (v) => r0 + (v / scale.max) * (r1 - r0);
  const now = jstParts(nowMs / 1000);
  const title = "Average aircraft heard in each hour of the day, Japan time";
  const svg = sv("svg", { class: "dial", viewBox: `0 0 ${size} ${size}`, role: "img", "aria-label": title });
  for (const t of scale.ticks.slice(1)) {
    svg.append(sv("circle", { cx: c, cy: c, r: radius(t), class: "ring" }),
      sv("text", { x: c - 5, y: c - radius(t) + 11, "text-anchor": "end", class: "ring-lbl" }, fmtInt(t)));
  }
  for (let h = 0; h < 24; h += 3) {
    const a = dialPoint(h / 24, r0, c, c), b = dialPoint(h / 24, r1 + 2, c, c), t = dialPoint(h / 24, r1 + 17, c, c);
    svg.append(sv("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: "spoke" }),
      sv("text", { x: t.x, y: t.y + 4, "text-anchor": "middle", class: "hour-lbl" }, String(h).padStart(2, "0")));
  }
  vals.forEach((v, h) => {
    svg.append(sv("path", { d: wedgePath(h, 24, r0 + 2, radius(v), c, c), class: h === now.h ? "wedge now" : "wedge" },
      sv("title", {}, `${hourLabel(h)} to ${hourLabel((h + 1) % 24)} JST: ${fmtDec(v)} aircraft heard on average`)));
  });
  const a = dialPoint((now.h + now.mi / 60) / 24, r0 - 6, c, c), b = dialPoint((now.h + now.mi / 60) / 24, r1 + 6, c, c);
  svg.append(sv("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: "hand" }),
    sv("text", { x: c, y: c + 2, "text-anchor": "middle", class: "center-num" }, fmtDec(vals[now.h])),
    sv("text", { x: c, y: c + 20, "text-anchor": "middle", class: "center-lbl" }, "aircraft heard"),
    sv("text", { x: c, y: c + 34, "text-anchor": "middle", class: "center-lbl" }, `at ${hourLabel(now.h)} JST`));
  // a table ignores width: 1px, so the wrapper is what keeps it out of the phone layout
  const table = el("div", { class: "sr-only" }, el("table", {},
    el("caption", {}, title),
    el("tr", {}, el("th", { scope: "col" }, "Hour (JST)"), el("th", { scope: "col" }, "Aircraft heard")),
    vals.map((v, h) => el("tr", {}, el("td", {}, hourLabel(h)), el("td", {}, fmtDec(v))))));
  return [svg, table];
}

function fact(num, unit, what, when, cls = "") {
  return el("div", { class: `panel fact ${cls}` },
    el("div", { class: "num" }, num, unit ? el("span", { class: "unit" }, unit) : null),
    el("p", { class: "what" }, what),
    when ? el("p", { class: "when" }, when) : null);
}

function record(title, value, unit, who) {
  return el("div", { class: "panel record" }, el("h3", {}, title),
    el("div", { class: "num" }, value, el("span", { class: "unit" }, unit)),
    el("p", { class: "who" }, who));
}

const byType = (tc) => (tc ? ["Flown by type ", el("b", {}, tc), "."]
  : "Type withheld: fewer than three of its airframes were heard.");

function rankList(rows, { icon, main, sub, value, unit, noIcon }) {
  if (!rows.length) return el("p", { class: "note" }, "Nothing to list yet.");
  const top = Math.max(1, ...rows.map(value));
  return el("ol", { class: noIcon ? "rank no-icon" : "rank" }, rows.map((r) => el("li", {},
    noIcon ? null : icon(r),
    el("span", { class: "label" },
      el("span", { class: "main" }, main(r)),
      sub(r) ? el("span", { class: "sub" }, sub(r)) : null,
      el("span", { class: "meter", "aria-hidden": "true" },
        el("i", { style: `width:${((value(r) / top) * 100).toFixed(1)}%` }))),
    el("span", { class: "val" }, fmtInt(value(r)), el("small", {}, unit)))));
}

function typeIcon(t) {
  const shape = silShape(t);
  return sv("svg", { class: shape === "heli" ? "ico heli" : "ico", viewBox: "0 0 64 64", "aria-hidden": "true" },
    sv("use", { href: `#sil-${shape}` }));
}

const typeListOpts = {
  icon: typeIcon, main: (t) => el("b", {}, t.typecode), sub: (t) => t.model,
  value: (t) => t.airframes, unit: "airframes",
};

function renderRooftop(s, nowMs) {
  const rec = s.records || {};
  const per = fillDays(s.per_day, "flights");
  const busiestAt = s.busiest_day ? per.findIndex((d) => d.day === s.busiest_day.day) : -1;
  const since = fmtDay(s.since);
  const hero = el("div", { class: "hero" },
    el("div", { class: "panel dial-panel" }, el("h3", {}, "A typical day, hour by hour"),
      el("p", { class: "note" }, `Average aircraft heard in each Japan-time hour since ${since}. The bright wedge is the hour right now.`),
      dial(s.hour_of_day || [], nowMs)),
    el("div", { class: "facts" },
      fact(fmtInt(s.airframes), null, "different airframes heard", `since ${since}`, "wide"),
      fact(fmtInt(s.military_airframes), null, "of them military", null, "mil"),
      fact(fmtInt(s.peak_minute?.aircraft), null, "aircraft in one minute, the most at once",
        fmtMinuteJst(s.peak_minute?.at)),
      fact(fmtInt(s.busiest_day?.flights), null, "flights on the busiest day",
        fmtDay(s.busiest_day?.day, { weekday: true })),
      fact(fmtInt(s.types_below_floor), null, "rarer types heard but not named",
        "each seen on fewer than three airframes")));
  const records = el("div", { class: "records" },
    record("Highest", fmtInt(rec.highest_ft?.value), "ft", byType(rec.highest_ft?.typecode)),
    record("Fastest", fmtDec(rec.fastest_kt?.value), "kt",
      [byType(rec.fastest_kt?.typecode), " Ground speed, so a tailwind counts."]),
    record("Farthest", fmtInt(rec.farthest_nmi), "nmi",
      `${fmtInt(rec.farthest_km)} km from the antenna. A distance only, never a direction.`));
  const daily = el("div", { class: "panel" }, el("h3", {}, "Rooftop flights per day"),
    el("p", { class: "note" }, "Flights this antenna heard at least once, by Japan-time day. A day settles two days after it ends."),
    per.length ? barChart({
      values: per.map((d) => d.flights),
      titles: per.map((d) => `${fmtDay(d.day, { weekday: true })}: ${d.flights === null ? "no data" : `${fmtInt(d.flights)} flights`}`),
      unit: "flights", hi: busiestAt,
      label: `Flights per day from ${fmtDay(per[0].day)} to ${fmtDay(per[per.length - 1].day)}`,
      callout: busiestAt >= 0 ? `busiest: ${fmtInt(per[busiestAt].flights)}` : null,
      xLabel: (i) => (per[i].day.endsWith("-01") ? fmtDay(per[i].day, { year: false }).split(" ")[1] : null),
    }) : el("p", { class: "note" }, "No settled days yet."));
  const asOf = s.routes_as_of ? ` through ${fmtDay(s.routes_as_of)}` : "";
  const lists = el("div", { class: "lists" },
    el("div", { class: "panel" }, el("h3", {}, "Most common types"),
      el("p", { class: "note" }, "Counted by different airframes, not by flights."),
      rankList(s.types || [], typeListOpts)),
    el("div", { class: "panel" }, el("h3", {}, "Rarest named types"),
      el("p", { class: "note" }, `The least-heard types with at least three airframes. ${fmtInt(s.types_below_floor)} rarer types stay unnamed.`),
      rankList(s.rarest_types || [], typeListOpts)),
    el("div", { class: "panel" }, el("h3", {}, "Airlines"),
      el("p", { class: "note" }, `By flights the antenna heard${asOf}.`),
      rankList(s.airlines || [], {
        noIcon: true, main: (a) => a.name, sub: (a) => `${fmtInt(a.airframes)} airframes`,
        value: (a) => a.flights, unit: "flights",
      })),
    el("div", { class: "panel" }, el("h3", {}, "Routes"),
      el("p", { class: "note" }, `Origin to destination, by flights the antenna heard${asOf}.`),
      rankList(s.routes || [], {
        noIcon: true, main: (r) => el("b", {}, `${r.o}–${r.d}`),
        sub: (r) => (r.o_city && r.d_city ? `${r.o_city} to ${r.d_city}` : null),
        value: (r) => r.flights, unit: "flights",
      })));
  return [hero, records, daily, lists];
}

function renderDatabase(s) {
  const layers = s.layers || [];
  const total = layers.reduce((a, l) => a + (l.bytes || 0), 0) || 1;
  const layerRow = el("div", { class: "layers" }, layers.map((l) => el("div", { class: "panel layer" },
    el("div", { class: "name" }, l.layer[0].toUpperCase() + l.layer.slice(1), el("small", {}, LAYER_NOTES[l.layer] || "")),
    el("div", { class: "big" }, fmtBytes(l.bytes)),
    el("div", { class: "share", role: "img", "aria-label": `${Math.round((l.bytes / total) * 100)}% of the warehouse on disk` },
      el("i", { style: `width:${((l.bytes / total) * 100).toFixed(1)}%` })),
    el("dl", { class: "kv" },
      el("dt", {}, "Rows"), el("dd", {}, fmtCount(l.rows)),
      el("dt", {}, "Uncompressed"), el("dd", {}, fmtBytes(l.raw_bytes)),
      el("dt", {}, "Compression"), el("dd", {}, l.ratio ? `${fmtDec(l.ratio)}×` : "—")))));
  const latest = Object.fromEntries((s.freshness || []).map((f) => [f.lane, f.latest]));
  const lanes = el("div", { class: "lanes" }, (s.per_day || []).map((l) => {
    const days = fillDays(completeDays(l.days, Date.now()), "rows");
    return el("div", { class: "panel lane" },
      el("div", { class: "lane-head" }, el("h3", {}, LANE_NAMES[l.lane] || l.lane),
        el("span", { class: "latest" }, "newest row ", stamp("b", latest[l.lane]))),
      days.length ? spark(days.map((d) => d.rows)) : null,
      el("p", { class: "note" }, days.length ? `Rows per day since ${fmtDay(days[0].day)}` : "No rows yet."),
      el("dl", { class: "kv" },
        el("dt", {}, "Average, last 30 days"), el("dd", {}, fmtInt(l.avg_30d)),
        el("dt", {}, "Busiest of the 30"),
        el("dd", {}, l.peak_30d ? `${fmtInt(l.peak_30d.rows)} (${fmtDay(l.peak_30d.day, { year: false })})` : "—"),
        el("dt", {}, "Disk added a day"), el("dd", {}, fmtBytes(l.bytes_per_day))));
  }));
  const msgs = fillDays(completeDays(s.messages_per_day, Date.now()), "messages");
  const msgPanel = el("div", { class: "panel" }, el("h3", {}, "Radio messages per day"),
    el("p", { class: "note" }, "Mode S and ADS-B messages the antenna decoded, over the last 30 days."),
    msgs.length ? barChart({
      values: msgs.map((m) => m.messages),
      titles: msgs.map((m) => `${fmtDay(m.day, { weekday: true })}: ${m.messages === null ? "no data" : `${fmtInt(m.messages)} messages`}`),
      unit: "messages", axisFmt: (t) => (t >= 1e6 ? `${fmtDec(t / 1e6, t % 1e6 ? 1 : 0)}M` : fmtInt(t)),
      label: `Messages per day from ${fmtDay(msgs[0].day)} to ${fmtDay(msgs[msgs.length - 1].day)}`,
      xLabel: (i) => (i % 7 === 0 ? fmtDay(msgs[i].day, { year: false }) : null), height: 170,
    }) : el("p", { class: "note" }, "No messages counted yet."));
  return [layerRow, lanes, msgPanel];
}

// Ages tick in place, so a tab left open shows how old its numbers are without refetching.
function stamp(tag, unixSeconds, prefix = "") {
  return el(tag, { "data-stamp": unixSeconds ?? "", "data-prefix": prefix }, prefix + fmtAgo(ageSeconds(unixSeconds, Date.now())));
}
function tick() {
  for (const node of document.querySelectorAll("[data-stamp]")) {
    const v = node.getAttribute("data-stamp");
    node.textContent = node.getAttribute("data-prefix") + fmtAgo(ageSeconds(v === "" ? null : Number(v), Date.now()));
  }
}

const UNAVAILABLE_COPY = {
  rooftop: ["Rooftop numbers are withheld right now",
    "They appear only when the warehouse can confirm which aircraft must stay private. The page asks again every few minutes."],
  database: ["Warehouse numbers are unavailable right now",
    "The warehouse did not answer. The page asks again every few minutes."],
};

function showUnavailable(name) {
  const [title, text] = UNAVAILABLE_COPY[name];
  $(`${name}-asof`).className = "asof";
  $(`${name}-asof`).replaceChildren();
  $(`${name}-body`).replaceChildren(el("div", { class: "panel unavailable", role: "status" }, el("h3", {}, title), el("p", {}, text)));
}

// Built off-screen and swapped in last, so a throw halfway through leaves this section on its unavailable card
// and never reaches the other section.
function renderSection(name, section, nowMs) {
  if (section.status === "ok") {
    try {
      const kids = name === "rooftop" ? renderRooftop(section, nowMs) : renderDatabase(section);
      const asof = [el("span", { class: "dot", "aria-hidden": "true" }),
        stamp("span", section.as_of, section.stale ? "Last good copy, from " : "Updated ")];
      $(`${name}-asof`).className = section.stale ? "asof stale" : "asof";
      $(`${name}-asof`).replaceChildren(...asof);
      $(`${name}-body`).replaceChildren(...kids);
      return;
    } catch (err) {
      console.error(`stats: ${name} section failed to render`, err);
    }
  }
  showUnavailable(name);
}

let lastLoad = 0;
let shown = false;
let loadSeq = 0;
async function load() {
  lastLoad = Date.now();
  const seq = ++loadSeq;
  let env = null;
  try {
    const r = await fetch("/stats-data");
    if (r.ok) env = parseEnvelope(await r.json());
  } catch {
    env = null;
  }
  // A slow older response must not overwrite a newer one already on screen.
  if (seq !== loadSeq) return;
  // A failed refresh keeps what is on screen, whose ticking age already says how old it is.
  if (!env && shown) return;
  env ||= { rooftop: UNAVAILABLE, database: UNAVAILABLE };
  resizer.disconnect();
  renderSection("rooftop", env.rooftop, Date.now());
  renderSection("database", env.database, Date.now());
  shown = true;
}

load();
setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
setInterval(tick, TICK_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Date.now() - lastLoad > REFRESH_MS) load();
});
