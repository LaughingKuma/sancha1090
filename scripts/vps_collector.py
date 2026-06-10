#!/usr/bin/env python3
from __future__ import annotations

import io
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import polars as pl
import pyarrow.parquet as pq
from pyarrow.fs import S3FileSystem


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vps_collector")


# Keep in sync with include/regions.py by hand: vps_up.sh ships this file as a
# single-file cloud-init payload, so the VPS can't import the include/ package.
REGIONS: list[dict[str, Any]] = [
    {"name": "japan", "lamin": 20.0, "lomin": 122.0, "lamax": 50.0, "lomax": 165.0},
]


OPENSKY_BASE_URL = "https://opensky-network.org/api"
OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)


class OpenSkyClient:
    def __init__(self, client_id: str, client_secret: str, timeout: float = 30.0, max_retries: int = 5):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.max_retries = max_retries
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry - 30:
            return self._token
        r = httpx.post(
            OPENSKY_TOKEN_URL,
            data={"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": self.client_secret},
            timeout=self.timeout,
        )
        r.raise_for_status()
        p = r.json()
        self._token = p["access_token"]
        self._token_expiry = now + p.get("expires_in", 1800)
        return self._token

    def get_states(self, bbox: tuple[float, float, float, float]) -> dict[str, Any]:
        lamin, lomin, lamax, lomax = bbox
        params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        backoff = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                r = httpx.get(f"{OPENSKY_BASE_URL}/states/all", params=params, headers=headers, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == self.max_retries:
                        r.raise_for_status()
                    log.warning("opensky %d attempt=%d/%d sleeping=%.1fs", r.status_code, attempt, self.max_retries, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.RequestError as exc:
                log.warning("opensky network err attempt=%d/%d: %s", attempt, self.max_retries, exc)
                if attempt == self.max_retries:
                    raise
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        raise RuntimeError(f"opensky failed after {self.max_retries} retries")


def _r2_fs() -> S3FileSystem:
    return S3FileSystem(
        endpoint_override=os.environ["R2_ENDPOINT"],
        access_key=os.environ["R2_ACCESS_KEY"],
        secret_key=os.environ["R2_SECRET"],
        region=os.environ.get("R2_REGION", "auto"),
        scheme="https",
    )


def fetch_and_write(region: dict[str, Any], client: OpenSkyClient, fs: S3FileSystem, bucket: str, run_ts: datetime) -> dict[str, Any]:
    bbox = (float(region["lamin"]), float(region["lomin"]), float(region["lamax"]), float(region["lomax"]))
    payload = client.get_states(bbox=bbox)
    states = payload.get("states") or []
    if not states:
        return {"region": region["name"], "rows": 0, "uri": None}

    df = pl.DataFrame(
        states,
        schema=["icao24", "callsign", "origin_country", "time_position", "last_contact",
                "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
                "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk",
                "spi", "position_source"],
        orient="row",
    ).drop("sensors").with_columns(
        pl.lit(payload["time"]).alias("snapshot_time"),
        pl.lit(region["name"]).alias("region"),
        pl.lit(run_ts.isoformat()).alias("ingested_at"),
    )

    key = (
        f"{bucket}/bronze/states_raw/"
        f"dt={run_ts.strftime('%Y-%m-%d')}/"
        f"hr={run_ts.strftime('%H')}/"
        f"min={run_ts.strftime('%M')}/"
        f"region={region['name']}.parquet"
    )

    buf = io.BytesIO()
    pq.write_table(df.to_arrow(), buf, compression="snappy")
    buf.seek(0)
    with fs.open_output_stream(key) as f:
        f.write(buf.read())

    uri = f"s3://{key}"
    log.info("wrote rows=%d uri=%s", df.height, uri)
    return {"region": region["name"], "rows": df.height, "uri": uri}


def main() -> int:
    for k in ("OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "R2_ENDPOINT", "R2_ACCESS_KEY", "R2_SECRET"):
        if not os.environ.get(k):
            log.error("missing required env: %s", k)
            return 2

    bucket = os.environ.get("R2_BUCKET", "opensky-vps-buffer")
    # Snap to the 12-minute slot so files line up with the local ingest_states partitions.
    now = datetime.now(timezone.utc)
    slot_minute = (now.minute // 12) * 12
    run_ts = now.replace(minute=slot_minute, second=0, microsecond=0)

    client = OpenSkyClient(os.environ["OPENSKY_CLIENT_ID"], os.environ["OPENSKY_CLIENT_SECRET"])
    fs = _r2_fs()

    results = []
    for region in REGIONS:
        try:
            results.append(fetch_and_write(region, client, fs, bucket, run_ts))
        except Exception as exc:
            log.error("region %s failed: %s", region["name"], exc)
            results.append({"region": region["name"], "rows": 0, "uri": None, "error": str(exc)})

    total_rows = sum(r["rows"] for r in results)
    with_data = sum(1 for r in results if r.get("uri"))
    log.info("summary: regions=%d with_data=%d total_rows=%d", len(REGIONS), with_data, total_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
