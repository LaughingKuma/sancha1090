from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task

from include.adsb_assets import adsb_bronze_table, adsb_raw_landed


@dag(
    dag_id="tableize_adsb",
    description="Load newly-landed adsb_state Parquet into ClickHouse bronze.adsb_states (byte-mirror)",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=[adsb_raw_landed],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "bronze", "clickhouse", "adsb"],
)
def tableize_adsb():

    @task(outlets=[adsb_bronze_table])
    def load_adsb_to_clickhouse() -> dict:
        # CH bronze is the canonical landing target; independent ch_loaded_at pending set, byte-mirror. Raise on
        # a load failure so the task reds and the bronze asset (which triggers transform_adsb_silver) isn't
        # emitted stale — the per-batch loader drains what it can before reporting ok=False.
        from include.clickhouse import load_adsb_pending_to_ch

        result = load_adsb_pending_to_ch()
        if not result.get("ok"):
            raise RuntimeError(f"CH adsb bronze load failed: {result}")
        return result

    load_adsb_to_clickhouse()


tableize_adsb()
