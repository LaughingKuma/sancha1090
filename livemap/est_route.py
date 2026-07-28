import re

# raw oceanic coordinate tokens only (DDMM[NS]/DDDMM[EW]); named fixes and airways are
# deliberately out of scope — no navigation database ships with the sidecar
_COORD_RE = re.compile(r"\b(\d{2})(\d{2})([NS])/(\d{3})(\d{2})([EW])\b")


def parse_route_coords(route):
    if not route:
        return []
    out = []
    for lat_d, lat_m, ns, lon_d, lon_m, ew in _COORD_RE.findall(route):
        if int(lat_m) >= 60 or int(lon_m) >= 60:
            continue
        lat = int(lat_d) + int(lat_m) / 60.0
        lon = int(lon_d) + int(lon_m) / 60.0
        # composed-value bound, not raw degrees: 18000W (=180°, filed on real transpacific
        # plans) must survive while 9059N (=90.98°) and 18059W must not
        if lat > 90.0 or lon > 180.0:
            continue
        out.append((-lat if ns == "S" else lat, -lon if ew == "W" else lon))
    return out
