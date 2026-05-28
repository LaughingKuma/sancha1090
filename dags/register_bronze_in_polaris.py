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
    tags=["sancha1090", "polaris", "v2", "manual"],
)
def register_bronze_in_polaris():

    @task
    def register() -> dict:
        from include import iceberg as ib
        from include import iceberg_rest as rest

        sql_table = ib.get_catalog().load_table(ib.QUALIFIED)
        sql_meta = sql_table.metadata_location
        sql_snap = sql_table.current_snapshot().snapshot_id

        token = rest.polaris_token()
        rest.ensure_bronze_namespace(token)

        current = rest.load_polaris_table(token)
        if current is None:
            action = "registered"
            pol_snap = rest.register_bronze_table(sql_meta, token)
        elif current["metadata-location"] == sql_meta:
            action = "noop"
            pol_snap = current["metadata"]["current-snapshot-id"]
        else:
            # Polaris pointer is stale (e.g. from the spike or after tableize_states
            # advanced SqlCatalog). v2.3's sync_polaris_pointer uses this same path
            # to keep them in step continuously.
            action = "repointed"
            rest.drop_bronze_table(token)
            pol_snap = rest.register_bronze_table(sql_meta, token)

        if pol_snap != sql_snap:
            raise RuntimeError(
                f"snapshot mismatch after {action}: sql={sql_snap} polaris={pol_snap}"
            )

        return {
            "action": action,
            "metadata_location": sql_meta,
            "snapshot_id": pol_snap,
        }

    register()


register_bronze_in_polaris()
