// North-pointing top-down silhouettes (authored 64×64, nose up), baked as SVG data-URIs. mask:true means
// deck ignores the fill color and tints by getColor, so age-fade + mil-red apply to any shape.
// All artwork is original, sized from published planform ratios — never traced from tar1090/FA (GPL).
// Raster bakes at 128 so ~60px heavies stay crisp on DPR-2; viewBox keeps the 64-unit artwork space.
export const _svg = (inner, fill = "#fff") =>
  "data:image/svg+xml;charset=utf-8," +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 64 64" fill="${fill}">${inner}</svg>`,
  );
const _icon = (inner) => ({ url: _svg(inner), width: 128, height: 128, anchorX: 64, anchorY: 64, mask: true });

const _path = (pts) => `M${pts.replaceAll(" ", " L")} Z`;
// Multi-tone inside one tint: airframe 0.82, cockpit notch cut to 0.3, engine details at 1.0 —
// the mask's alpha carries the tones, so the single getColor tint still drives everything.
const _jet = (body, notch, detail = "") =>
  `<path fill-rule="evenodd" opacity="0.82" d="${_path(body)} ${_path(notch)}"/>` +
  `<polygon opacity="0.3" points="${notch}"/>` +
  detail;

const BODY_NARROW =
  "32,3 35,10 34,24 60,39 60,43 34,31 33,48 39,52 39,55 32,52 25,55 25,52 31,48 30,31 4,43 4,39 30,24 29,10";
const NOTCH_NARROW = "32,5 34.1,9.4 29.9,9.4";
// wider fuselage + broader span than the narrowbody — reads as a heavy twin (777/787/A350)
const BODY_WIDE =
  "32,3 37,12 36,23 61,38 61,43 36,31 35,48 42,53 42,56 32,53 22,56 22,53 29,48 28,31 3,43 3,38 28,23 27,12";
const NOTCH_WIDE = "32,5.5 34.4,10.8 29.6,10.8";
// nacelles poke forward of the leading edge — the tar1090 small-size legibility trick
const NAC_NARROW = '<ellipse cx="25.5" cy="29" rx="2" ry="3.4"/><ellipse cx="38.5" cy="29" rx="2" ry="3.4"/>';
const NAC_WIDE = '<ellipse cx="24.5" cy="28.5" rx="2.5" ry="4"/><ellipse cx="39.5" cy="28.5" rx="2.5" ry="4"/>';
const NAC_QUAD =
  '<ellipse cx="27" cy="29" rx="2.5" ry="3.8"/><ellipse cx="37" cy="29" rx="2.5" ry="3.8"/>' +
  '<ellipse cx="20" cy="32" rx="2.5" ry="3.8"/><ellipse cx="44" cy="32" rx="2.5" ry="3.8"/>';
// near-square planform (span/length ≈ 1.10): blunt wide fuselage, deep-chord wing, 4 big nacelles
const BODY_A380 =
  "32,4 34.3,6.8 36,11 36,21 62,37 62,42 35.5,33 34.5,49 44,55 44,58 32,54.5 20,58 20,55 29.5,49 28.5,33 2,42 2,37 28,21 28,11 29.7,6.8";
const NOTCH_A380 = "32,6.5 34.5,11 29.5,11";
const NAC_A380 =
  '<ellipse cx="41" cy="25.5" rx="2.8" ry="4.4"/><ellipse cx="50" cy="31" rx="2.6" ry="4.1"/>' +
  '<ellipse cx="23" cy="25.5" rx="2.8" ry="4.4"/><ellipse cx="14" cy="31" rx="2.6" ry="4.1"/>';
// longer than wide (span/length ≈ 0.90): hump as a wider forward-fuselage shoulder, sharper sweep
const BODY_B747 =
  "32,2 35,7 35.5,16 34.5,19 34,26 58,43 58,46.5 34,35 33.5,51 41,56.5 41,59.5 32,56 23,59.5 23,56.5 30.5,51 30,35 6,46.5 6,43 30,26 29.5,19 28.5,16 29,7";
const NOTCH_B747 = "32,4 34,7.4 30,7.4";
// the upper deck glows at full alpha — the hump reads even when the shoulder geometry blurs
const HUMP_B747 = '<rect x="30" y="8.5" width="4" height="8.5" rx="2"/>';
const NAC_B747 =
  '<ellipse cx="39.5" cy="30" rx="2.5" ry="4.2"/><ellipse cx="46.5" cy="35.5" rx="2.4" ry="3.9"/>' +
  '<ellipse cx="24.5" cy="30" rx="2.5" ry="4.2"/><ellipse cx="17.5" cy="35.5" rx="2.4" ry="3.9"/>';

