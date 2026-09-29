from __future__ import annotations

from airflow.sdk import dag

from include.assets import bronze_adsbx_db_table
from include.dag_dbt import ADSB_SILVER_POOL, dbt_run_test
from include.dag_defaults import default_args


@dag(
    dag_id="refresh_fct_adsb_state_dims",
    description="Weekly full rebuild of fct_adsb_state so every day carries the current airframe dims",
    schedule=[bronze_adsbx_db_table],
    catchup=False,
    max_active_runs=1,
    default_args=default_args(),
    tags=["sancha1090", "silver", "adsb"],
)
def refresh_fct_adsb_state_dims():

    # Older fct days keep the dims of their last build; this re-applies the week's ADSBx fill to all history.
    dbt_run_test(
        "--select +fct_adsb_state --exclude int_adsb_callsign_from_opensky",
        run_flags="--full-refresh",
        pool=ADSB_SILVER_POOL,
    )


refresh_fct_adsb_state_dims()
