from __future__ import annotations

import os

from psycopg2.extras import execute_values

from include.clickhouse import ch_client
from include.db import rw_connect

# Route memory horizon: callsigns map to stable scheduled legs, so the latest flight
# within a week is a reliable backstory for an aircraft flying that callsign today.
LOOKBACK_DAYS = int(os.environ.get("FLIGHT_ROUTES_LOOKBACK_DAYS", "7"))
# CH gold-lane schema for the routes source (P3 built marts into gold_ch); one constant so P7's rename is a one-liner.
CH_GOLD_SCHEMA = os.environ.get("CH_GOLD_SCHEMA", "gold_ch")


def _routes_sql() -> str:
    # Latest known route per callsign from the reconciled consensus mart (SP2): one source, endpoint geo
    # already resolved. now('UTC') matches the UTC-stored start_time.
    return f"""
    WITH ranked AS (
      SELECT
        callsign,
        origin_icao,
        coalesce(nullIf(origin_iata, ''), origin_icao) AS origin_code,
        origin_city,
        dest_icao,
        coalesce(nullIf(dest_iata, ''), dest_icao) AS dest_code,
        dest_city,
        toInt64(toUnixTimestamp(start_time)) AS departed_epoch,
        row_number() OVER (PARTITION BY callsign ORDER BY start_time DESC) AS rn
      FROM {CH_GOLD_SCHEMA}.fct_flights_reconciled
      WHERE callsign IS NOT NULL AND origin_icao IS NOT NULL AND dest_icao IS NOT NULL
        AND origin_icao <> dest_icao
        AND start_time > now('UTC') - INTERVAL {LOOKBACK_DAYS} DAY
    )
    SELECT callsign, origin_icao, origin_code, origin_city,
           dest_icao, dest_code, dest_city, departed_epoch
    FROM ranked WHERE rn = 1
    """


def _compute() -> list[tuple]:
    # Latest known route per callsign; both endpoints resolved so the tooltip line
    # always reads "XXX → YYY". now('UTC') matches the UTC-stored start_time.
    sql = _routes_sql()
    client = ch_client()
    try:
        return client.query(sql).result_rows
    finally:
        client.close()


def refresh_flight_routes() -> int:
    rows = _compute()
    conn = rw_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS dim_flight_routes ("
                " callsign varchar, origin_icao varchar, origin_code varchar, origin_city varchar,"
                " dest_icao varchar, dest_code varchar, dest_city varchar,"
                " departed_epoch bigint, gen bigint)"
            )
            # Versioned publish (RW has no read-write txns): write a new gen, FLUSH, then
            # drop older gens — readers select max(gen) so a failed load keeps serving the
            # last complete route set. An empty compute means an upstream gap (the 7-day
            # fct_flights_reconciled lookback can't legitimately be empty once the lane is live), so
            # we deliberately DON'T advance gen on empty — same stale-over-blank choice as
            # range_outline; map.js already downgrades old rows to "usual route".
            cur.execute("SELECT coalesce(max(gen), 0) + 1 FROM dim_flight_routes")
            new_gen = cur.fetchone()[0]
            if rows:
                tagged = [tuple(r) + (new_gen,) for r in rows]
                execute_values(
                    cur,
                    "INSERT INTO dim_flight_routes "
                    "(callsign, origin_icao, origin_code, origin_city,"
                    " dest_icao, dest_code, dest_city, departed_epoch, gen) VALUES %s",
                    tagged,
                    template="(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    page_size=max(len(tagged), 1),
                )
                cur.execute("FLUSH")
                cur.execute("DELETE FROM dim_flight_routes WHERE gen IS NULL OR gen <> %s", (new_gen,))
                cur.execute("FLUSH")
    finally:
        conn.close()
    print(f"loaded {len(rows)} flight routes (gen {new_gen}) into RisingWave")
    return len(rows)


if __name__ == "__main__":
    refresh_flight_routes()
