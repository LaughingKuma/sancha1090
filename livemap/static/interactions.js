import { S } from "./state.js";
import { cardData, hoverCardHTML } from "./card.js";
import { rebuildSelectedSegments, pruneSelectedPts, pushFix } from "./trails.js";
import { map, overlay } from "./mapsetup.js";

// Spotlight panel (v5.6) — pure reader of S.selected + S.snap.
const spEl = (id) => document.getElementById(id);
const spotlightEl = spEl("spotlight");
export function renderSpotlight() {
  if (!S.selected) {
    spotlightEl.hidden = true;
    return;
  }
  const a = S.snap.aircraft.find((x) => x.hex === S.selected.hex);
  spotlightEl.hidden = false;
  spotlightEl.classList.toggle("lost", !a);
  spEl("sp-lost").hidden = !!a;
  if (!a) {
    // a dropped contact is not an active emergency — clear the alert chrome (data fields stay greyed)
    spotlightEl.classList.remove("emerg");
    spEl("sp-emerg").hidden = true;
    spEl("sp-src").className = ""; // drop the teal MLAT accent so the lost panel greys uniformly
    spEl("sp-vs").className = ""; // and the climb/descent accent — the partial lost-grey wouldn't fully mute it
    return;
  }
  spotlightEl.classList.toggle("mil", a.is_military === true);
  const c = cardData(a);
  spEl("sp-callsign").textContent = c.callsign;
  spEl("sp-badges").innerHTML = c.badges;
  const stEl = spEl("sp-state");
  stEl.hidden = !c.state;
  stEl.textContent = c.state || "";
  const flagEl = spEl("sp-flag");
  flagEl.hidden = !c.flagIso;
  flagEl.className = c.flagIso ? `fi sp-flag fi-${c.flagIso}` : "fi sp-flag";
  spotlightEl.classList.toggle("emerg", !!c.emergency);
  const emEl = spEl("sp-emerg");
  emEl.hidden = !c.emergency;
  if (c.emergency) emEl.textContent = `${c.emergency.code} · ${c.emergency.label}`;
  const srcEl = spEl("sp-src");
  srcEl.textContent = c.source;
  srcEl.className = c.sourceClass;
  spEl("sp-signal").textContent = c.signal;
  spEl("sp-model").textContent = c.model;
  spEl("sp-org").textContent = c.org;
  spEl("sp-route").hidden = !c.route;
  if (c.route) spEl("sp-route").textContent = c.route;
  spEl("sp-alt").textContent = c.alt;
  const vsEl = spEl("sp-vs");
  vsEl.textContent = c.vs;
  vsEl.className = c.vsClass;
  spEl("sp-spd").textContent = c.spd;
  spEl("sp-hdg").textContent = c.hdg;
  spEl("sp-nav").hidden = !c.nav;
  spEl("sp-nav").previousElementSibling.hidden = !c.nav; // hide the <dt>Nav</dt> too when absent
  if (c.nav) spEl("sp-nav").textContent = c.nav;
  spEl("sp-rng").textContent = c.rng;
  spEl("sp-brg").textContent = c.brg;
  const ownerEl = spEl("sp-owner");
  ownerEl.hidden = !c.owner;
  ownerEl.previousElementSibling.hidden = !c.owner; // hide the <dt>Owner</dt> too when absent
  if (c.owner) ownerEl.textContent = c.owner;
  spEl("sp-reg").textContent = c.reg;
  spEl("sp-code").textContent = c.code;
  spEl("sp-hex").textContent = c.hex;
  spEl("sp-origin").textContent = c.origin;
  spEl("sp-recv").textContent = c.recv;
  spEl("sp-contact").textContent = c.contact;
  const pts = S.selected.pts;
  if (pts.length > 1) {
    const mins = Math.max(1, Math.round((pts[pts.length - 1].ts - pts[0].ts) / 60));
    const est = pts.filter((p) => p.est).length;
    spEl("sp-track").textContent =
      `track ${mins} min · ${pts.length} fixes` + (est ? ` · ${est} est` : "");
  } else {
    spEl("sp-track").textContent = "track —";
  }
}
spEl("sp-close").addEventListener("click", clearSelection);
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") clearSelection();
});

function clearSelection() {
  if (!S.selected) return;
  S.selected = null;
  S.trackFetchSeq++; // a deselect must orphan any in-flight /track fetch
  rebuildSelectedSegments();
  renderSpotlight();
}

