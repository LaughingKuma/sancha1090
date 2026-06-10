# sancha1090: a local-first data platform

A local-first platform that turns a live rooftop ADS-B receiver over Tokyo into
both a real-time map and a full Iceberg lakehouse. The receiver's own feed drives
a streaming hot path (Redpanda → RisingWave) for what's overhead *right now*, and
Airflow-orchestrated bronze/silver/gold marts (Garage S3 + Polaris catalog + Trino
+ dbt + Superset) for the accumulated history — all on a single host in Docker
Compose, no cloud accounts.

The receiver is the anchor: everything it hears is ground truth. Around it, the
[OpenSky Network](https://opensky-network.org) adds the rings the antenna can't
reach alone — **context** (all of Japan and the surrounding ocean, beyond its
horizon) and, increasingly, **backstory** (where those flights came from and are
headed). The two feeds stay on separate refresh tracks and fuse in
`gold.fct_flight_legs`.

> **Data model:** the full column-level schema, lineage, and entity map for every
> bronze/silver/gold table live in **[`docs/datalake.md`](docs/datalake.md)**.

## Architecture

The antenna feed and the OpenSky context feed both land in Iceberg bronze and stay
on separate refresh tracks (partitioned by dbt tag so they never race), fusing only
in `gold.fct_flight_legs`:

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/architecture-dark.svg">
    <img src="docs/architecture.svg" alt="sancha1090 medallion lakehouse: a rooftop ADS-B antenna plus OpenSky context feed flow through bronze → silver → gold to Trino + Superset" width="520">
  </picture>
</p>

Provenance for both feeds lives in Postgres (`public.ingestion_manifest` for the
OpenSky context feed, `public.adsb_ingestion_manifest` for the rooftop antenna) —
one row per landed file. See
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

Common tasks are wrapped in a `Makefile` — `make` lists them (`make up`, `make test`,
`make lint`, `make parse`, `make check`).

First boot: ~3–5 min for image builds + initial Postgres migrations.
Airflow UI at <http://localhost:38080> (admin / admin).

Trigger `ingest_states` in Airflow to start populating; `tableize_states`
and `transform_marts` cascade automatically via asset events.

### Live hot path (v4)

A single-node **Redpanda** broker (`redpanda` service) carries the live ADS-B feed:
the rooftop antenna edge unit publishes readsb state to topic `adsb.live` over the
LAN, advertised at `REDPANDA_EXTERNAL_HOST:19092` (set to the main PC's LAN IP in
`.env`). The `redpanda-init` one-shot creates the topic and enforces its ~10-min
retention. The external listener's host port is **not** published by default — to
expose it on the LAN for the edge, add it to the gitignored `docker-compose.local.yml`
(same pattern as Garage's `:3900`) and make sure that file is in `COMPOSE_FILE` (see
`.env.example`):

```yaml
# docker-compose.local.yml
services:
  redpanda:
    ports:
      - "0.0.0.0:19092:19092"
```

Verify:

```bash
docker compose exec redpanda rpk cluster info   # broker healthy, lists brokers
docker compose exec redpanda rpk topic list     # shows adsb.live
# from the edge host, prove the advertised listener is reachable
# (<main-pc-ip> = REDPANDA_EXTERNAL_HOST from the main PC's .env):
nc -vz <main-pc-ip> 19092
```

**RisingWave** (`risingwave` service, v4.1) consumes `adsb.live` from the internal
listener (`redpanda:9092`) and maintains the enriched live materialized views that
Superset's "Live" dashboard reads over PG-wire. Single-node mode: meta + state live
on one local volume, no extra sidecars.

The live views use a **120 s staleness window**: `mv_current_aircraft` means "aircraft
with a position update in the last 120 s", matching tar1090's measured position
retention — fringe aircraft (>60 nmi, weak signal) decode positions tens of seconds
apart, and tar1090 keeps showing their last fix for ~2 minutes, so a tighter window
undercounts contacts it still renders. Expect the count to sit **0–1 below tar1090's
total**: tar1090 also lists aircraft heard without a position fix, which never enter
the position feed the hot path consumes.

Verify:

```bash
docker compose exec postgres-airflow psql -h risingwave -p 4566 -U root -d dev -c 'SELECT version();'
# or from the host (loopback port from docker-compose.override.yml):
psql -h 127.0.0.1 -p 34566 -U root -d dev -c 'SELECT version();'
```

The **`livemap`** service (v4.3) is a small FastAPI sidecar that polls
`mv_current_aircraft` twice a second into an in-memory snapshot and serves a dark
maplibre + deck.gl map of live aircraft over Tokyo at **<http://localhost:38100>**.
The server-side cache is the point: every browser tab shares that one query stream,
so N viewers never become N queries against RisingWave. Aircraft dead-reckon between
polls (track/groundspeed, capped at 15 s of projection) and fade with position age
over the 120 s window.

## Tech stack

- Apache Airflow 3.2 — TaskFlow, dynamic task mapping, asset chains
- Apache Iceberg via PyIceberg + Apache Polaris REST catalog
- dbt-trino for silver/gold marts
- Trino as the query engine over Iceberg
- Garage as a local S3-compatible object store
- polars + pyarrow for in-memory transforms
- Three Postgres instances (Airflow metadata, Polaris+manifest, Superset metadata)
- Redpanda — single-node Kafka broker carrying the v4 ADS-B live hot path (edge → `adsb.live`)
- RisingWave — streaming engine materializing the live enriched views off `adsb.live` (v4.1)
- FastAPI + maplibre + deck.gl — the `livemap` live aircraft map over RisingWave (v4.3)
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
├── risingwave/sql/                  # Live MV DDL (source/dims/enriched views)
├── livemap/                         # FastAPI + maplibre/deck.gl live aircraft map
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
asserts it stays under the 8,000/day active-feeder quota.

## Acknowledgements

This project stands on two community networks that choose to keep aviation
data open:

- **[The OpenSky Network](https://opensky-network.org)** — the wide-context
  ring around the antenna: every state vector beyond the receiver's horizon and
  every flight narrative in the backstory ring comes from their crowdsourced
  receiver network, run as a non-profit for research since 2013. This platform
  also feeds back into it.
- **[adsb.lol](https://adsb.lol)** — the deep history: their daily
  `globe_history` releases are one of the very few genuinely open archives of
  global aircraft traces, published under ODbL with no gatekeeping. The entire
  pre-pipeline backfill exists because they publish what others paywall.

If you run an ADS-B receiver, feed these networks.

## License & data attribution

Code: MIT.

Data: live context and flight histories from the
[OpenSky Network](https://opensky-network.org) (research/non-commercial terms);
pre-pipeline historical positions contain data from
[adsb.lol](https://adsb.lol), licensed under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
