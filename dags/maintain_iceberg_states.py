from __future__ import annotations

from datetime import timedelta

import pendulum

from airflow.sdk import dag, task


SNAPSHOT_RETENTION = timedelta(days=7)


@dag(
    dag_id="maintain_iceberg_states",
    description="Daily expiry of Iceberg snapshots older than the retention window",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule="30 3 * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["opensky", "iceberg", "maintenance", "stage-12"],
)
def maintain_iceberg_states():

    @task
    def expire_snapshots() -> dict:
        from datetime import datetime, timezone
        from include import iceberg as ib

        catalog = ib.get_catalog()
        table = catalog.load_table(ib.QUALIFIED)
        before = len(table.snapshots())
        threshold = datetime.now(timezone.utc) - SNAPSHOT_RETENTION
        table.maintenance.expire_snapshots().older_than(threshold).commit()
        table.refresh()
        after = len(table.snapshots())
        print(f"snapshots before={before} after={after} threshold={threshold.isoformat()}")
        return {"before": before, "after": after, "expired": before - after}

    expire_snapshots()


maintain_iceberg_states()
