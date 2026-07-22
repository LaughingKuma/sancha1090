from dataclasses import dataclass, field
from itertools import pairwise
import math

EARTH_R_NM = 3440.065
KM_PER_NM = 1.852


class NearAntipodal(ValueError):
    pass


def haversine_nm(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_R_NM * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    return math.degrees(math.atan2(y, x)) % 360.0


def gc_point(lat1, lon1, lat2, lon2, f, max_central_deg=179.0):
    d = haversine_nm(lat1, lon1, lat2, lon2) / EARTH_R_NM
    if math.degrees(d) > max_central_deg:
        raise NearAntipodal(f"central angle {math.degrees(d):.1f} deg")
    if d < 1e-12:
        return (lat1, lon1)
    sd = math.sin(d)
    a = math.sin((1 - f) * d) / sd
    b = math.sin(f * d) / sd
    p1, l1 = math.radians(lat1), math.radians(lon1)
    p2, l2 = math.radians(lat2), math.radians(lon2)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return (math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x)))


def dr_point(lat, lon, bearing_deg, dist_nm):
    d = dist_nm / EARTH_R_NM
    tc = math.radians(bearing_deg)
    p1 = math.radians(lat)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(tc))
    dlam = math.atan2(math.sin(tc) * math.sin(d) * math.cos(p1),
                      math.cos(d) - math.sin(p1) * math.sin(p2))
    lon2 = (math.degrees(math.radians(lon) + dlam) + 540.0) % 360.0 - 180.0
    return (math.degrees(p2), lon2)


def angle_diff_deg(a, b):
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d


def _finite(x):
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


@dataclass(frozen=True)
class Fix:
    ts: float
    lat: float
    lon: float
    alt_ft: float | None
    on_ground: bool
    gs_kt: float | None
    track_deg: float | None
    source: str


@dataclass(frozen=True)
class Endpoint:
    lat: float | None = None
    lon: float | None = None
    source: str | None = None
    agreement: str | None = None


@dataclass(frozen=True)
class OD:
    origin: Endpoint = field(default_factory=Endpoint)
    dest: Endpoint = field(default_factory=Endpoint)


@dataclass(frozen=True)
class EstConfig:
    gap_min_s: float = 600.0
    sample_s: float = 60.0
    motion_fallback_s: float = 600.0
    gap_min_kt: float = 30.0
    gap_max_kt: float = 800.0
    entry_gs_min_kt: float = 80.0
    entry_gs_max_kt: float = 700.0
    extension_min_km: float = 50.0
    bearing_cone_deg: float = 90.0
    dest_cap_s: float = 14400.0
    origin_cap_s: float = 14400.0
    dr_cap_s: float = 600.0
    decel_ramp_nm: float = 40.0
    accel_ramp_nm: float = 50.0
    dest_stop_short_nm: float = 10.0
    dest_ramp_floor_kt: float = 140.0
    origin_ramp_floor_kt: float = 160.0
    wind_sample_nm: float = 250.0
    max_central_deg: float = 179.0


DEFAULT_CONFIG = EstConfig()


def norm_track(t):
    if not _finite(t):
        return None
    return float(t) % 360.0


def valid_motion(fix, kind, cfg):
    if not _finite(fix.gs_kt) or not (cfg.gap_min_kt <= fix.gs_kt <= cfg.gap_max_kt):
        return False
    if kind in ("ext", "dr") and norm_track(fix.track_deg) is None:
        return False
    return True


def find_motion(fixes, idx, direction, kind, cfg, stop_idx):
    anchor_ts = fixes[idx].ts
    i = idx
    while 0 <= i < len(fixes) and i != stop_idx:
        f = fixes[i]
        if abs(f.ts - anchor_ts) > cfg.motion_fallback_s:
            return None
        if valid_motion(f, kind, cfg):
            return f
        i += direction
    return None


def prepare(points):
    fixes = []
    for ts, lat, lon, alt, ground, gs, track, src in points:
        if not (_finite(ts) and _finite(lat) and _finite(lon)):
            continue
        fixes.append(Fix(float(ts), float(lat), float(lon),
                         float(alt) if _finite(alt) else None,
                         bool(ground),
                         float(gs) if _finite(gs) else None,
                         float(track) if _finite(track) else None,
                         str(src)))
    fixes.sort(key=lambda f: f.ts)
    out, seen = [], set()
    for f in fixes:
        if f.ts in seen:
            continue
        seen.add(f.ts)
        out.append(f)
    return out


