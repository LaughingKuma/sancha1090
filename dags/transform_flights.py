from __future__ import annotations

from datetime import timedelta

from airflow.sdk import dag, task
from airflow.providers.standard.operators.bash import BashOperator

from include.assets import bronze_flights_table


@dag(
    dag_id="transform_flights",
    description="Build tag:flights dbt models from bronze flights/registry, then push routes to RisingWave",
    schedule=[bronze_flights_table],
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["sancha1090", "silver", "gold", "v5"],
)
def transform_flights():

    @task
    def ensure_bronze_tables() -> None:
        # Blank-warehouse safety: the dbt sources must exist (even empty) regardless of
        # whether ingest_aircraft_db/ingest_flights ran yet; idempotent in steady state.
        from include import flights_iceberg as fib

        fib.ensure_flights_table()
        fib.ensure_aircraft_db_table()

    dbt_deps = BashOperator(
        task_id="dbt_deps",
        bash_command=(
            "cd /opt/airflow/dbt/sancha1090 && "
            "dbt deps --profiles-dir . --no-use-colors"
        ),
    )

    # The lane's cross-lane seed deps (registry -> dim_hex_country; fact_flights ->
    # dim_airports; agg_operator_traffic -> dim_airlines), so a blank warehouse converges
    # no matter which transform lane runs first. fact_flights' other cross-lane ref,
    # fact_state_snapshots, self-heals via transform_marts on the 12-min states tick.
    dbt_seed = BashOperator(
        task_id="dbt_seed",
        bash_command=(
            "cd /opt/airflow/dbt/sancha1090 && "
            "dbt seed --profiles-dir . --target trino --no-use-colors "
            "--select dim_hex_country dim_airports dim_airlines"
        ),
    )

    dbt_run = BashOperator(
        task_id="dbt_run",
        bash_command=(
            "cd /opt/airflow/dbt/sancha1090 && "
            "dbt run --profiles-dir . --target trino --no-use-colors --select tag:flights"
        ),
    )

    dbt_test = BashOperator(
        task_id="dbt_test",
        bash_command=(
            "cd /opt/airflow/dbt/sancha1090 && "
            "dbt test --profiles-dir . --target trino --no-use-colors --select tag:flights"
        ),
    )

    @task
    def push_flight_routes() -> int:
        # Same Trino→RisingWave versioned-publish path as the range outline.
        from include.flight_routes import refresh_flight_routes

        return refresh_flight_routes()

    ensure_bronze_tables() >> dbt_deps >> dbt_seed >> dbt_run >> dbt_test >> push_flight_routes()


transform_flights()
