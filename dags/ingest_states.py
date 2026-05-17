"""ingest_states: pull aircraft state snapshots from OpenSky every 10 minutes.

Stage 9: dynamic task mapping over geographic regions. Each region runs
as its own parallel task instance, with independent retries and logs.

Design notes:
- 8 regions cover the populated airspace. Sparse polar areas omitted.
- The summarize task uses trigger_rule="all_done" so it runs even if
  some regions fail — we want the partial-success summary, not "skipped".
- Bounding boxes are small enough that each call costs ~1 OpenSky credit.
  8 calls every 10 minutes = 48 calls/hour = 1152/day. Well under the
  4000/day quota for authenticated accounts.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pendulum

from airflow.sdk import dag, task, Asset

from include.assets import bronze_states


@dag(
    dag_id="ingest_states",
    description="Pull state vectors per region every 10 minutes",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule="*/10 * * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=10),
    },
    tags=["opensky", "bronze", "stage-9"],
)
def ingest_states():

    @task
    def list_regions() -> list[dict[str, Any]]:
        """Return the region list. This is the 'expansion source' — its
        output becomes the iterable that fetch_region maps over."""
        from include.regions import REGIONS
        
        return REGIONS

    @task(
        retries=3,
        retry_delay=timedelta(seconds=30),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=5),
    )
    def fetch_region(region: dict[str, Any], **context) -> dict[str, Any]:
        """Fetch state vectors for one region's bbox, write parquet.

        Returns a small summary dict for the summarizer. The actual data
        never travels through XCom — only the URI of where it landed.
        """

        import polars as pl
        from include.opensky_client import OpenSkyClient
        from include.minio_helpers import write_parquet
       
        client = OpenSkyClient.from_env()

        bbox = (
            float(region["lamin"]),
            float(region["lomin"]),
            float(region["lamax"]),
            float(region["lomax"]),
        )

        payload = client.get_states(bbox=bbox)

        states = payload.get("states") or []
        if not states:
            # Region has no aircraft right now (unusual but possible).
            # Return a successful summary so the summarizer sees a clean count.
            return {"region": region["name"], "rows": 0, "uri": None}
        
        df = pl.DataFrame(
            states,
            schema=[
                "icao24", "callsign", "origin_country", "time_position", "last_contact",
                "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
                "true_track", "vertical_rate", "sensors", "geo_altitude", "squawk",
                "spi", "position_source",
            ],
            orient="row",
        )

        df = df.drop("sensors")

        df = df.with_columns(
            pl.lit(payload["time"]).alias("snapshot_time"),
            pl.lit(region["name"]).alias("region"),
            pl.lit(context["logical_date"].isoformat()).alias("ingested_at"),
        )

        # Build the partitioned key. Note we include region in the key so
        # 8 regions per minute don't collide.
        logical = context["logical_date"]
        key = (
            f"bronze/states/"
            f"dt={logical.strftime('%Y-%m-%d')}/"
            f"hr={logical.strftime('%H')}/"
            f"min={logical.strftime('%M')}/"
            f"region={region['name']}.parquet"
        )

        uri = write_parquet(df, key)
        return {"region": region["name"], "rows": df.height, "uri": uri, "snapshot_time": payload["time"]}

    @task(trigger_rule="all_done", outlets=[bronze_states])
    def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
        """Aggregate per-region results into a single summary.

        trigger_rule='all_done' means this runs whether the mapped tasks
        succeeded, failed, or got skipped. We want the summary even on
        partial failure so we can see what happened.
        """
        results = list(results)

        total_rows = sum(r["rows"] for r in results if r is not None)
        with_data = sum(1 for r in results if r is not None and r.get("uri"))
        summary = {
            "total_rows": total_rows,
            "regions_with_data": with_data,
            "regions_attempted": len(results),
            "per_region": results,
        }
        print(f"Ingestion summary: {summary}")
        return summary

    # The actual graph.
    regions = list_regions()
    results = fetch_region.expand(region=regions)
    summarize(results) # type: ignore[arg-type] 


ingest_states()