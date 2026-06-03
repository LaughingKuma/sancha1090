# sancha1090: a local-first data platform

Local Airflow 3 platform that fuses **two live aircraft-state feeds** — the global
[OpenSky Network](https://opensky-network.org) REST API and a local **rooftop ADS-B
antenna** — and shapes them through bronze/silver/gold layers in an Iceberg lakehouse
(Garage S3 + Polaris catalog + Trino query engine), all in Docker Compose. No cloud
accounts.

> **Data model:** the full column-level schema, lineage, and entity map for every
> bronze/silver/gold table live in **[`docs/datalake.md`](docs/datalake.md)**.

## Architecture

Two independent feeds land in Iceberg bronze and stay on separate refresh tracks
(partitioned by dbt tag so they never race), fusing only in `gold.fct_flight_legs`:

<p align="center">
  <img src="docs/architecture.svg" alt="sancha1090 dual-feed medallion lakehouse: OpenSky global + rooftop ADS-B feeds flow through bronze → silver → gold to Trino + Superset" width="520">
</p>

Provenance for both feeds lives in Postgres (`public.ingestion_manifest` for global,
`public.adsb_ingestion_manifest` for rooftop) — one row per landed file. See
[`docs/datalake.md`](docs/datalake.md) for the full lineage, entity map, and per-table schema.

## Quickstart

```bash
git clone <this-repo>
cd sancha1090
cp .env.example .env
# Fill the blank secrets in .env (each has a "Generate with:" hint).
docker compose up -d

# Once healthy:
docker compose exec airflow-scheduler bash -c "cd /opt/airflow && pytest tests/ -v"
```

First boot: ~3–5 min for image builds + initial Postgres migrations.
Airflow UI at <http://localhost:38080> (admin / admin).

Trigger `ingest_states` in Airflow to start populating; `tableize_states`
and `transform_marts` cascade automatically via asset events.

## Tech stack

- Apache Airflow 3.2 — TaskFlow, dynamic task mapping, asset chains
- Apache Iceberg via PyIceberg + Apache Polaris REST catalog
- dbt-trino for silver/gold marts
- Trino as the query engine over Iceberg
- Garage as a local S3-compatible object store
- polars + pyarrow for in-memory transforms
- Three Postgres instances (Airflow metadata, Polaris+manifest, Superset metadata)
- Docker Compose for the whole stack

## Storage layout

| Service              | Role                                              |
|----------------------|---------------------------------------------------|
| `postgres-airflow`   | Airflow metadata: DAG runs, XCom, etc.            |
| `postgres-analytics` | Polaris metastore + `public.ingestion_manifest`   |
| `postgres-superset`  | Superset metadata                                 |
| `garage`             | Raw parquet + Iceberg warehouse (S3-compatible)   |

The rule: don't mix orchestration metadata with analytical data. A
runaway query that locks tables shouldn't be able to take down the
scheduler. Each Postgres has its own user, volume, and backup profile.

## Project layout

```
sancha1090/
├── docker-compose.yml               # Full stack
├── docker-compose.override.yml      # Host port bindings (loopback only)
├── docker-compose.local.yml         # Host-specific overrides (gitignored)
├── .env.example                     # Secrets template
├── dags/                            # Thin Airflow DAGs
├── include/                         # Logic imported by DAGs
├── dbt/sancha1090/                  # dbt project (silver + gold marts)
├── docs/                            # Data-model reference (datalake.md)
├── scripts/                         # Operational helpers
└── tests/                           # pytest: DAG integrity + credit budget
```

## Tests

```bash
docker compose exec airflow-scheduler bash -c "cd /opt/airflow && pytest tests/ -v"
```

`tests/test_dag_integrity.py` parses every DAG and asserts the expected
schedule and task set. `tests/test_credit_budget.py` computes the daily
OpenSky credit cost from the live region config + ingest schedule and
asserts it stays under the 4,000/day quota.

## License

MIT
