import { S } from "./state.js?v=6.34";

// Great-circle range/bearing from the receiver — S.feederCenter is [lon, lat] from /range-outline.
export function stationVector(lon, lat) {
  if (!S.feederCenter || lon == null || lat == null) return null;
  const toRad = Math.PI / 180;
  const [flon, flat] = S.feederCenter;
  const dLat = (lat - flat) * toRad;
  const dLon = (lon - flon) * toRad;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(flat * toRad) * Math.cos(lat * toRad) * Math.sin(dLon / 2) ** 2;
  const nm = 2 * 3440.065 * Math.asin(Math.sqrt(a)); // earth radius in nm
  const y = Math.sin(dLon) * Math.cos(lat * toRad);
  const x =
    Math.cos(flat * toRad) * Math.sin(lat * toRad) -
    Math.sin(flat * toRad) * Math.cos(lat * toRad) * Math.cos(dLon);
  const brg = ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
  return { nm, brg };
}

// Route endpoint: city with its code when both exist, city alone when the code is missing, bare code otherwise.
export const routeEnd = (city, code) => {
  if (city && code) return `${city} · ${code}`;
  if (city) return city;
  return code ?? "—";
};

// ADS-B emitter category → ICAO wake-turbulence class (the terms ATC/pilots use), not the
// raw DO-260 size names — so a 737 reads Medium, not "Large". B*/C* keep a vehicle-type label.
const CATEGORY_LABEL = {
  A1: "Light", A2: "Medium", A3: "Medium", A4: "Heavy", A5: "Heavy",
  B1: "Glider", B2: "LTA", B4: "Ultralight", B6: "UAV", B7: "Space",
  C1: "Surface", C2: "Surface", C3: "Obstacle",
};
// Wake class is a property of the airframe TYPE: per-typecode overrides first, then body_class
// (curated from dim_aircraft_types), then the noisy ADS-B category (often A0 = "no info") as last resort.
const WAKE_BY_BODY = { ga: "Light", regional: "Medium", narrowbody: "Medium", widebody: "Heavy", quad: "Heavy" };
// A380 is a quad but its own ICAO wake class (Super); body_class can't express that.
const WAKE_BY_TYPE = { A388: "Super" };
export const classLabel = (a) => {
  if (a.is_helicopter === true) return null; // HELI badge already covers rotorcraft — no wake chip
  return WAKE_BY_TYPE[a.typecode] ?? WAKE_BY_BODY[a.body_class] ?? CATEGORY_LABEL[a.category] ?? null;
};

// D-2-sourced rows carry an old departure time — show the clock only when it's today's leg
export function routeSuffix(r) {
  if (!r) return "";
  const dep = r.departed_epoch;
  const ageH = dep ? (Date.now() / 1000 - dep) / 3600 : Infinity;
  return ageH < 24 ? ` · departed ${new Date(dep * 1000).toTimeString().slice(0, 5)}` : " · usual route";
}
