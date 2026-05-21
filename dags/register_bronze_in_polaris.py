from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="register_bronze_in_polaris",
    description="One-shot: register the existing bronze.opensky_states in Polaris (v2.1)",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
    },
    tags=["opensky", "polaris", "v2", "manual"],
)
def register_bronze_in_polaris():

    @task
    def register() -> dict:
        from include import iceberg as ib
        from include import iceberg_rest as rest

        sql_table = ib.get_catalog().load_table(ib.QUALIFIED)
        metadata_location = sql_table.metadata_location

        token = rest.polaris_token()
        rest.ensure_bronze_namespace(token)
        result = rest.register_bronze_table(metadata_location, token)

        sql_snap = sql_table.current_snapshot().snapshot_id
        pol_snap = result["snapshot_id"]
        if sql_snap != pol_snap:
            raise RuntimeError(
                f"snapshot mismatch after register: sql={sql_snap} polaris={pol_snap}"
            )

        return {
            "status": result["status"],
            "metadata_location": metadata_location,
            "snapshot_id": pol_snap,
        }

    register()


register_bronze_in_polaris()
