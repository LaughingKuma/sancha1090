from __future__ import annotations

# Re-land trace-days the live-site lane fetched at lag <= 1 and landed truncated: the 'landed'
# ledger row froze the mid-air cut. Recipe: docs/notes/runbooks.md#adsblol-class-1-endpoint-recovery
import os
import sys

# Runs inside an airflow container (docker exec sancha1090-airflow-scheduler-1 ...)
# where ClickHouse/analytics endpoints resolve; scripts/ is bind-mounted there.
sys.path.insert(0, "/opt/airflow")

from include import adsblol_route_ledger as ledger
from include.clickhouse import ch_client
from scripts import backfill_adsblol_resegment as resegment

# The last live-site landing was the 2026-08-30 03:00Z sweep (attempted_at <= 03:31Z); every stamp
# past this is a tar landing. The 08-29 pre-merge tar tests cost at most one wasted re-land day (08-28).
TAR_CUTOVER = "2026-08-30T04:00:00+00:00"

# A dest-NULL flight whose last overlapping segment ends airborne before 23:50 of the trace day is
# a cut trace, not a real overnight; the ±1 h window keeps the join to the flight's own segments.
# The trace_day bound prunes whole partitions: nothing past the cutover can be class-1.
CANDIDATES_SQL = """
WITH f AS (
  SELECT icao24, start_time, end_time
  FROM {gold}.fct_flights_reconciled
  WHERE dest_icao IS NULL AND icao24 IS NOT NULL
)
SELECT DISTINCT hex, trace_day FROM (
  SELECT lower(f.icao24) AS hex,
         argMax(s.trace_day, s.seg_end) AS trace_day,
         max(s.seg_end) AS cut_end,
         argMax(s.last_on_ground, s.seg_end) AS last_gnd
  FROM f
  JOIN (SELECT lower(icao24) AS hex, trace_day, seg_start, seg_end, last_on_ground
        FROM bronze.adsblol_flight_segments FINAL
        WHERE trace_day <= toDate('{cutover_day}')) s
    ON s.hex = lower(f.icao24)
  WHERE s.seg_end >= f.start_time - INTERVAL 1 HOUR AND s.seg_start <= f.end_time + INTERVAL 1 HOUR
  GROUP BY f.icao24, f.start_time, f.end_time
  HAVING last_gnd = 0
     AND cut_end < toDateTime(trace_day) + INTERVAL 23 HOUR + INTERVAL 50 MINUTE
)
"""


def affected_pairs(client=None, engine=None) -> list[tuple[str, str]]:
    sql = CANDIDATES_SQL.format(gold=os.environ.get("CH_GOLD_SCHEMA", "gold_ch"),
                                cutover_day=TAR_CUTOVER[:10])
    c = client or ch_client()
    try:
        rows = c.query(sql).result_rows
    finally:
        if client is None:
            c.close()
    candidates = [(r[0], resegment._day_iso(r[1])) for r in rows]
    live = ledger.landed_live_era(candidates, TAR_CUTOVER, engine)
    return sorted(live, key=lambda p: (p[1], p[0]))


def main(argv=None) -> int:
    return resegment.cli(
        argv, affected=affected_pairs,
        description="Re-land adsb.lol trace-days the live-site lane landed truncated (class-1).",
        execute_help="Clear the frozen 'landed' ledger rows and re-land from the release tar.")


if __name__ == "__main__":
    raise SystemExit(main())
