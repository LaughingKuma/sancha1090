"""ingest_states: pull aircraft state snapshots from OpenSky every 12 minutes.

Stage 9: dynamic task mapping over geographic regions. Since v5.0 the region
list is a single Japan+ocean box (OpenSky is the antenna's wide-context layer,
not a global sweep — see include/regions.py), but the mapping is kept so
sub-regions can return without a DAG rewrite.

Design notes:
- One Japan+ocean bbox; the antenna covers the local sky, OpenSky covers the
  rest of Japan and the surrounding ocean (beyond the receiver's horizon).
- The summarize task uses trigger_rule="all_done" so it runs even if the
  region fails — we want the partial-success summary, not "skipped".
- The bbox is >400 sq deg, the top tier of OpenSky's pricing, so the call
  costs 4 credits. 1 region x 4 credits x 120 runs/day = 480 credits/day,
  ~6% of the 8,000/day active-feeder quota — deliberate headroom to raise
  cadence later. See tests/test_credit_budget.py for the enforced version.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pendulum

from airflow.sdk import dag, task

from include.assets import raw_states_landed


@dag(
    dag_id="ingest_states",
    description="Pull state vectors per region every 12 minutes",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule="*/12 * * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=10),
    },
    tags=["sancha1090", "bronze"],
)
def ingest_states():

    @task
    def list_regions() -> list[dict[str, Any]]:
        from include.regions import REGIONS

        return REGIONS

    @task(
        retries=3,
        retry_delay=timedelta(seconds=30),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=5),
    )
    def fetch_region(region: dict[str, Any], **context) -> dict[str, Any]:
        """The actual data never travels through XCom — only the URI."""

        import polars as pl
        from include.opensky_client import OpenSkyClient
        from include.s3_helpers import write_parquet
        from include import manifest

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

        # Include region in the key so mapped regions per minute don't collide.
        logical = context["logical_date"]
        key = (
            f"bronze/states_raw/"
            f"dt={logical.strftime('%Y-%m-%d')}/"
            f"hr={logical.strftime('%H')}/"
            f"min={logical.strftime('%M')}/"
            f"region={region['name']}.parquet"
        )

        uri = write_parquet(df, key)

        snapshot_time = payload["time"]
        manifest.record_load(
            object_uri=uri,
            snapshot_min=snapshot_time,
            snapshot_max=snapshot_time,
            row_count=df.height,
        )

        return {"region": region["name"], "rows": df.height, "uri": uri, "snapshot_time": snapshot_time}

    @task(trigger_rule="all_done", outlets=[raw_states_landed])
    def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
        """trigger_rule='all_done' runs this even on partial failure —
        we want the summary, not skipped."""
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

    regions = list_regions()
    results = fetch_region.expand(region=regions)
    summarize(results) # type: ignore[arg-type] 


ingest_states()