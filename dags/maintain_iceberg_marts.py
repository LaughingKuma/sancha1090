from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator


# Every dbt-rebuilt Iceberg table across all lanes, seeds included — the hourly
# adsb REPLACE cycle (:05) clears the 04:30 maintenance window by 25 minutes, and
# the REPLACE-vs-OPTIMIZE race is the existing retry-absorbed exposure.
SILVER_TABLES = [
    "dim_aircraft",
    "dim_aircraft_registry",
    # dim_aircraft_types is RW-only (livemap dim loads the CSV) — no Iceberg copy to maintain
    "dim_airlines",
    "dim_airports",
    "dim_hex_country",
    "fact_state_snapshots",
    "fct_adsb_state",
    "stg_states",
    "stg_states_history",
]
GOLD_TABLES = [
    "agg_airline_traffic",
    "agg_airport_daily",
    "agg_country_traffic",
    "agg_country_traffic_adsb",
    "agg_flight_routes",
    "agg_hourly_traffic",
    "agg_hourly_traffic_history",
    "agg_hourly_traffic_live_archive",
    "agg_operator_traffic",
    "agg_route_traffic",
    "anomalies",
    "fact_flights",
    "fct_flight_legs",
    "longest_flights",
]

RETENTION = "7d"


@dag(
    dag_id="maintain_iceberg_marts",
    description="Daily compaction + snapshot expiry for silver+gold Iceberg marts",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule="30 4 * * *",
    catchup=False,
    max_active_runs=1,
    default_args={"owner": "amit", "retries": 1, "retry_delay": timedelta(minutes=5)},
    tags=["sancha1090", "iceberg", "maintenance", "v2"],
)
def maintain_iceberg_marts():
    # One statement per list entry: the Trino DBAPI runs a single statement per
    # execute, so SQLExecuteQueryOperator iterates the list rather than splitting.
    def _ops(ns: str, tables: list[str], op: str) -> list[str]:
        if op == "optimize":
            return [f"ALTER TABLE iceberg.{ns}.{t} EXECUTE optimize" for t in tables]
        if op == "expire":
            return [
                f"ALTER TABLE iceberg.{ns}.{t} EXECUTE expire_snapshots(retention_threshold => '{RETENTION}')"
                for t in tables
            ]
        if op == "orphans":
            return [
                f"ALTER TABLE iceberg.{ns}.{t} EXECUTE remove_orphan_files(retention_threshold => '{RETENTION}')"
                for t in tables
            ]
        raise ValueError(f"unsupported op {op!r} for ns {ns!r}")

    def _task(task_id: str, ns: str, tables: list[str], op: str) -> SQLExecuteQueryOperator:
        return SQLExecuteQueryOperator(task_id=task_id, conn_id="trino_default", sql=_ops(ns, tables, op))

    optimize_silver = _task("optimize_silver", "silver", SILVER_TABLES, "optimize")
    expire_silver = _task("expire_silver", "silver", SILVER_TABLES, "expire")
    orphans_silver = _task("orphans_silver", "silver", SILVER_TABLES, "orphans")
    optimize_gold = _task("optimize_gold", "gold", GOLD_TABLES, "optimize")
    expire_gold = _task("expire_gold", "gold", GOLD_TABLES, "expire")
    orphans_gold = _task("orphans_gold", "gold", GOLD_TABLES, "orphans")

    # Serialize optimize -> expire -> orphans: orphan-removal sweeps last so it
    # can't delete files snapshots still reference. Both optimizes finish before
    # any expire to bound Trino concurrency to one phase at a time.
    [optimize_silver, optimize_gold] >> expire_silver
    [optimize_silver, optimize_gold] >> expire_gold
    expire_silver >> orphans_silver
    expire_gold >> orphans_gold


maintain_iceberg_marts()
