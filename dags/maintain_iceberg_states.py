from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator


# bronze.adsb_states is excluded: that lane registers external files via add_files
# and has its own maintenance story (maintain_adsb_schema).
BRONZE_TABLES = ["opensky_states", "opensky_flights", "archive_states", "aircraft_db"]

RETENTION = "7d"


@dag(
    dag_id="maintain_iceberg_states",
    description="Daily compaction + snapshot expiry + orphan sweep for the bronze Iceberg tables",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule="30 3 * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["sancha1090", "iceberg", "maintenance"],
)
def maintain_iceberg_states():

    @task
    def ensure_bronze_tables() -> None:
        # Blank-warehouse safety: archive_states only gets data from the one-shot
        # backfill, but the ALTERs below must not fail before the first wave runs.
        from include import archive_iceberg
        from include import flights_iceberg as fib

        archive_iceberg.ensure_archive_table()
        fib.ensure_flights_table()
        fib.ensure_aircraft_db_table()

    # One statement per list entry: the Trino DBAPI runs a single statement per
    # execute, so SQLExecuteQueryOperator iterates the list rather than splitting.
    def _ops(op: str) -> list[str]:
        if op == "optimize":
            return [f"ALTER TABLE iceberg.bronze.{t} EXECUTE optimize" for t in BRONZE_TABLES]
        if op == "expire":
            return [
                f"ALTER TABLE iceberg.bronze.{t} EXECUTE expire_snapshots(retention_threshold => '{RETENTION}')"
                for t in BRONZE_TABLES
            ]
        if op == "orphans":
            return [
                f"ALTER TABLE iceberg.bronze.{t} EXECUTE remove_orphan_files(retention_threshold => '{RETENTION}')"
                for t in BRONZE_TABLES
            ]
        raise ValueError(f"unsupported op {op!r}")

    def _task(task_id: str, op: str) -> SQLExecuteQueryOperator:
        return SQLExecuteQueryOperator(task_id=task_id, conn_id="trino_default", sql=_ops(op))

    optimize = _task("optimize_bronze", "optimize")
    expire = _task("expire_bronze", "expire")
    orphans = _task("orphans_bronze", "orphans")

    # Serialize optimize -> expire -> orphans: orphan-removal sweeps last so it
    # can't delete files snapshots still reference. tableize appends commit
    # concurrently without conflict (append-only lane, no REPLACE); retries
    # absorb the rare optimistic-commit clash.
    ensure_bronze_tables() >> optimize >> expire >> orphans


maintain_iceberg_states()