def detect_gaps(fixes, cfg):
    return [(i, i + 1) for i in range(len(fixes) - 1)
            if fixes[i + 1].ts - fixes[i].ts > cfg.gap_min_s]


def _run_bounds(gaps, i, j, n):
    # entry run extends back to just after the previous gap; exit run forward to just before the next
    prev_end = max((b for a, b in gaps if b <= i), default=0)
    next_start = min((a for a, b in gaps if a >= j), default=n - 1)
    return prev_end - 1, next_start + 1  # exclusive stop_idx values for find_motion


def gap_eligibility(fixes, i, j, gaps, cfg):
    a, b = fixes[i], fixes[j]
    if a.on_ground or b.on_ground:
        return "on_ground_edge"
    dist = haversine_nm(a.lat, a.lon, b.lat, b.lon)
    if math.degrees(dist / EARTH_R_NM) > cfg.max_central_deg:
        return "near_antipodal"
    implied_kt = dist / ((b.ts - a.ts) / 3600.0)
    if not (cfg.gap_min_kt <= implied_kt <= cfg.gap_max_kt):
        return "gap_kinematics"
    back_stop, fwd_stop = _run_bounds(gaps, i, j, len(fixes))
    entry = find_motion(fixes, i, -1, "gap", cfg, stop_idx=back_stop)
    exit_ = find_motion(fixes, j, +1, "gap", cfg, stop_idx=fwd_stop)
    if entry is None or exit_ is None:
        return "invalid_motion"
    return (entry, exit_)


def ext_eligibility(fixes, od, end, cfg):
    ep = od.origin if end == "origin" else od.dest
    if ep.lat is None or ep.lon is None:
        return "missing_endpoint"
    edge_idx = 0 if end == "origin" else len(fixes) - 1
    edge = fixes[edge_idx]
    if edge.on_ground:
        return "on_ground_edge"
    dist_nm = haversine_nm(edge.lat, edge.lon, ep.lat, ep.lon)
    if math.degrees(dist_nm / EARTH_R_NM) > cfg.max_central_deg:
        return "near_antipodal"
    if dist_nm * KM_PER_NM < cfg.extension_min_km:
        return "below_min_distance"
    direction = +1 if end == "origin" else -1
    stop = len(fixes) if end == "origin" else -1
    # temporal bound only: safe while motion_fallback_s <= gap_min_s, so the walk cannot cross a real gap
    motion = find_motion(fixes, edge_idx, direction, "ext", cfg, stop_idx=stop)
    if motion is None:
        return "invalid_motion"
    if not (cfg.entry_gs_min_kt <= motion.gs_kt <= cfg.entry_gs_max_kt):
        return "entry_speed_envelope"
    track = norm_track(motion.track_deg)
    if end == "dest":
        brg = initial_bearing_deg(edge.lat, edge.lon, ep.lat, ep.lon)
    else:
        brg = initial_bearing_deg(ep.lat, ep.lon, edge.lat, edge.lon)
    if angle_diff_deg(track, brg) > cfg.bearing_cone_deg:
        return "bearing_conflict"
    return {"motion": motion, "dist_nm": dist_nm, "bearing_deg": brg}


@dataclass(frozen=True)
class Segment:
    kind: str
    points: list
    meta: dict


def round_ts(x):
    return int(math.floor(x + 0.5))


def _gap_bin(duration_s):
    if duration_s <= 3600:
        return "gap_15_60m"
    if duration_s <= 10800:
        return "gap_60_180m"
    return "gap_180m_plus"


def build_gap(a, b, entry, exit_, cfg):
    total_t = b.ts - a.ts
    gs_in, gs_out = entry.gs_kt, exit_.gs_kt
    # trapezoid distance fraction, renormalized so f(total_t) == 1 exactly
    denom = gs_in * total_t + (gs_out - gs_in) * total_t / 2.0
    both_alt = a.alt_ft is not None and b.alt_ft is not None

    def frac(tau):
        if denom <= 0:
            return tau / total_t
        return (gs_in * tau + (gs_out - gs_in) * tau * tau / (2.0 * total_t)) / denom

    points = [[a.lon, a.lat, round_ts(a.ts), a.alt_ft]]
    tau = cfg.sample_s
    while tau < total_t:
        f = min(1.0, frac(tau))
        lat, lon = gc_point(a.lat, a.lon, b.lat, b.lon, f, cfg.max_central_deg)
        alt = a.alt_ft + (b.alt_ft - a.alt_ft) * (tau / total_t) if both_alt else None
        points.append([lon, lat, round_ts(a.ts + tau), alt])
        tau += cfg.sample_s
    points.append([b.lon, b.lat, round_ts(b.ts), b.alt_ft])
    return Segment("gap", points, {
        "gs_entry_kt": gs_in, "gs_exit_kt": gs_out, "capped": False,
        "bin": _gap_bin(total_t),
        "confidence": {"endpoint_source": None, "endpoint_agreement": None,
                       "times_low_confidence": False},
    })


