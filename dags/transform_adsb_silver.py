from __future__ import annotations

from airflow.sdk import dag

from include.adsb_assets import adsb_bronze_table
from include.dag_dbt import dbt_run_test
from include.dag_defaults import default_args


@dag(
    dag_id="transform_adsb_silver",
    description="Build silver ADS-B dims + fct (dbt-clickhouse) from the ClickHouse bronze.adsb_states table",
    schedule=[adsb_bronze_table],
    catchup=False,
    max_active_runs=1,
    default_args=default_args(),
    tags=["sancha1090", "silver", "adsb"],
)
def transform_adsb_silver():

    # +tag:adsb pulls in dim_aircraft_registry (the one cross-lane ancestor dim_aircraft depends on);
    # the P4 ADS-B aggregates are served by self-maintaining MVs (include/ch_incremental_mvs.py), not dbt models.
    dbt_run_test("--select +tag:adsb")


transform_adsb_silver()