async function selectAircraft(hex) {
  if (S.selected && S.selected.hex === hex) return;
  S.selected = { hex, pts: [], mil: S.snap.aircraft.some((x) => x.hex === hex && x.is_military === true) };
  rebuildSelectedSegments();
  renderSpotlight();
  const seq = ++S.trackFetchSeq;
  try {
    const j = await (await fetch(`/track/${encodeURIComponent(hex)}`, { cache: "no-store" })).json();
    if (seq !== S.trackFetchSeq) return; // a later click or deselect superseded this fetch
    const pts = [];
    for (const [lon, lat, ts, alt] of j.points || []) pushFix(pts, lon, lat, ts, alt);
    // live fixes may have landed while the fetch was in flight — keep them after the history
    const lastTs = pts.length ? pts[pts.length - 1].ts : -Infinity;
    S.selected.pts = pts.concat(S.selected.pts.filter((p) => p.ts > lastTs));
    if (S.selected.pts.length) pruneSelectedPts();
    rebuildSelectedSegments();
    renderSpotlight();
  } catch (e) {
    /* track fetch is best-effort — selection stays, the wake is unaffected */
  }
}

// deck pick wins over the bare map click — the interleaved overlay fires both for one gesture
map.on("click", (e) => {
  const pick = overlay._deck?.pickObject({ x: e.point.x, y: e.point.y, radius: 4, layerIds: ["planes"] });
  if (pick && pick.object && pick.object.a.hex) selectAircraft(pick.object.a.hex);
  else clearSelection();
});

// Hover card — own DOM node (not deck's built-in tooltip, which anchors top-left at the cursor
// and can't flip/clamp, so it clips off the bottom/right edges). Offset off the cursor + flipped
// and clamped to the viewport so the whole card is always visible.
const hoverEl = document.createElement("div");
hoverEl.id = "hovercard";
hoverEl.hidden = true;
document.body.appendChild(hoverEl);
// coalesce picks to one per frame — mousemove fires far faster than deck can pick
let hoverRaf = 0, lastMove = null, dragging = false;
const hideHover = () => {
  hoverEl.hidden = true;
  if (hoverRaf) { cancelAnimationFrame(hoverRaf); hoverRaf = 0; } // a queued frame must not re-open it with a stale fix
  lastMove = null;
};
function placeHover(cx, cy) {
  const OFF = 14, M = 8;
  const w = hoverEl.offsetWidth, h = hoverEl.offsetHeight; // measured after innerHTML + unhide
  const vw = window.innerWidth, vh = window.innerHeight;
  let left = cx + OFF + w > vw - M ? cx - OFF - w : cx + OFF; // flip left of cursor when it would overrun
  let top = cy + OFF + h > vh - M ? cy - OFF - h : cy + OFF; // flip above the cursor likewise
  hoverEl.style.left = `${Math.min(Math.max(left, M), Math.max(M, vw - M - w))}px`; // final clamp guarantees on-screen
  hoverEl.style.top = `${Math.min(Math.max(top, M), Math.max(M, vh - M - h))}px`;
}
function pickHover() {
  hoverRaf = 0;
  const e = lastMove;
  if (!e) return; // hidden (mouseout/dragstart) between scheduling this frame and its running
  const pick = overlay._deck?.pickObject({ x: e.point.x, y: e.point.y, radius: 4, layerIds: ["planes"] });
  const card = pick && pick.object ? hoverCardHTML(pick.object.a) : null;
  if (!card) return hideHover();
  hoverEl.className = card.emerg ? "ac-tip emerg" : "ac-tip";
  hoverEl.innerHTML = card.html;
  hoverEl.hidden = false;
  placeHover(e.originalEvent.clientX, e.originalEvent.clientY);
}
map.on("mousemove", (e) => { if (dragging) return; lastMove = e; if (!hoverRaf) hoverRaf = requestAnimationFrame(pickHover); });
map.on("mouseout", hideHover);
map.on("dragstart", () => { dragging = true; hideHover(); }); // stay hidden through the whole pan, not just one frame
map.on("dragend", () => { dragging = false; });
// a click hands the plane to the spotlight — drop the transient hover card (no mousemove fires to re-pick + suppress it)
map.on("click", hideHover);
