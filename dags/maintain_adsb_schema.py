from __future__ import annotations

import pendulum

from airflow.sdk import dag, task

from include.dag_defaults import default_args


@dag(
    dag_id="maintain_adsb_schema",
    description="Weekly scan of bronze _raw_json for new untyped readsb fields (schema-drift alert)",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule="35 4 * * 1",  # Monday 04:35 UTC, off-peak weekly slot
    catchup=False,
    max_active_runs=1,
    default_args=default_args(delay_min=5),
    tags=["sancha1090", "bronze", "adsb", "maintenance"],
)
def maintain_adsb_schema():

    @task
    def scan_drift() -> dict:
        from include.adsb_drift import DEFAULT_LIMIT_FILES, DEFAULT_SAMPLE_ROWS, scan_core
        from include.s3_helpers import garage_pyarrow_fs, get_bucket

        return scan_core(garage_pyarrow_fs(), f"{get_bucket()}/bronze/adsb_state",
                         limit_files=DEFAULT_LIMIT_FILES, sample_rows=DEFAULT_SAMPLE_ROWS)

    scan_drift()


maintain_adsb_schema()
