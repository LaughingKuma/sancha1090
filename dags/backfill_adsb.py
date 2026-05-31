from __future__ import annotations

import uuid

import pendulum

from airflow.sdk import dag, task

from include.adsb_assets import adsb_bronze_table


# Earliest live bronze hour (UTC) — capture_v2's parallel run started at 2026-05-28T03; at/after
# is already in the lake, so backfill stops strictly before it.
DEFAULT_END_BEFORE_HOUR = "2026-05-28T03"
BACKFILL_HOST = "sangenjaya-edge"
# One logical backfill "process" → a stable synthetic UUID for the manifest's NOT NULL column.
BACKFILL_PROCESS_UUID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "sancha1090.adsb.backfill"))


def backfill_core(*, lines, catalog, engine, fs, bucket, end_before_hour,
                  host=BACKFILL_HOST, uri_prefix="s3://", provenance="backfill") -> dict:
    """Convert legacy JSONL → hourly bronze Parquet (zero overlap with live), register via the same
    add_files path as live. No Garage manifest sidecar is written, so ingest_adsb never re-discovers
    these — the Postgres row (provenance='backfill') is the system of record."""
    from include import adsb_backfill as bf
    from include import adsb_iceberg as ai
    from include import adsb_manifest as am

    table = ai.ensure_adsb_namespace_and_table(catalog)

    def write_hour(hour: str, rows: list[dict]) -> dict:
        day = hour[:10]
        filename = f"{host}_adsb_state_{hour}_bkfl.parquet"
        key = f"{bucket}/bronze/adsb_state/dt={day}/{filename}"
        fs.create_dir(key.rsplit("/", 1)[0], recursive=True)
        bf.write_hour_parquet(rows, key, filesystem=fs)
        data_uri = f"{uri_prefix}{key}"
        am.record_bundle(
            engine=engine, filename=filename, process_uuid=BACKFILL_PROCESS_UUID,
            stream="adsb_state", hostname=host,
            rotation_start_ts=f"{hour}:00:00Z", rotation_end_ts=f"{hour}:59:59Z",
            complete=True, schema_version=bf.SCHEMA_VERSION, s3_uri=data_uri,
            manifest_s3_uri=data_uri, row_count=len(rows), provenance=provenance,
        )
        return {"s3_uri": data_uri, "filename": filename}

    summaries = bf.backfill_records(bf.iter_records(lines), end_before_hour, write_hour)
    if not summaries:
        return {"hours": 0, "rows": 0, "committed": 0}

    snapshot_by_uri = ai.add_files_to_adsb(table, [s["s3_uri"] for s in summaries])
    snapshot_by_filename = {s["filename"]: snapshot_by_uri[s["s3_uri"]] for s in summaries}
    committed = am.mark_iceberg_committed(snapshot_by_filename, engine)
    return {"hours": len(summaries), "rows": sum(s["rows"] for s in summaries), "committed": committed}


# The legacy Beast stream is one frozen multi-day blob (2026-05-23T10:27Z → ~2026-05-25T06:59Z).
LEGACY_BEAST_START_TS = "2026-05-23T10:27:33Z"
LEGACY_BEAST_END_TS = "2026-05-25T06:59:59Z"


def _copy_object(fs, src_key: str, dst_key: str, chunk: int = 8 << 20) -> int:
    # compression=None forces a raw byte copy — pyarrow otherwise auto-(de)compresses .gz by
    # extension, which would alter the bytes and desync the .beastidx offsets.
    fs.create_dir(dst_key.rsplit("/", 1)[0], recursive=True)
    size = 0
    with fs.open_input_stream(src_key, compression=None) as r, \
            fs.open_output_stream(dst_key, compression=None) as w:
        while True:
            b = r.read(chunk)
            if not b:
                break
            w.write(b)
            size += len(b)
    return size


