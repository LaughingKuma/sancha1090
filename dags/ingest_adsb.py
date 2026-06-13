from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pendulum

from airflow.sdk import dag, task

from include.adsb_assets import adsb_raw_landed


STALE_THRESHOLD = timedelta(hours=2)


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def summarize_results(results: list[Optional[dict]]) -> dict[str, int]:
    landed = sum(1 for r in results if r and r["ok"])
    failed = sum(1 for r in results if r and not r["ok"])
    adsb_landed = sum(1 for r in results if r and r["ok"] and r["stream"] == "adsb_state")
    beast_landed = sum(1 for r in results if r and r["ok"] and r["stream"] == "beast_raw")
    return {"landed": landed, "failed": failed,
            "adsb_landed": adsb_landed, "beast_landed": beast_landed}


def maybe_log_stale(results: list[Optional[dict]], now: datetime, logger: logging.Logger,
                    manifest_newest: Optional[datetime] = None) -> bool:
    """Surfaces a silent producer/push: if the newest known adsb_state close time is >2 h behind
    wall clock, log.error. Falls back to the manifest's newest when this run landed nothing —
    a silent producer has no current-run results yet is exactly what must alert. Returns staleness."""
    ends = [_parse_iso(r["rotation_end_ts"]) for r in results
            if r and r["ok"] and r["stream"] == "adsb_state"]
    newest = max(ends) if ends else manifest_newest
    if newest is None:
        return False
    if now - newest > STALE_THRESHOLD:
        logger.error("adsb ingest stale: newest adsb_state rotation_end_ts %s is >%s behind %s",
                     newest.isoformat(), STALE_THRESHOLD, now.isoformat())
        return True
    return False


@dag(
    dag_id="ingest_adsb",
    description="Discover landed ADS-B bronze bundles in Garage, validate, record to Postgres",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule="5 * * * *",
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "amit",
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": True,
        "max_retry_delay": timedelta(minutes=10),
    },
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
        summary = summarize_results(results)
        maybe_log_stale(results, now=datetime.now(timezone.utc),
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