def _speed_at(schedule, x):
    for (x0, v0), (x1, v1) in pairwise(schedule):
        if x <= x1:
            if x1 == x0:
                return v1
            return v0 + (v1 - v0) * (x - x0) / (x1 - x0)
    return schedule[-1][1]


def integrate_schedule(schedule, sample_s, cap_s, dx_nm=0.25):
    x_end = schedule[-1][0]
    samples = [(0.0, 0.0)]
    x, t, next_emit = 0.0, 0.0, sample_s
    capped = False
    while x < x_end:
        step = min(dx_nm, x_end - x)
        v = max(1.0, _speed_at(schedule, x + step / 2.0))
        dt = step / v * 3600.0
        # emit AT exact sample boundaries: interpolate inside the crossing substep
        while t + dt >= next_emit and next_emit < cap_s:
            f = (next_emit - t) / dt
            samples.append((x + step * f, next_emit))
            next_emit += sample_s
        if t + dt >= cap_s:
            f = (cap_s - t) / dt
            samples.append((x + step * f, cap_s))
            capped = True
            break
        x, t = x + step, t + dt
    if not capped and samples[-1][0] < x_end:
        samples.append((x_end, t))
    return samples, capped


def _ext_confidence(ep, low_conf):
    return {"endpoint_source": ep.source, "endpoint_agreement": ep.agreement,
            "times_low_confidence": low_conf}


def build_dest_ext(last, motion, dest, dist_nm, cfg):
    entry_gs = motion.gs_kt
    dist_ext = dist_nm - cfg.dest_stop_short_nm
    ramp_start = max(0.0, dist_ext - cfg.decel_ramp_nm)
    target = min(entry_gs, cfg.dest_ramp_floor_kt)
    schedule = [(0.0, entry_gs), (ramp_start, entry_gs), (dist_ext, target)]
    samples, capped = integrate_schedule(schedule, cfg.sample_s, cfg.dest_cap_s)
    points = [[last.lon, last.lat, round_ts(last.ts), None]]
    for x, t in samples[1:]:
        f = x / dist_nm
        lat, lon = gc_point(last.lat, last.lon, dest.lat, dest.lon, f, cfg.max_central_deg)
        points.append([lon, lat, round_ts(last.ts + t), None])
    return Segment("dest_ext", points, {
        "gs_entry_kt": entry_gs, "gs_exit_kt": target if not capped else None,
        "capped": capped, "bin": "dest_ext",
        "confidence": _ext_confidence(dest, False),
    })


def build_origin_ext(first, motion, origin, dist_nm, cfg):
    entry_gs = motion.gs_kt
    start_v = min(entry_gs, cfg.origin_ramp_floor_kt)
    ramp_end = min(cfg.accel_ramp_nm, dist_nm)
    # schedule measured FROM THE AIRPORT; integrate outward from the observed fix by mirroring x
    airport_sched = [(0.0, start_v), (ramp_end, entry_gs), (dist_nm, entry_gs)]
    mirrored = [(dist_nm - x, v) for x, v in reversed(airport_sched)]
    samples, capped = integrate_schedule(mirrored, cfg.sample_s, cfg.origin_cap_s)
    rev = []
    for x, t in samples[1:]:
        f = (dist_nm - x) / dist_nm   # fraction along origin->first_fix
        lat, lon = gc_point(origin.lat, origin.lon, first.lat, first.lon, f, cfg.max_central_deg)
        rev.append([lon, lat, round_ts(first.ts - t), None])
    points = list(reversed(rev)) + [[first.lon, first.lat, round_ts(first.ts), None]]
    return Segment("origin_ext", points, {
        "gs_entry_kt": entry_gs, "gs_exit_kt": entry_gs,
        "capped": capped, "bin": "origin_ext",
        "confidence": _ext_confidence(origin, True),
    })


