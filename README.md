# OpenSky Live: a local-first data platform

A reproducible, fully-orchestrated data pipeline that ingests live aircraft
state vectors from the [OpenSky Network](https://opensky-network.org), shapes
them through bronze/silver/gold layers, and serves analytics through an
interactive Superset dashboard. Everything runs locally via Docker Compose
with no cloud dependencies.

![OpenSky Live Dashboard](docs/dashboard.png)

## Architecture

```
OpenSky REST API (live, global aircraft state)
                │
                ▼
   ┌──────────────────────────────────┐
   │  ingest_states (Airflow)         │
   │  every 10 min, dynamic mapping   │
   │  over 8 geographic regions       │
   └─────────────┬────────────────────┘
                 │  8 parquet files per run
                 ▼
       MinIO bronze/states/dt=.../hr=.../min=.../region={X}.parquet
                 │
                 │  Asset event: bronze_states
                 ▼
   ┌──────────────────────────────────┐
   │  transform_marts (Airflow)       │
   │  asset-triggered:                │
   │  load → dbt run → dbt test       │
   └─────────────┬────────────────────┘
                 │
                 ▼
       Postgres analytics
         staging.raw_states        (loaded from bronze)
         staging.stg_states        (dbt view: typed, deduped)
         marts.agg_country_traffic (dbt table)
         marts.anomalies           (dbt table: data quality)
                 │
                 ▼
       Superset (BI)
         OpenSky Live dashboard:
         KPI + ranked countries + anomaly breakdown
```

## Tech stack

- **Apache Airflow 3** — orchestration; TaskFlow API, dynamic task mapping,
  asset-driven cross-DAG scheduling.
- **dbt** with Postgres — staging and mart transformations, schema tests,
  custom `generate_schema_name` macro for clean schema names.
- **MinIO** — S3-compatible object store for the bronze layer.
- **polars + pyarrow** — fast columnar transforms in the ingestion path.
- **Apache Superset** — BI; dashboard and charts provisioned declaratively
  via the asset import bundle.
- **Three separate Postgres instances** (see *Storage layout* below).
- **Docker Compose** — full local stack on an isolated bridge network,
  host ports in the 33xxx–39xxx range bound to loopback only, to avoid
  collisions with other dev work.

## Storage layout

Three Postgres instances, by design, plus MinIO:

| Service                | Role                                    | Why separate                                    |
|------------------------|-----------------------------------------|--------------------------------------------------|
| `postgres-airflow`     | Airflow metadata: DAG runs, XCom, etc.  | Latency-sensitive; scheduler heartbeat depends on it |
| `postgres-analytics`   | Warehouse: staging + marts              | Bursty heavy queries; mustn't threaten scheduler |
| `postgres-superset`    | Superset metadata: dashboards, charts   | Different upgrade cadence; recoverable from YAML in repo |
| `minio` (object store) | Bronze layer parquet                    | Cheap, append-only, partitioned for query pruning |

The cardinal rule is don't mix orchestration metadata with analytical data.
A runaway analytical query that locks tables should not take down the
scheduler. Each Postgres has its own user, volume, and backup profile;
the analytics user has no read access to Airflow's connection passwords.

## Quickstart

```bash
git clone <this-repo>
cd opensky-airflow
cp .env.example .env
# Fill in the blank secrets in .env. Each has a "Generate with:" hint
# in the example file.
docker compose up -d
```

First boot takes ~3–5 minutes for image builds and the initial Postgres
migrations.

When healthy:

| Service             | URL                              | Login                |
|---------------------|----------------------------------|----------------------|
| Airflow             | http://localhost:38080           | admin / admin        |
| Superset            | http://localhost:38088           | from `.env`          |
| MinIO Console       | http://localhost:39001           | from `.env`          |
| Analytics Postgres  | localhost:35432                  | from `.env`          |

To populate data: in Airflow, unpause `ingest_states` and trigger it. The
asset-driven `transform_marts` cascades automatically. Within a minute the
Superset dashboard will populate with live data.

## Key design decisions

**Two-layer Python architecture: thin DAGs, fat helpers.**
DAG files import from `include/` and call helpers inside `@task` functions.
This keeps DAG-parse time low — the scheduler re-parses every file on a
short interval — and isolates infrastructure failures from DAG registration.

**Dynamic task mapping over geographic regions.**
The world is split into 8 bounding boxes (`include/regions.py`). Each
region fetches in parallel as its own task instance with independent retry
and logs. Smaller bboxes also cost fewer OpenSky API credits.

**Asset-triggered transform, not `ExternalTaskSensor`.**
`transform_marts` subscribes to `bronze_states` (declared in
`include/assets.py`). When `ingest_states.summarize` succeeds, the asset
event fires and the transform DAG triggers automatically. No polling, no
cron coupling.

**Three Postgres instances, not one.**
Detailed in *Storage layout* above. The short version: scheduler metadata,
warehouse, and BI metadata have different latency profiles, backup needs,
and failure semantics. Mixing them creates a single point of failure that's
not justified by the saved overhead.

**`TRUNCATE`, not `DROP`, for staging reloads.**
The bronze-to-staging load uses `TRUNCATE TABLE` followed by
`to_sql(if_exists='append')`. `DROP` would cascade-destroy the dependent
dbt view, forcing it to rebuild on every load. `TRUNCATE` preserves the
table structure and downstream views.

**Custom dbt `generate_schema_name` macro.**
By default, dbt concatenates the profile schema with the per-folder
schema config, producing names like `marts_staging` and `marts_marts`.
The macro override in `dbt/opensky/macros/generate_schema_name.sql` uses
the per-folder name literally. Cleaner names; the standard fix for
single-environment setups.

**Declarative Superset bootstrap.**
Database connection, datasets, charts, and the dashboard live as YAML in
`superset/assets/`. On container start, a bootstrap script calls
`ImportAssetsCommand` directly inside a Flask app context — more reliable
than the CLI across Superset versions. Credentials are substituted from
env vars at import time, never committed to YAML.

**Env-driven secrets with mandatory fail-on-missing.**
Every secret uses `${VAR:?error msg}` in Compose. No defaults for secrets;
Compose refuses to start if a required secret is unset. This prevents the
"accidentally inherited the dev default in staging" class of bugs.

## Project layout

```
opensky-airflow/
├── Dockerfile                       # Airflow image with project deps
├── docker-compose.yml               # Full stack
├── docker-compose.override.yml      # Host port bindings (loopback only)
├── .env.example                     # Secrets template
├── dags/                            # Airflow DAG definitions (thin)
│   ├── ingest_states.py
│   └── transform_marts.py
├── include/                         # Logic imported by DAGs
│   ├── opensky_client.py            # API client with auth + retries
│   ├── minio_helpers.py             # Parquet IO via s3fs
│   ├── regions.py                   # Geographic bbox config
│   └── assets.py                    # Centralized Asset URIs
├── dbt/opensky/                     # dbt project
│   ├── dbt_project.yml
│   ├── profiles.yml
│   ├── macros/
│   │   └── generate_schema_name.sql
│   └── models/
│       ├── sources.yml
│       ├── staging/stg_states.sql
│       └── marts/
│           ├── agg_country_traffic.sql
│           └── anomalies.sql
└── superset/                        # Superset image, bootstrap, asset bundle
    ├── Dockerfile
    ├── bootstrap.sh
    ├── superset_config.py
    └── assets/                      # Round-trippable YAML
        ├── metadata.yaml
        ├── databases/analytics.yaml
        ├── datasets/analytics/
        ├── charts/
        └── dashboards/
```

## What I would do at scale

- **Kubernetes executor in Airflow.** LocalExecutor here for simplicity;
  per-task isolation via Kubernetes pods would be the production move.
- **Incremental dbt models.** Current marts rebuild fully on each run.
  Fine for our data volume; switch to `materialized='incremental'` with
  partition pruning at scale.
- **Partitioned external tables.** Postgres analytics is convenient but
  doesn't scale to terabytes. At that point: Iceberg or Delta over the
  bronze parquet, queried with Trino or DuckDB.
- **Cosmos for dbt.** Currently `dbt run` is a single BashOperator. Cosmos
  would create one Airflow task per dbt model for per-model retries and
  observability in the Airflow UI.
- **Secrets backend.** Local `.env` works for dev; production replaces it
  with AWS Secrets Manager or GCP Secret Manager via Airflow's secrets
  backend. The env-var contract stays the same; only the source changes.
- **Source freshness checks.** dbt source freshness plus Airflow Deadline
  Alerts for SLO monitoring.
- **A second ingestion DAG.** `ingest_flights` for OpenSky's
  `/flights/arrival` and `/flights/departure` endpoints, on a daily
  schedule, joined into mart tables by route.
- **Observability stack.** StatsD → Prometheus → Grafana wired to Airflow's
  built-in metrics. A custom statsd mapping file prevents cardinality
  explosions from per-DAG metric names becoming labels.

## License

MIT