def backfill_beast_core(*, engine, fs, bucket, source_beast_key, source_idx_key,
                        day, host=BACKFILL_HOST, uri_prefix="s3://", provenance="backfill") -> dict:
    """Beast is tracked in Postgres only (never Iceberg) — so backfill just mirrors the frozen
    legacy .beast.gz + .beastidx.gz into beast_raw/ and records one manifest row. frame_count /
    beast_uncompressed_size are unknown for the legacy stream (no manifest existed) → left NULL."""
    from include import adsb_manifest as am

    stem = f"{host}_beast_raw_legacy_bkfl"
    dst_beast = f"{bucket}/bronze/beast_raw/dt={day}/{stem}.beast.gz"
    dst_idx = f"{bucket}/bronze/beast_raw/dt={day}/{stem}.beastidx.gz"
    byte_count = _copy_object(fs, source_beast_key, dst_beast)
    _copy_object(fs, source_idx_key, dst_idx)

    filename = f"{stem}.beast.gz"
    data_uri = f"{uri_prefix}{dst_beast}"
    am.record_bundle(
        engine=engine, filename=filename, process_uuid=BACKFILL_PROCESS_UUID,
        stream="beast_raw", hostname=host,
        rotation_start_ts=LEGACY_BEAST_START_TS, rotation_end_ts=LEGACY_BEAST_END_TS,
        complete=True, schema_version=1, s3_uri=data_uri, manifest_s3_uri=data_uri,
        byte_count=byte_count, provenance=provenance,
    )
    return {"filename": filename, "byte_count": byte_count, "s3_uri": data_uri}


@dag(
    dag_id="backfill_adsb",
    description="One-shot: legacy pre-cutover JSONL → bronze.adsb_states (provenance=backfill)",
    start_date=pendulum.datetime(2026, 5, 1, tz="UTC"),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    params={
        "source_uri": "s3://sancha1090/backups/pre-cutover-jsonl/capture_20260523_192733.jsonl.gz",
        "end_before_hour": DEFAULT_END_BEFORE_HOUR,
        "beast_source_uri": "s3://sancha1090/backups/pre-cutover-jsonl/capture_20260523_192733.beast.gz",
        "beastidx_source_uri": "s3://sancha1090/backups/pre-cutover-jsonl/capture_20260523_192733.beastidx.gz",
        "include_beast": True,
    },
    default_args={"owner": "amit", "retries": 0},
    tags=["sancha1090", "bronze", "adsb", "backfill", "manual"],
)
def backfill_adsb():

    @task(outlets=[adsb_bronze_table])
    def run_backfill(**context) -> dict:
        import gzip
        import os

        from pyarrow.fs import S3FileSystem

        from include import adsb_iceberg as ai
        from include import adsb_manifest as am
        from include.s3_helpers import get_bucket

        p = context["params"]
        source_uri = p["source_uri"]
        if not source_uri.startswith("s3://"):
            raise ValueError(f"source_uri must be an s3:// Garage URI: {source_uri}")

        fs = S3FileSystem(
            endpoint_override=f"http://{os.environ['S3_ENDPOINT']}",
            access_key=os.environ["S3_ACCESS_KEY"], secret_key=os.environ["S3_SECRET_KEY"],
            region="garage", scheme="http",
        )
        # compression=None: hand gzip the raw .gz bytes; pyarrow would otherwise auto-inflate by
        # extension and gzip.open would then double-decompress.
        with fs.open_input_stream(source_uri[len("s3://"):], compression=None) as raw:
            with gzip.open(raw, "rt") as lines:
                return backfill_core(
                    lines=lines, catalog=ai.get_catalog(), engine=am._engine(),
                    fs=fs, bucket=get_bucket(), end_before_hour=p["end_before_hour"],
                )

    @task
    def run_beast_backfill(**context) -> dict:
        import os

        from pyarrow.fs import S3FileSystem

        from include import adsb_manifest as am
        from include.s3_helpers import get_bucket

        p = context["params"]
        if not p.get("include_beast"):
            return {"skipped": True}
        for k in ("beast_source_uri", "beastidx_source_uri"):
            if not p[k].startswith("s3://"):
                raise ValueError(f"{k} must be an s3:// Garage URI: {p[k]}")

        fs = S3FileSystem(
            endpoint_override=f"http://{os.environ['S3_ENDPOINT']}",
            access_key=os.environ["S3_ACCESS_KEY"], secret_key=os.environ["S3_SECRET_KEY"],
            region="garage", scheme="http",
        )
        return backfill_beast_core(
            engine=am._engine(), fs=fs, bucket=get_bucket(),
            source_beast_key=p["beast_source_uri"][len("s3://"):],
            source_idx_key=p["beastidx_source_uri"][len("s3://"):],
            day=LEGACY_BEAST_START_TS[:10],
        )

    run_backfill()
    run_beast_backfill()


backfill_adsb()
