from __future__ import annotations

from airflow.sdk import dag

from include.assets import bronze_flights_table
from include.dag_dbt import dbt_run_test
from include.dag_defaults import default_args


@dag(
    dag_id="transform_flights",
    description="Build tag:flights dbt-clickhouse models from bronze flights/registry",
    schedule=[bronze_flights_table],
    catchup=False,
    max_active_runs=1,
    default_args=default_args(),
    tags=["sancha1090", "silver", "gold", "v5"],
)
def transform_flights():

    # Cross-lane seed deps (dim_hex_country/dim_airports/dim_airlines) are seeded once by clickhouse-marts-init.
    # The RW route publish moved to transform_marts (SP2), so this lane is a plain build -> test gate.
    dbt_run_test("--select tag:flights")


transform_flights()