export const SHAPES = {
  airliner: _jet(BODY_NARROW, NOTCH_NARROW, NAC_NARROW),
  widebody: _jet(BODY_WIDE, NOTCH_WIDE, NAC_WIDE),
  // four nacelles on the generic airframe — fallback for 4-engine types without their own shape
  quad: _jet(BODY_NARROW, NOTCH_NARROW, NAC_QUAD),
  a380: _jet(BODY_A380, NOTCH_A380, NAC_A380),
  b747: _jet(BODY_B747, NOTCH_B747, HUMP_B747 + NAC_B747),
  // straighter wings + two prop discs — turboprop regional
  regional:
    '<polygon points="32,7 34,13 33,24 55,30 55,33 33,28 32,48 37,52 37,54 32,51 27,54 27,52 31,48 31,28 9,33 9,30 31,24 30,13"/>' +
    '<circle cx="16" cy="26" r="4" opacity="0.8"/><circle cx="48" cy="26" r="4" opacity="0.8"/>',
  // light GA: nose prop disc + slim fuselage + STRAIGHT (unswept) high wing — reads as a Cessna, not a jet
  ga:
    '<ellipse cx="32" cy="11" rx="9" ry="1.8" opacity="0.7"/>' +
    '<rect x="30.4" y="10" width="3.2" height="42" rx="1.6"/>' +
    '<rect x="7" y="25" width="50" height="3.6" rx="1.8"/>' +
    '<rect x="22" y="46" width="20" height="3" rx="1.5"/>',
  // rotor disc + crossed blades + tail boom — helicopter
  heli:
    '<circle cx="32" cy="27" r="12" opacity="0.22"/>' +
    '<rect x="30.5" y="27" width="3" height="28" rx="1.5"/><rect x="28" y="51" width="8" height="3" rx="1.5"/>' +
    '<ellipse cx="32" cy="27" rx="5.5" ry="7.5"/>' +
    '<rect x="9" y="25.5" width="46" height="3" rx="1.5" transform="rotate(35 32 27)"/>' +
    '<rect x="9" y="25.5" width="46" height="3" rx="1.5" transform="rotate(-35 32 27)"/>',
};
export const SIL = Object.fromEntries(Object.entries(SHAPES).map(([k, v]) => [k, _icon(v)]));
document.body.insertAdjacentHTML("beforeend", `<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>${Object.entries(SHAPES).map(([k, s]) => `<g id="sil-${k}" fill="currentColor">${s}</g>`).join("")}</defs></svg>`);

// climb/descend cues — plain triangles, billboarded (never rotated with track)
export const CHEV_UP = _icon('<polygon points="32,14 52,50 12,50"/>');
export const CHEV_DOWN = _icon('<polygon points="32,50 52,14 12,14"/>');

// body_class → shape. Unknown class → generic airliner.
const CLASS_SHAPE = {
  quad: "quad", widebody: "widebody", narrowbody: "airliner",
  regional: "regional", ga: "ga", heli: "heli", airliner: "airliner",
};
// Wingspan-true sizing (v5.6): px = 18 + (span−10)·0.43, clamp 18–48, ×0.93 twin-widebody,
// × zoom multiplier. Spans are published figures; class fallbacks cover unknown typecodes.
const SPAN_M = {
  A388: 79.8, B748: 68.4, B744: 64.4, B74F: 64.4, BLCF: 64.4,
  B77W: 64.8, B77L: 64.8, B772: 60.9, B789: 60.1, B788: 60.1, B78X: 60.1,
  A359: 64.8, A35K: 64.8, A332: 60.3, A333: 60.3, B763: 47.6, B764: 51.9,
  B738: 35.8, B737: 35.8, B739: 35.8, A320: 35.8, A321: 35.8, A20N: 35.8, A21N: 35.8, A319: 35.8,
  E190: 28.7, E170: 26.0, DH8D: 28.4, AT76: 27.1, AT75: 27.1,
  C172: 11.0, DA40: 11.9, PC12: 16.3,
};
const SPAN_CLASS = { quad: 65, widebody: 60, narrowbody: 35, regional: 27, ga: 11, heli: 14, airliner: 35 };
// Exact-typecode shapes win; body_class stays the fallback; generic airliner last.
const TYPE_SHAPE = { A388: "a380", B748: "b747", B744: "b747", B74F: "b747", BLCF: "b747" };
export function silShape(a) {
  if (a.is_helicopter === true) return "heli";
  const t = TYPE_SHAPE[a.typecode];
  if (t) return t;
  return CLASS_SHAPE[a.body_class] ?? "airliner";
}
export function sizeFor(a) {
  const cls = a.is_helicopter === true ? "heli" : (CLASS_SHAPE[a.body_class] ? a.body_class : "airliner");
  const span = SPAN_M[a.typecode] ?? SPAN_CLASS[cls];
  let px = Math.min(48, Math.max(18, 18 + (span - 10) * 0.43));
  if (cls === "widebody") px *= 0.93; // pure span flatters twins — restore the jumbo tier
  return px;
}
export const zoomMult = (z) => Math.min(1.25, Math.max(0.85, 0.85 + (z - 7) * 0.1));
