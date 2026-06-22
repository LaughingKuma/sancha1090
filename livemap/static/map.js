import { S, STREAM_FREEZE_S } from "./state.js";
import { zoomMult, sizeFor, _svg, SHAPES } from "./silhouettes.js"; // also injects the <defs> the legend uses
import "./mapsetup.js"; // build map + overlay before the trail/render loops reference them
import { ingestTrails, appendSelectedFix, rebuildTrailSegments, loadHistory } from "./trails.js";
import { renderSpotlight } from "./interactions.js"; // registers the click/keydown/close listeners
import { detectAcquisitions } from "./layers.js"; // starts the iso2/outline loaders + the rAF render loop

// ── Poll the server-side cache (one shared query stream, never one per tab) ──
let pollInFlight = false;
async function poll() {
  if (pollInFlight) return; // never let a slow response race a newer one
  pollInFlight = true;
  try {
    const r = await fetch("/aircraft", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const j = await r.json();
    // coerced: a string ts would turn serverNow() into concatenation and NaN all clock math
    const serverTs = Number(j.server_ts);
    // duplicates too: re-anchoring perf0 on an equal ts steps serverNow() backward (DR stutter)
    if (!Number.isFinite(serverTs) || serverTs <= S.snap.server_ts) {
      // server reachable but feed not advancing — distinct from the fetch-error path below
      if (performance.now() / 1000 - S.snap.perf0 > STREAM_FREEZE_S)
        document.getElementById("meta-line").textContent = "Stream stalled — waiting…";
      return;
    }
    S.snap = { server_ts: serverTs, aircraft: j.aircraft || [], perf0: performance.now() / 1000 };
    // absence from the accepted snapshot is the one authority on "gone" (MV 120 s expiry)
    const live = new Set(S.snap.aircraft.map((a) => a.hex));
    for (const hex of S.trails.keys()) if (!live.has(hex)) S.trails.delete(hex);
    for (const hex of S.renderState.keys()) if (!live.has(hex)) S.renderState.delete(hex);
    ingestTrails();
    if (S.selected) {
      const cur = S.snap.aircraft.find((x) => x.hex === S.selected.hex);
      // v5.6: a vanished hex keeps the selection — the spotlight greys to SIGNAL LOST instead of closing
      if (cur) appendSelectedFix(cur);
    }
    rebuildTrailSegments();
    detectAcquisitions();
    if (!S.historyLoaded) loadHistory();

    const total = S.snap.aircraft.length;
    const milCount = S.snap.aircraft.filter((a) => a.is_military === true).length;
    document.getElementById("stat-total").textContent = total;
    document.getElementById("stat-mil").textContent = milCount;
    renderSpotlight();
    document.getElementById("meta-line").textContent =
      `Synced ${new Date(serverTs * 1000).toTimeString().slice(0, 8)} · ${total} contacts · shared cache`;
  } catch (e) {
    document.getElementById("meta-line").textContent = `Stream error — retrying… (${e.message})`;
  } finally {
    pollInFlight = false;
  }
}
poll();
setInterval(poll, 500);

// ── ?icons — debug strip: every shape at authoring + on-map size, plus fade/mil tints ──
if (new URLSearchParams(location.search).has("icons")) {
  const zm8 = zoomMult(8);
  const onMap = {
    a380: sizeFor({ typecode: "A388", body_class: "quad" }) * zm8,
    b747: sizeFor({ typecode: "B748", body_class: "quad" }) * zm8,
    quad: sizeFor({ body_class: "quad" }) * zm8,
    widebody: sizeFor({ typecode: "B789", body_class: "widebody" }) * zm8,
    airliner: sizeFor({ typecode: "B738", body_class: "narrowbody" }) * zm8,
    regional: sizeFor({ typecode: "DH8D", body_class: "regional" }) * zm8,
    ga: sizeFor({ typecode: "C172", body_class: "ga" }) * zm8,
    heli: sizeFor({ is_helicopter: true }) * zm8,
  };
  const strip = document.createElement("div");
  strip.style.cssText =
    "position:fixed;left:50%;bottom:90px;transform:translateX(-50%);z-index:40;display:flex;gap:18px;" +
    "padding:14px 18px 10px;background:rgba(5,9,14,0.92);border:1px solid rgba(255,176,0,0.25);" +
    "font:10px 'Spline Sans Mono',monospace;color:#7e93a8;text-align:center;";
  const img = (name, px, fill, op = 1) =>
    `<img src="${_svg(SHAPES[name], fill)}" width="${px}" height="${px}" style="opacity:${op}">`;
  for (const [name, size] of Object.entries(onMap)) {
    strip.insertAdjacentHTML(
      "beforeend",
      `<div style="display:flex;flex-direction:column;align-items:center;gap:6px;">` +
        `<div style="display:flex;align-items:flex-end;">${img(name, 64, "#ffb000")}</div>` +
        `<div style="display:flex;align-items:center;gap:6px;">` +
        `${img(name, size, "#ffb000")}${img(name, size, "#ffb000", 0.35)}${img(name, size, "#ff3b30")}</div>` +
        `<span>${name} · ${Math.round(size)}px</span></div>`,
    );
  }
  document.body.appendChild(strip);
}
