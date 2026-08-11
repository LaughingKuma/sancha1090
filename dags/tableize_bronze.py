from __future__ import annotations

import pendulum

from airflow.sdk import dag, task

from include.adsb_assets import adsb_bronze_table, adsb_raw_landed
from include.dag_defaults import default_args
from include.assets import (
    bronze_flights_table,
    bronze_states_table,
    bronze_swim_table,
    raw_flights_landed,
    raw_states_landed,
)


def make_tableize(*, dag_id, description, start_date, schedule, outlet, tags, task_id,
                  loader_name, lane, skip_on_empty=False):
    @dag(
        dag_id=dag_id,
        description=description,
        start_date=start_date,
        schedule=schedule,
        catchup=False,
        max_active_runs=1,
        default_args=default_args(),
        tags=tags,
    )
    def tableize():

        @task(task_id=task_id, outlets=[outlet])
        def load() -> dict:
            # Loader resolved by name at run time so this module stays parse-light for the dag-processor.
            import include.clickhouse

            result = getattr(include.clickhouse, loader_name)()
            if not result.get("ok"):
                raise RuntimeError(f"CH {lane} bronze load failed: {result}")
            if skip_on_empty and result.get("files", 0) == 0:
                from airflow.exceptions import AirflowSkipException

                raise AirflowSkipException(f"no pending {lane} files to load")
            return result

        load()

    return tableize()


# Raise on a failed load so the bronze asset (-> transform_adsb_silver) is never emitted stale;
# the per-batch loader already drains what it can before reporting ok=False.
make_tableize(
    dag_id="tableize_adsb",
    description="Load newly-landed adsb_state Parquet into ClickHouse bronze.adsb_states (byte-mirror)",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=[adsb_raw_landed],
    outlet=adsb_bronze_table,
    tags=["sancha1090", "bronze", "clickhouse", "adsb"],
    task_id="load_adsb_to_clickhouse",
    loader_name="load_adsb_pending_to_ch",
    lane="adsb",
)

# Raise on a failed load so the bronze asset (-> transform_marts) is never emitted stale.
make_tableize(
    dag_id="tableize_states",
    description="Load newly-landed raw state parquet into ClickHouse bronze.opensky_states",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=[raw_states_landed],
    outlet=bronze_states_table,
    tags=["sancha1090", "bronze", "clickhouse"],
    task_id="load_pending_to_clickhouse",
    loader_name="load_states_pending_to_ch",
    lane="states",
)

# CH bronze is the canonical landing target; drains its own ch_loaded_at pending set. Raise on a load
# failure so the task reds and the bronze asset (which triggers transform_flights) is NOT emitted stale.
make_tableize(
    dag_id="tableize_flights",
    description="Load newly-landed raw flights parquet into ClickHouse bronze.opensky_flights",
    start_date=pendulum.datetime(2026, 6, 1, tz="UTC"),
    schedule=[raw_flights_landed],
    outlet=bronze_flights_table,
    tags=["sancha1090", "bronze", "clickhouse", "v5"],
    task_id="load_pending_to_clickhouse",
    loader_name="load_flights_pending_to_ch",
    lane="flights",
)

# Check ok before files: a genuine failure can also report files=0, which must RED not SKIP.
# Cron (the producer is an always-on consumer, not a DAG); most ticks land nothing, so skip over a hollow asset event.
make_tableize(
    dag_id="tableize_swim",
    description="Load newly-landed SWIM parquet into ClickHouse bronze.swim_flightdata",
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    schedule="*/5 * * * *",
    outlet=bronze_swim_table,
    tags=["sancha1090", "bronze", "clickhouse", "swim"],
    task_id="load_pending_to_clickhouse",
    loader_name="load_swim_pending_to_ch",
    lane="swim",
    skip_on_empty=True,
)