def build_dr(anchor, motion, cfg):
    gs = motion.gs_kt
    track = norm_track(motion.track_deg)
    points = [[anchor.lon, anchor.lat, round_ts(anchor.ts), anchor.alt_ft]]
    t = cfg.sample_s
    while t <= cfg.dr_cap_s:
        lat, lon = dr_point(anchor.lat, anchor.lon, track, gs * t / 3600.0)
        points.append([lon, lat, round_ts(anchor.ts + t), anchor.alt_ft])
        t += cfg.sample_s
    if points[-1][2] != round_ts(anchor.ts + cfg.dr_cap_s):
        lat, lon = dr_point(anchor.lat, anchor.lon, track, gs * cfg.dr_cap_s / 3600.0)
        points.append([lon, lat, round_ts(anchor.ts + cfg.dr_cap_s), anchor.alt_ft])
    return Segment("dr", points, {
        "gs_entry_kt": gs, "gs_exit_kt": gs, "capped": True, "bin": "dr",
        "confidence": {"endpoint_source": None, "endpoint_agreement": None,
                       "times_low_confidence": False},
    })


@dataclass(frozen=True)
class EstimateResult:
    segments: list
    skips: list
    wind_request: list


def _wind_request_for(seg_idx, seg, cfg):
    # anchor samples first: PR-4's TAS inference needs wind at BOTH gap edges and at the
    # observed-fix anchor for ext/dr (design section 8); then one sample per wind_sample_nm
    pts = seg.points
    if seg.kind == "gap":
        anchors = [pts[0], pts[-1]]
    elif seg.kind == "origin_ext":
        anchors = [pts[-1]]
    else:
        anchors = [pts[0]]
    marks = [(seg_idx, a[1], a[0], a[3], a[2]) for a in anchors]
    cum = 0.0
    next_mark = cfg.wind_sample_nm / 2.0
    for p1, p2 in pairwise(pts):
        leg = haversine_nm(p1[1], p1[0], p2[1], p2[0])
        while leg > 0.0 and cum + leg >= next_mark:
            f = (next_mark - cum) / leg
            # interpolated positions never invent altitude: either edge NULL -> NULL (ZeroWind rule)
            alt = None if p1[3] is None or p2[3] is None else p1[3] + (p2[3] - p1[3]) * f
            lat, lon = gc_point(p1[1], p1[0], p2[1], p2[0], f, cfg.max_central_deg)
            marks.append((seg_idx, lat, lon, alt, round_ts(p1[2] + (p2[2] - p1[2]) * f)))
            next_mark += cfg.wind_sample_nm
        cum += leg
    return marks


def estimate(points, od, cfg=DEFAULT_CONFIG):
    fixes = prepare(points)
    if not fixes:
        return EstimateResult([], [{"kind": "all", "reason": "no_input"}], [])
    segments, skips = [], []
    gaps = detect_gaps(fixes, cfg)

    # origin end first so segment order is origin_ext, gaps..., dest_ext/dr
    got = ext_eligibility(fixes, od, "origin", cfg)
    if isinstance(got, dict):
        try:
            segments.append(build_origin_ext(fixes[0], got["motion"], od.origin, got["dist_nm"], cfg))
        except NearAntipodal:
            skips.append({"kind": "origin_ext", "reason": "near_antipodal"})
    else:
        skips.append({"kind": "origin_ext", "reason": got})

    for i, j in gaps:
        got = gap_eligibility(fixes, i, j, gaps, cfg)
        if isinstance(got, tuple):
            try:
                segments.append(build_gap(fixes[i], fixes[j], got[0], got[1], cfg))
            except NearAntipodal:
                skips.append({"kind": "gap", "reason": "near_antipodal"})
        else:
            skips.append({"kind": "gap", "reason": got})

    if od.dest.lat is None or od.dest.lon is None:
        # rev-8 trigger contract: historical NULL destination -> capped forward DR
        # on-ground first so the skip reason matches gap/ext precedence
        if fixes[-1].on_ground:
            skips.append({"kind": "dr", "reason": "on_ground_edge"})
        else:
            motion = find_motion(fixes, len(fixes) - 1, -1, "dr", cfg, stop_idx=-1)
            if motion is None:
                skips.append({"kind": "dr", "reason": "invalid_motion"})
            else:
                segments.append(build_dr(fixes[-1], motion, cfg))
    else:
        got = ext_eligibility(fixes, od, "dest", cfg)
        if isinstance(got, dict):
            try:
                segments.append(build_dest_ext(fixes[-1], got["motion"], od.dest, got["dist_nm"], cfg))
            except NearAntipodal:
                skips.append({"kind": "dest_ext", "reason": "near_antipodal"})
        else:
            # rejected known destination: skip ONLY, never a DR fallback
            skips.append({"kind": "dest_ext", "reason": got})

    wind_request = []
    for idx, seg in enumerate(segments):
        wind_request.extend(_wind_request_for(idx, seg, cfg))
    return EstimateResult(segments, skips, wind_request)
