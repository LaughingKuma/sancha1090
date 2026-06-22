from __future__ import annotations

from datetime import timedelta
from typing import Any

import pendulum

from airflow.sdk import dag, task

from include.airports_jp import AIRPORTS_JP
from include.assets import raw_flights_landed


@dag(
    dag_id="ingest_flights",
    description="Pull arrival/departure flight summaries for the tracked JP airports",
    start_date=pendulum.datetime(2026, 6, 1, tz="UTC"),
    # 23:30 JST, after the day's traffic wraps; flights credits are an independent
    # bucket from /states (verified 2026-06-10), see tests/test_credit_budget.py.
    schedule="30 14 * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=10),
    },
    tags=["sancha1090", "bronze", "v5"],
)
def ingest_flights():

    @task(
        retries=3,
        retry_delay=timedelta(seconds=30),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=5),
    )
    def fetch_airport(airport: dict[str, Any], **context) -> dict[str, Any]:
        """The actual data never travels through XCom — only the URI."""

        import polars as pl
        from include.bronze_transforms import RAW_FLIGHTS_SCHEMA, flight_row
        from include.opensky_client import OpenSkyClient
        from include.s3_helpers import write_parquet
        from include import manifest

        client = OpenSkyClient.from_env()
        icao = airport["icao"]
        # Manual runs carry no data interval in Airflow 3; run_after is always set.
        end_dt = context.get("data_interval_end") or context["dag_run"].run_after
        end = int(end_dt.timestamp())

        # D-2 is the authoritative window — OpenSky's flight summaries only fully
        # populate ~48h after the fact (last-24h arrivals were near-empty at spike).
        # D-0 departures are complete same-day and give tooltips fresh narratives.
        windows = [
            ("arrival", "d2", end - 3 * 86400, end - 2 * 86400),
            ("departure", "d2", end - 3 * 86400, end - 2 * 86400),
            ("departure", "d0", end - 86400, end),
        ]

        rows: list[dict[str, Any]] = []
        for direction, window_kind, begin, until in windows:
            fetch = (
                client.get_flights_arrival
                if direction == "arrival"
                else client.get_flights_departure
            )
            for f in fetch(icao, begin, until):
                # first_seen is the partition key + dedup key; drop records OpenSky returns without it.
                if f.get("firstSeen") is None:
                    continue
                rows.append(flight_row(f, icao, direction, window_kind))

        if not rows:
            return {"airport": icao, "rows": 0, "uri": None}

        df = pl.DataFrame(rows, schema=RAW_FLIGHTS_SCHEMA).with_columns(
            pl.lit(end_dt.isoformat()).alias("ingested_at"),
        )

        key = (
            f"bronze/flights_raw/"
            f"dt={end_dt.strftime('%Y-%m-%d')}/"
            f"airport={icao}.parquet"
        )
        uri = write_parquet(df, key)

        first_seens = [r["first_seen"] for r in rows if r["first_seen"]]
        manifest.record_load(
            object_uri=uri,
            snapshot_min=min(first_seens) if first_seens else None,
            snapshot_max=max(first_seens) if first_seens else None,
            row_count=df.height,
        )

        return {"airport": icao, "rows": df.height, "uri": uri}

    @task(trigger_rule="all_done", outlets=[raw_flights_landed])
    def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
        """trigger_rule='all_done' runs this even on partial failure —
        we want the summary, not skipped."""
        results = list(results)

        total_rows = sum(r["rows"] for r in results if r is not None)
        with_data = sum(1 for r in results if r is not None and r.get("uri"))
        summary = {
            "total_rows": total_rows,
            "airports_with_data": with_data,
            "airports_attempted": len(results),
            "per_airport": results,
        }
        print(f"Flights ingestion summary: {summary}")
        return summary

    results = fetch_airport.expand(airport=AIRPORTS_JP)
    summarize(results)  # type: ignore[arg-type]


ingest_flights()
