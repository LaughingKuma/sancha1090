# Engine history

This file is history. It is frozen at the v6.0 ClickHouse migration and is not extended —
current-state facts live in `README.md` and `docs/datalake.md`, and each release since is
described in its own GitHub release notes.

## The Iceberg lakehouse (v1 to v5.12)

sancha1090 began as a local-first medallion lakehouse: the rooftop antenna plus the OpenSky
network feeding bronze → silver → gold Iceberg tables on Garage (S3), a Polaris REST
catalog, and Trino, transformed by dbt-on-Trino and served to Superset. It was deliberately
the full open-table-format stack, with schema evolution, time travel, zero-copy `add_files`
ingest, and a catalog service, and it ran the whole thing on one box. Fifty-four releases
evolved it from a world sweep to a focused Japan feed with a live map, flight legs, and
airline analytics. That history is in the private development repository; the public
showcase starts at v6.0, after the migration below.

## Why it changed: right-sizing to the workload (v6.0)

ADS-B is append-heavy, time-ordered telemetry, which is the canonical columnar-OLAP
workload. As the data grew toward hundreds of millions of rows, the
distributed-query-engine and catalog stack was paying its full operational cost (JVM heap
tuning, worker OOM from view re-expansion, OPTIMIZE-vs-rebuild races, a separate metastore)
for none of its multi-engine or petabyte benefit on a single host. v6.0 replaced the Iceberg
+ Polaris + Trino batch warehouse with ClickHouse, keeping Postgres for manifests and
metadata and RisingWave for the live hot path. That left one engine instead of three
services, aggregates that maintain themselves, and an entire class of operational gotchas
deleted. The migration landed as eight reviewed, parity-checked phases.

## Benchmarks: why ClickHouse

ClickHouse was re-measured on the production box at v6.0 (warm, server-side, against the
live ~21.6 M-row `bronze.adsb_states`). The Trino + Iceberg column is the pre-migration
2026-06-19 spike baseline (~19.2 M rows); the lakehouse was retired at v6.0, so those are
the last measured figures, not re-runnable ones. Answers are identical across engines (the
window count is 28 aircraft, `max(r_dst)` is 166.453 nm), so the queries are equivalent.
Speedups are approximate, since the row counts differ.

| Query | Trino + Iceberg (spike) | ClickHouse (re-measured) | Speedup |
|-------|------------------------:|-------------------------:|--------:|
| Point-in-time aircraft count (2-min window) | ~5.1 s | 3 ms | ~1700× |
| Max receiver range (`max(r_dst)`) | ~5.0 s | 8 ms | ~600× |
| Airline traffic rollup (full scan + regex + `uniqExact`) | ~5.0 s | 155 ms | ~30× |
| Day-of-week / time-of-day scan | ~4.8 s | 14 ms | ~340× |

The window query prunes via the `capture_ts` sort key, reading only the 2-minute window out
of 21.6 M rows, where Trino full-scanned the unpartitioned Iceberg table at a flat ~5 s. At
a 10× synthetic 192 M rows the windowed query stayed flat (27 ms, spike projection) while a
full scan grows linearly.

The trade-offs:

- Ingest is no longer zero-copy. Iceberg `add_files` registered edge Parquet for free;
  ClickHouse physically materializes it. A full 19.2 M-row load takes 12 s, with about 2 min
  projected at 200 M.
- Naive storage was bigger, not smaller (3.96 GiB against 1.5 GB of Parquet at v6.0, 39% of
  it a verbatim raw-JSON column). This one got engineered away rather than accepted: v6.3
  eliminated the raw-JSON column, baking its one useful field into a real column at load, and
  added per-column ZSTD/T64 codecs; v6.4 put a verified cold copy of the raw landing zone on
  the NAS.
- Eventual-merge reads. Self-maintaining aggregates need `…Merge()` or `FINAL`, a real
  footgun the mart layer has to respect.
- Mart maintenance got cheaper. Cheap aggregates became incremental views that update on
  insert, which retired the scheduled rebuild along with the OOM and OPTIMIZE-race failure
  modes.

The hardest mart to port was flight-leg sessionization, an ordered cross-row window that
cannot be incremental. It came over with exact parity (143,605 legs, identical boundaries) at
116 ms, spill-safe under a tight memory cap.
