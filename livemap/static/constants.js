// 120 s is the MV's data contract (tar1090 position-retention parity) — fade-by-age
// visually recovers freshness within it.
export const WINDOW_S = 120;
export const RING_NM = [25, 50, 100];
// Runway centerlines as geographic segments — published runway-end coords, verified against the
// basemap at z13; visual furniture, not navigation data; codes are the most-recognizable per
// field (IATA for the civils, ICAO for the bases).
export const AIRPORTS = [
  { code: "HND", label: [139.784, 35.544], runways: [
    { name: "16R/34L", path: [[139.7688, 35.5603], [139.7856, 35.5366]] },
    { name: "04/22", path: [[139.7613, 35.5490], [139.7771, 35.5674]] },
    { name: "16L/34R", path: [[139.7866, 35.5659], [139.8051, 35.5397]] },
    { name: "05/23", path: [[139.8035, 35.5240], [139.8221, 35.5406]] },
  ]},
  { code: "NRT", label: [140.392, 35.772], runways: [
    { name: "16R/34L", path: [[140.3683, 35.7744], [140.3907, 35.7433]] },
    { name: "16L/34R", path: [[140.3781, 35.8052], [140.3922, 35.7858]] },
  ]},
  { code: "RJTY", label: [139.3545, 35.7485], runways: [
    { name: "18/36", path: [[139.3454, 35.7634], [139.3516, 35.7336]] },
  ]},
  { code: "RJTA", label: [139.4540, 35.4546], runways: [
    { name: "01/19", path: [[139.4503, 35.4436], [139.4499, 35.4656]] },
  ]},
];
export const RUNWAY_PATHS = AIRPORTS.flatMap((ap) => ap.runways.map((r) => ({ path: r.path })));
// Threshold designators (published): name order follows path point order — first designator is the
// heading you fly FROM the first endpoint, so each number lands at its painted threshold.
export const RUNWAY_ENDS = AIRPORTS.flatMap((ap) =>
  ap.runways.flatMap((r) => {
    const [p1, p2] = r.path;
    const [n1, n2] = r.name.split("/");
    const ex = (p1[0] - p2[0]) * Math.cos((p1[1] * Math.PI) / 180);
    const ny = p1[1] - p2[1];
    const m = Math.hypot(ex, ny);
    const off = [(ex / m) * 14, (-ny / m) * 14]; // pixel y grows downward
    return [
      { pos: p1, text: n1, off },
      { pos: p2, text: n2, off: [-off[0], -off[1]] },
    ];
  }),
);
export const AMBER = [255, 176, 0];
export const MIL = [255, 59, 48];
export const TEAL = [78, 162, 174];
// Historical fused path (fct_flight_path click-through): a muted cool slate — deliberately outside the live
// amber/altitude language AND the teal furniture, so a past journey reads as its own class at a glance.
export const HISTORY = [150, 172, 210];
export const KT_TO_MS = 0.514444;
// Beyond this the projection outruns reality (turns, descents) — cap the lead here.
export const MAX_DR_S = 15;
// Hold the lead briefly, then settle back onto the last real fix — a frozen row must not
// hold a fabricated position for the rest of the 120 s window. Both must stay > PING_GAP_S
// so any contact that visibly parked fires the acquisition ping on return.
export const DR_HOLD_S = 20;
export const DR_PARK_S = 26;
