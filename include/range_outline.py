from __future__ import annotations

import os

import psycopg2
import trino
from psycopg2.extras import execute_values

# Real antenna is a secret (home rooftop) — from .env; default is the public Carrot Tower landmark.
FEEDER_LAT = float(os.environ.get("LIVEMAP_FEEDER_LAT", "35.6434"))
FEEDER_LON = float(os.environ.get("LIVEMAP_FEEDER_LON", "139.6692"))

BINS = int(os.environ.get("RANGE_OUTLINE_BINS", "120"))          # bearing bins (120 = 3° each)
MAX_NMI = float(os.environ.get("RANGE_OUTLINE_MAX_NMI", "300"))  # drop bogus far hits beyond ADS-B range
# garbage-guard prefilter, derived from the feeder so it relocates with the receiver (6° ≫ any reception)
BBOX_MARGIN_DEG = float(os.environ.get("RANGE_OUTLINE_BBOX_MARGIN_DEG", "6"))


def _compute(lat0: float, lon0: float) -> list[tuple]:
    # Farthest decoded position per bearing bin = the coverage envelope. great_circle_distance is km (/1.852 → nmi).
    bin_deg = 360.0 / BINS
    m = BBOX_MARGIN_DEG
    sql = f"""
    WITH calc AS (
      SELECT s.lat, s.lon,
        great_circle_distance({lat0}, {lon0}, s.lat, s.lon) / 1.852 AS dist_nmi,
        mod((degrees(atan2(
          sin(radians(s.lon - {lon0})) * cos(radians(s.lat)),
          cos(radians({lat0})) * sin(radians(s.lat))
            - sin(radians({lat0})) * cos(radians(s.lat)) * cos(radians(s.lon - {lon0}))
        )) + 360.0), 360.0) AS bearing
      FROM adsb_states s
      WHERE s.lat BETWEEN {lat0 - m} AND {lat0 + m} AND s.lon BETWEEN {lon0 - m} AND {lon0 + m}
    ),
    binned AS (
      SELECT cast(floor(bearing / {bin_deg}) AS int) AS bin,
             max_by(ARRAY[lat, lon], dist_nmi) AS far,
             max(dist_nmi) AS max_nmi
      FROM calc WHERE dist_nmi BETWEEN 1 AND {MAX_NMI}
      GROUP BY 1
    )
    SELECT bin, far[1] AS lat, far[2] AS lon, max_nmi FROM binned ORDER BY bin
    """
    conn = trino.dbapi.connect(
        host=os.environ.get("TRINO_HOST", "trino-coordinator"),
        port=int(os.environ.get("TRINO_PORT", "8080")),
        user=os.environ.get("TRINO_USER", "airflow"),
        catalog="iceberg",
        schema="bronze",
    )
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return [(int(b), float(la), float(lo), float(nmi)) for b, la, lo, nmi in cur.fetchall()]
    finally:
        conn.close()


def refresh_range_outline() -> int:
    rows = _compute(FEEDER_LAT, FEEDER_LON)
    # guard: a sparse polygon means a bad feeder coord or a thin window — don't blank a good outline
    if len(rows) < BINS // 2:
        raise RuntimeError(f"range outline only {len(rows)}/{BINS} bins — refusing to load a sparse polygon")
    conn = psycopg2.connect(
        host=os.environ.get("RISINGWAVE_HOST", "risingwave"),
        port=int(os.environ.get("RISINGWAVE_PORT", "4566")),
        user="root",
        dbname="dev",
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS range_outline "
                "(bin int, lat double precision, lon double precision, dist_nmi double precision, gen bigint)"
            )
            # Versioned publish (RW has no read-write txns): write a new gen, FLUSH, then drop older
            # gens — readers select max(gen) so a failed load keeps serving the last complete polygon.
            cur.execute("SELECT coalesce(max(gen), 0) + 1 FROM range_outline")
            new_gen = cur.fetchone()[0]
            tagged = [(b, la, lo, nmi, new_gen) for (b, la, lo, nmi) in rows]
            execute_values(
                cur,
                "INSERT INTO range_outline (bin, lat, lon, dist_nmi, gen) VALUES %s",
                tagged,
                template="(%s, %s, %s, %s, %s)",
                page_size=len(tagged),  # one INSERT statement
            )
            cur.execute("FLUSH")  # new generation becomes visible to readers
            cur.execute(f"DELETE FROM range_outline WHERE gen IS NULL OR gen <> {new_gen}")
            cur.execute("FLUSH")  # retire prior generations
    finally:
        conn.close()
    print(f"loaded {len(rows)} range-outline bins (gen {new_gen}) into RisingWave")
    return len(rows)


if __name__ == "__main__":
    refresh_range_outline()
