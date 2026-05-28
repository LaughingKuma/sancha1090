# sancha1090: a local-first data platform

Local Airflow 3 platform that pulls live aircraft state from the
[OpenSky Network](https://opensky-network.org), shapes it through
bronze/silver/gold layers in an Iceberg lakehouse (Garage S3 + Polaris
catalog + Trino query engine), all in Docker Compose. No cloud accounts.

> Post-v3.0 (sancha1090 rebrand), this README is intentionally minimal —
> the previous screenshots and narrative were stale. A proper rewrite is
> planned alongside the v3.x edge-ingest work.

## Architecture

```
OpenSky REST API  (live, global aircraft state)
                │
                ▼
   ┌──────────────────────────────────┐
   │  ingest_states (Airflow)         │
   │  every 12 min, dynamic mapping   │
   │  over 8 geographic regions       │
   └─────────────┬────────────────────┘
                 │  asset event: raw_states_landed
                 ▼
       Garage bronze/states_raw/dt=.../hr=.../min=.../region={X}.parquet
       Postgres public.ingestion_manifest (one row per landed file)
                 │
                 ▼
   ┌──────────────────────────────────┐
   │  tableize_states (Airflow)       │
   │  drain manifest → PyIceberg      │
   │  append (single canonical writer)│
   └─────────────┬────────────────────┘
                 │  asset event: bronze_states_table
                 ▼
       Iceberg bronze.opensky_states (Polaris-backed REST catalog)
                 │
                 ▼
   ┌──────────────────────────────────┐
   │  transform_marts (Airflow)       │
   │  dbt-trino: silver + gold marts  │
   └──────────────────────────────────┘
                 │
                 ▼
       iceberg.silver.{stg_states, fact_state_snapshots}
       iceberg.gold.{agg_country_traffic, agg_hourly_traffic, anomalies}
```

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
