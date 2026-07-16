import { S } from "./state.js";
import { cardData, hoverCardHTML } from "./card.js";
import { rebuildSelectedSegments, pruneSelectedPts, pushFix, setHistPath, clearHistPath } from "./trails.js";
import { map, overlay } from "./mapsetup.js";

// Spotlight panel (v5.6) — pure reader of S.selected + S.snap.
const spEl = (id) => document.getElementById(id);
const spotlightEl = spEl("spotlight");

// "Where else has it been" — recent flights from CH; rows are DOM nodes with textContent
// (codes/callsigns/airport names are attacker-transmittable, so never innerHTML).
const flightsWrapEl = spEl("sp-flights");
const flightsListEl = spEl("sp-flights-list");
const flightsHdEl = spEl("sp-flights-hd");
const ffCode = (end) => (end && end.code) || "?";
const ffDate = (ts) => {
  if (ts == null) return "";
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
// The drawn sighting's row — tracked so a new pick, a re-render (expand), or a deselect clears the prior
// active chrome. The drawn path itself lives in S (histSegments/histMarkers).
let activeRow = null;
function clearActiveRow() {
  if (activeRow) { activeRow.classList.remove("ff-active"); activeRow.setAttribute("aria-pressed", "false"); }
  activeRow = null;
}
// "No recorded path" is shown IN the route cell (swap-and-restore), never as an extra line — the old
// flex-wrap hint made the row grow taller on empty, which read as jank.
let hintTimer = 0, hintRoute = null, hintOrig = "";
// The route-cell swap is silent to screen readers (and making the cell itself a live region would re-announce
// the route on restore). A dedicated visually-hidden status region announces the hint once instead.
const hintLive = document.createElement("div");
hintLive.setAttribute("role", "status");
hintLive.setAttribute("aria-live", "polite");
hintLive.style.cssText = "position:absolute;width:1px;height:1px;margin:-1px;padding:0;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0;";
document.body.appendChild(hintLive);
function restoreHint() {
  if (hintTimer) { clearTimeout(hintTimer); hintTimer = 0; }
  if (hintRoute) { hintRoute.textContent = hintOrig; hintRoute.classList.remove("ff-nopath"); }
  hintRoute = null; hintOrig = "";
  hintLive.textContent = ""; // clearing is silent; the visible route restore is not a live region
}
function showRouteHint(route, text) {
  restoreHint();
  hintRoute = route; hintOrig = route.textContent;
  route.classList.add("ff-nopath");
  route.textContent = text;
  hintLive.textContent = text; // announce once to assistive tech
  hintTimer = setTimeout(restoreHint, 2000); // transient — the row returns to its route after a beat
}
// A path far from the current view must visibly do something — frame the whole journey unless both ends are
// already on-screen. Endpoints, not a point-count fraction: dense per-second approach fixes cluster at one end
// and would fool a fraction test into thinking a trans-ocean flight is "mostly in view".
function maybeFitHistPath(pts) {
  if (pts.length < 2) return;
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  for (const p of pts) { w = Math.min(w, p.lon); e = Math.max(e, p.lon); s = Math.min(s, p.lat); n = Math.max(n, p.lat); }
  // antimeridian: a naive lon box wider than 180° is the wrong way round the globe — shift western-hemisphere
  // lons +360 so the box wraps the dateline the short way (fitBounds accepts lngs > 180). HNL legs hit this.
  if (e - w > 180) {
    w = Infinity; e = -Infinity;
    for (const p of pts) { const lon = p.lon < 0 ? p.lon + 360 : p.lon; w = Math.min(w, lon); e = Math.max(e, lon); }
  }
  const b = map.getBounds();
  const inView = (p) => p.lon >= b.getWest() && p.lon <= b.getEast() && p.lat >= b.getSouth() && p.lat <= b.getNorth();
  // maplibre honours prefers-reduced-motion, so the flight is instant for those users
  if (!(inView(pts[0]) && inView(pts[pts.length - 1])))
    map.fitBounds([[w, s], [e, n]], { padding: 80, maxZoom: 11, duration: 700 });
}
// Clicking a recent-sightings row draws that historical flight's fused path; clicking it again clears it.
function selectSighting(fid, li, route) {
  const seq = ++S.pathFetchSeq; // any click supersedes an in-flight fetch — including a toggle-off re-click
  restoreHint(); // drop any lingering row hint (no-path / sparse) from a prior pick
  if (S.histFlightId === fid) { clearHistPath(); clearActiveRow(); return; } // re-click the drawn row → toggle off
  clearActiveRow();
  activeRow = li;
  li.classList.add("ff-active");
  li.setAttribute("aria-pressed", "true");
  S.histFlightId = fid; // claim active now so a re-click toggles even before the fetch resolves
  setHistPath([]);      // drop any prior path immediately; the fetch fills it back in
  fetch(`/path/${encodeURIComponent(fid)}`, { cache: "no-store" })
    .then((r) => r.json())
    .then((j) => {
      if (seq !== S.pathFetchSeq) return; // a newer pick or a deselect superseded this fetch
      const n = setHistPath(j.points);
      // a re-render (expand) between click and callback detaches the captured route node — resolve the live one
      const liveRoute = route.isConnected ? route
        : (S.histFlightId === fid && activeRow) ? activeRow.querySelector(".ff-route") : null;
      if (!n) { if (liveRoute) showRouteHint(liveRoute, "no recorded path"); } // drew nothing → say so, honestly
      else {
        maybeFitHistPath(S.histPts); // drew a path → frame it if it's off-screen
        // all-sparse (breadcrumbs, no line): name the dots so they don't read as a mystery
        if (S.histSegments.length === 0 && liveRoute) showRouteHint(liveRoute, `sparse path · ${n} fixes`);
      }
    })
    .catch(() => { /* best-effort: the active row stays, no path drawn */ });
}
// A deselect or a switch to another aircraft drops the drawn history path with the selection.
function resetHistPath() {
  S.pathFetchSeq++; // orphan any in-flight /path fetch
  restoreHint();
  clearHistPath();
  clearActiveRow();
}

const FLIGHTS_COLLAPSED = 5;
function renderFlights(list, expanded = false) {
  flightsListEl.replaceChildren();
  activeRow = null; // the old nodes are detached; re-link below if the drawn row re-renders
  restoreHint(); // a re-render detaches the route node a pending hint would restore — cancel it here
  flightsListEl.classList.toggle("expanded", expanded);
  if (!list || !list.length) { flightsWrapEl.hidden = true; return; }
  // list is this airframe's history (keyed by hex) — label with the reg so rows don't read as the header flight's legs
  const reg = spEl("sp-reg").textContent;
  flightsHdEl.textContent = reg && reg !== "—" ? `Recent flights · ${reg}` : "Recent flights";
  for (const f of expanded ? list : list.slice(0, FLIGHTS_COLLAPSED)) {
    const li = document.createElement("li");
    li.className = f.src === "rooftop" ? "ff-row ff-rooftop" : "ff-row";
    const date = document.createElement("span");
    date.className = "ff-date";
    date.textContent = ffDate(f.ts);
    const call = document.createElement("span"); // per-leg callsign: each row is its own flight number, not the header's
    call.className = "ff-call";
    call.textContent = f.callsign || "";
    const route = document.createElement("span");
    route.className = "ff-route";
    route.textContent = `${ffCode(f.origin)} → ${ffCode(f.dest)}`;
    const oName = f.origin && f.origin.name, dName = f.dest && f.dest.name;
    if (oName || dName) route.title = `${oName || ffCode(f.origin)} → ${dName || ffCode(f.dest)}`;
    li.append(date, call, route);
    if (f.flight_id) {
      // rows click through to the historical fused path; role+tabindex give a native-button affordance
      li.classList.add("ff-clickable");
      li.setAttribute("role", "button");
      li.setAttribute("aria-pressed", "false");
      li.tabIndex = 0;
      const fire = () => selectSighting(f.flight_id, li, route);
      li.addEventListener("click", fire);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fire(); } });
      if (S.histFlightId === f.flight_id) { // an expand re-render must keep the drawn row highlighted
        li.classList.add("ff-active");
        li.setAttribute("aria-pressed", "true");
        activeRow = li;
      }
    }
    flightsListEl.appendChild(li);
  }
  if (!expanded && list.length > FLIGHTS_COLLAPSED) {
    const more = document.createElement("li");
    const btn = document.createElement("button"); // native button = keyboard operability for free
    btn.type = "button";
    btn.className = "ff-more";
    btn.textContent = `+ ${list.length - FLIGHTS_COLLAPSED} more`;
    btn.addEventListener("click", () => renderFlights(list, true));
    more.appendChild(btn);
    flightsListEl.appendChild(more);
  }
  flightsWrapEl.hidden = false;
}
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
  S.flightsFetchSeq++; // orphan any in-flight /flights fetch
  resetHistPath(); // a deselect drops the drawn history path too
  renderFlights(null);
  rebuildSelectedSegments();
  renderSpotlight();
}

async function selectAircraft(hex) {
  if (S.selected && S.selected.hex === hex) return;
  S.selected = { hex, pts: [], mil: S.snap.aircraft.some((x) => x.hex === hex && x.is_military === true) };
  rebuildSelectedSegments();
  renderSpotlight();
  const seq = ++S.trackFetchSeq;
  const fseq = ++S.flightsFetchSeq;
  resetHistPath(); // switching aircraft drops the previous one's drawn history path
  renderFlights(null); // clear any prior selection's list immediately
  fetch(`/flights/${encodeURIComponent(hex)}`, { cache: "no-store" })
    .then((r) => r.json())
    .then((j) => { if (fseq === S.flightsFetchSeq) renderFlights(j.flights || []); })
    .catch(() => { /* history is best-effort — selection + track are unaffected */ });
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
