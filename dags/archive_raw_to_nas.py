from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


@dag(
    dag_id="archive_raw_to_nas",
    description="Daily off-peak tiering of aged, already-ClickHoused raw Parquet from Garage to the NAS cold archive (copy + verify, never deletes the Garage source)",
    start_date=pendulum.datetime(2026, 6, 24, tz="UTC"),
    schedule="0 19 * * *",  # ~04:00 JST, off-peak, clear of maintain_bronze_dedup at 18:30 (Airflow schedules are UTC)
    catchup=False,
    max_active_runs=1,
    # Maintenance task — self-skips on hosts without the NFS mount, so it runs from first boot without a manual unpause.
    is_paused_upon_creation=False,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
    },
    tags=["sancha1090", "storage", "maintenance"],
)
def archive_raw_to_nas():

    @task
    def archive_pending_to_nas() -> dict:
        # Raises only on a real copy/verify failure (a corrupt source or a write that fails verification);
        # an absent cold mount is a green skip so non-prod hosts don't red.
        from include.archive_to_nas import archive_pending

        out = archive_pending()
        if not out["ok"]:
            raise RuntimeError(f"archive_raw_to_nas: some objects failed copy/verify — {out}")
        return out

    archive_pending_to_nas()


archive_raw_to_nas()
