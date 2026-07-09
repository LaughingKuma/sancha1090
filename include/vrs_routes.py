from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import urlopen

log = logging.getLogger(__name__)

ROUTES_URL = "https://vrs-standing-data.adsb.lol/routes.csv"
_EXPECTED_HEADER = ["Callsign", "Code", "Number", "AirlineCode", "AirportCodes"]
# ~618k rows upstream; a short fetch is a broken mirror, never a real shrink.
_MIN_ROWS = 300_000
_RAW_PREFIX = "dims/vrs_routes_raw"
_DIM_TABLE = "dim.dim_vrs_routes"
_COLS = ["callsign", "code", "number", "airline_code", "airport_codes"]


def fetch_routes_csv(url: str = ROUTES_URL) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "vrs-standing-data.adsb.lol":
        raise ValueError(f"unsupported source URL: {url}")
    with urlopen(url, timeout=120) as resp:
        return resp.read().decode("utf-8")


def parse_routes(text: str, min_rows: int = _MIN_ROWS) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    header = next(reader, None)
    if header != _EXPECTED_HEADER:
        raise ValueError(f"vrs routes.csv header drift: {header!r}")
    rows = [r for r in reader if len(r) == 5 and r[0] and r[4]]
    if len(rows) < min_rows:
        raise ValueError(f"vrs routes.csv parsed only {len(rows)} rows; refusing to load")
    return rows


def load_vrs_routes_to_ch() -> dict:
    from include.clickhouse import ch_client
    from include.s3_helpers import get_bucket, get_s3fs

    text = fetch_routes_csv()
    rows = parse_routes(text)

    # Same-day re-run overwrites the same key: idempotent provenance copy, not a per-attempt archive.
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fs, bucket = get_s3fs(), get_bucket()
    uri = f"{bucket}/{_RAW_PREFIX}/routes-{day}.csv"
    fs.pipe(uri, text.encode("utf-8"))

    client = ch_client()
    try:
        client.insert(_DIM_TABLE, rows, column_names=_COLS)
    finally:
        client.close()
    log.info("vrs_routes: loaded %d rows (raw copy %s)", len(rows), uri)
    return {"rows": len(rows), "garage_uri": uri}
