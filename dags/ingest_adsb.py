from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pendulum

from airflow.sdk import dag, task

from include.adsb_assets import adsb_raw_landed
from include.dag_defaults import default_args


@dag(
    dag_id="ingest_adsb",
    description="Discover landed ADS-B bronze bundles in Garage, validate, record to Postgres",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    # :10, not :05 — the edge's hourly push lands :01-:05; :05 pickup lost a 5s race once (2026-07-21).
    schedule="10 * * * *",
    catchup=False,
    max_active_runs=1,
    default_args=default_args(retries=2, backoff=True),
    tags=["sancha1090", "bronze", "adsb"],
)
def ingest_adsb():

    @task(retries=2, retry_delay=timedelta(seconds=30), retry_exponential_backoff=True,
          max_retry_delay=timedelta(minutes=10))
    def list_remote_bundles() -> list[dict[str, Any]]:
        from dataclasses import asdict
        from include import adsb_discovery as ad
        from include.s3_helpers import get_s3fs, get_bucket

        fs = get_s3fs()
        return [asdict(b) for b in ad.list_remote_bundles(fs, get_bucket())]

    @task
    def select_new(bundles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from include import adsb_manifest as am

        if not bundles:
            return []
        ingested = am.already_ingested([b["filename"] for b in bundles])
        return [b for b in bundles if b["filename"] not in ingested]

    @task(retries=1, retry_delay=timedelta(minutes=1), max_active_tis_per_dag=6)
    def validate_and_record(bundle: dict[str, Any]) -> dict[str, Any]:
        from include import adsb_discovery as ad
        from include import adsb_manifest as am
        from include.s3_helpers import garage_pyarrow_fs

        b = ad.RemoteManifestBundle(**bundle)

        num_rows = None
        if b.stream == "adsb_state":
            num_rows = ad.read_parquet_num_rows(garage_pyarrow_fs(), b.data_s3_uri[len("s3://"):])
        ad.validate_bundle(b, num_rows)  # raises on rowcount mismatch → task red, retried next run

        m = b.manifest
        am.record_bundle(
            filename=b.filename, process_uuid=m["process_uuid"], stream=b.stream,
            hostname=m["hostname"], rotation_start_ts=m["rotation_start_ts"],
            rotation_end_ts=m["rotation_end_ts"], complete=m["complete"],
            schema_version=m["schema_version"], s3_uri=b.data_s3_uri,
            manifest_s3_uri=b.manifest_s3_uri, row_count=m.get("row_count"),
            frame_count=m.get("frame_count"), byte_count=m.get("byte_count"),
            beast_uncompressed_size=m.get("beast_uncompressed_size"),
        )
        return {"filename": b.filename, "stream": b.stream, "ok": True,
                "rotation_end_ts": m["rotation_end_ts"]}

    @task(trigger_rule="all_done", outlets=[adsb_raw_landed])
    def summarize_emit_asset(results: list[dict[str, Any]]) -> dict[str, Any]:
        """all_done so we summarize even on partial failure. Emits adsb_raw_landed only when
        at least one adsb_state row landed (skip → no Asset event → tableize_adsb not triggered)."""
        from airflow.exceptions import AirflowSkipException
        from include import adsb_manifest as am

        results = list(results)
        summary = am.summarize_results(results)
        am.maybe_log_stale(results, now=datetime.now(timezone.utc),
                           logger=logging.getLogger("ingest_adsb"),
                           manifest_newest=am.newest_adsb_rotation_end())
        print(f"ingest_adsb summary: {summary}")

        if summary["adsb_landed"] == 0:
            raise AirflowSkipException("no adsb_state rows landed this run; not emitting asset")
        return summary

    bundles = list_remote_bundles()
    new = select_new(bundles)
    results = validate_and_record.expand(bundle=new)
    summarize_emit_asset(results)  # type: ignore[arg-type]


ingest_adsb()
