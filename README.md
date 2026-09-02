# sancha1090: a local-first data platform

A rooftop ADS-B receiver over Tokyo feeds two paths at once: a streaming hot path
(Redpanda → RisingWave) for what is overhead right now, and Airflow-orchestrated
bronze/silver/gold marts (Garage S3 → ClickHouse → dbt → Superset) for the accumulated
history. Both run on a single host under Docker Compose, with no cloud accounts. The
live hot-path map is public at **[sancha1090.tokyo](https://sancha1090.tokyo)**.

The receiver is the anchor, and whatever it hears directly is ground truth. The
[OpenSky Network](https://opensky-network.org) covers what one antenna cannot: all of
Japan and the surrounding ocean beyond the receiver's horizon, plus arrival and departure
records for where those flights came from and are headed. [adsb.lol](https://adsb.lol)'s
ODbL `globe_history` supplies the deep past, the hours before the pipeline existed. Each
source keeps its own bronze table and its own refresh track, and they fuse only at
well-defined seams, the sharpest being `gold.fct_flights_reconciled`.

> Data model: lineage + layer map in [`docs/datalake.md`](docs/datalake.md); columns in the
> dbt yml.

## Architecture

The rooftop, OpenSky, adsb.lol, and FAA SWIM feeds land as Parquet in the Garage S3 zone and
load into ClickHouse bronze via manifest-driven per-file bookkeeping. They stay on separate
refresh tracks, partitioned by dbt tag so they never race, and fuse only in the marts that
declare a fusion seam. Cheap aggregates skip the rebuild cycle entirely:
`AggregatingMergeTree` views update on insert and serve through merge-aware views. Every
served value is re-checked every 15 minutes against bronze by `ch_serving_parity`. Replay
safety is per lane: the OpenSky states and SWIM message logs are `ReplacingMergeTree` keyed on
a content fingerprint, so a crash-replay cannot double-count; the rooftop feed replaces on its
sort key and the adsb.lol trace tables upsert on refetch; the adsb.lol states and OpenSky
flights tables are plain `MergeTree` append-only. A daily alarm reds when a dbt model nears
its memory cap, and a NAS cold archive keeps a verified copy-only mirror of the landing zone. This README names the layers
conceptually (`silver.`, `gold.`); the physical ClickHouse databases are `bronze`,
`silver_ch`, `gold_ch`, and `dim`.

`bronze.path_estimates` sits outside that mart flow as append-only serving exhaust: the
livemap logs every estimate it computes there, and `gold_ch.fct_est_settlement` scores each
one against the coverage that arrived later. The gold footprint is serving telemetry only —
counts, distributions, and scalar scores — never estimated positions. The standing demand
read is
`SELECT day, requests, served FROM gold_ch.agg_est_usage_daily WHERE producer = 'serving-public' ORDER BY day`;
the drift read and the column semantics are in [`docs/datalake.md`](docs/datalake.md).

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/architecture-dark.svg">
    <img src="docs/architecture.svg" alt="sancha1090 architecture: the rooftop ADS-B, OpenSky, adsb.lol, and FAA SWIM feeds land in a Garage S3 zone, load into ClickHouse bronze/silver/gold, and serve Superset, with a NAS cold archive and a 15-minute served-value check" width="520">
  </picture>
</p>

Provenance lives in Postgres (`public.ingestion_manifest` for the OpenSky, adsb.lol, and
FAA SWIM lanes, `public.adsb_ingestion_manifest` for the rooftop antenna), one row per landed
file. The ingest path fails loud on anything it does not recognize: a producer manifest
outside its lane's prefix, or an unregistered object under the ADS-B prefix, aborts the run
rather than blending sources.

### FAA SWIM: filed flight plans

The FAA SWIM lane taps the SWIM Cloud Distribution Service's TFMData feed of filed flight
plans. An always-on `swim-consumer` holds a persistent subscription, parses each message,
and flushes rolling Parquet to the same Garage zone; the write lands durably before the
message is acknowledged, so a dropped connection cannot lose data. A 5-minute
`tableize_swim` DAG drains it into `bronze.swim_flightdata`, and `transform_marts` builds
the latest amendment per flight, a callsign→hex match against the states feeds (SWIM carries
no Mode-S hex of its own), and an origin/destination opinion.

That opinion votes in `gold.fct_flights_reconciled` at the top of the source-authority
order, though plurality outvotes authority: rank only breaks a tie. It fires only for filed
plans with an endpoint inside the observation box (20-50°N, 122-165°E), so a pure overflight
stays unresolved. Its value is the foreign endpoint on an international leg no in-box source
ever sees.

A second obligation rides the same feed's identity data: the FAA's LADD privacy list,
tracked SCD2 in `dim.dim_ladd` from a weekly pull and applied at display time on the public
livemap only. The mart flags rather than deletes, and the private LAN instance suppresses
nothing: LADD is a public-display obligation, not a data-access restriction. This is the
pipeline's own read of a public FAA feed and a public FAA privacy list. The FAA neither
publishes nor endorses it.

## Where it came from

sancha1090 started as a local-first Iceberg + Polaris + Trino lakehouse and ran that way
through 54 releases. v6.0 replaced the batch warehouse with ClickHouse after the same queries
measured 30 to 1,700 times faster on the same box; Postgres kept the manifests, RisingWave
the live hot path. The public showcase repository starts at v6.0, and the measurements and
trade-offs are frozen in [`docs/history.md`](docs/history.md).

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

Common tasks are wrapped in a `Makefile`; `make` lists them.

First boot takes 3 to 5 minutes for image builds plus the initial Postgres migrations.
Airflow UI at <http://localhost:38080> (admin / admin). `docker compose up` bootstraps the
ClickHouse schemas, dictionary, seeds, and aircraft registry on its own; the optional
multi-year adsb.lol history backfill is a separate manual step, `scripts/ch_setup_marts.sh`.

Trigger `ingest_states` to start populating: `tableize_states` cascades via asset events,
and `transform_marts` rebuilds on its own 10-minute cron.

### Live hot path

A single-node Redpanda broker (the `redpanda` service) carries the live ADS-B feed. The
rooftop antenna edge unit publishes readsb state to topic `adsb.live` over the LAN,
advertised at `REDPANDA_EXTERNAL_HOST:19092` (the main PC's LAN IP, set in `.env`); the
`redpanda-init` one-shot creates the topic and enforces its ~35-min retention. The external
listener's host port is not published by default — the snippet that exposes it on the LAN,
and the commands that verify the broker, are in `.env.example` and beside the `redpanda`
service in `docker-compose.yml`.

RisingWave (the `risingwave` service) consumes `adsb.live` from the internal listener
(`redpanda:9092`) and maintains the enriched live materialized views that Superset's "Live"
dashboard reads over PG-wire. It runs single-node: meta and state on one local volume, no
extra sidecars. The live views use a 120 s staleness window, matching tar1090's measured
position retention, so the count sits 0 to 1 below tar1090's total.

The `livemap` service is a small FastAPI sidecar that polls `mv_current_aircraft` twice a
second into an in-memory snapshot and serves a dark maplibre + deck.gl map of live aircraft
over Tokyo at <http://localhost:38100>. That server-side cache is the point: every browser
tab shares one query stream, so N viewers never become N queries against RisingWave.
Aircraft dead-reckon between polls (capped at 15 s of projection) and fade with position age
over the 120 s window. It has grown into the platform's showcase surface: per-type
silhouettes (ICAO Doc 8643), motion trails, a spotlight card with airline, registration, and
owner identity, click-to-select track history, a recent-flights drill-down that draws each
flight's own fused historical path, and the antenna's measured coverage outline. Those
features are computed in the ClickHouse batch lane and shipped to the map, so the hot path
stays a thin 120-second window.

The private instance additionally hosts an **analysis workbench**: a left-rail console over
the reconciled ClickHouse marts, with an overview home, an anomaly-flag feed (from a
companion mart, `gold.fct_flight_flags`), trends, estimate quality, coverage health, an
airline-to-service-to-flight drill, and typed search. Every instance row carries a
precomputed reconstruction-tier badge (settled / estimated / provisional / none), every
number is a doorway into the view that explains it, and the state is URL-addressable. Only
the private instance registers the `/features` endpoint that unlocks it, so the public map
serves none of it, not even its static modules. The frontend is a Preact island behind a
typed map facade, built with Vite and shipped image-baked (`make livemap-image`).

`/path` follows a three-rung freshness ladder. Live position always comes from the
120-second RisingWave window above. A click on a flight that reconciled after
`fct_flight_path`'s settled build head gets a provisional trajectory instead, fused straight
from the same three bronze sources at serve time (rooftop > adsb.lol > OpenSky) and returned
with `"provisional": true`. Once the daily settlement build reaches that flight's start day,
the same click resolves to the settled, mart-served path. Every `/path` response carries
`Cache-Control: no-store`.

The spotlight can overlay an estimate on any drawn path (`GET /path/{flight_id}/estimate`),
or a dead-reckoning wedge ahead of an aircraft still in the live snapshot
(`GET /estimate/live/{icao24}`). The estimator bridges coverage gaps with great circles drawn
as a violet dashed overlay; when the flight has a filed FAA SWIM plan, the oceanic coordinate
waypoints airlines file for exactly the stretches no receiver hears are parsed from the route
string and, when they pass their guards, replace that great circle. Each segment carries a
p50/p90 uncertainty band drawn as translucent ribbons in true meters, so an estimate reads as
a corridor, not a track. The endpoints inherit `/path`'s privacy posture whole: LADD authorization re-runs on every
settled cache hit, and suppressed, unknown, and errored requests all return the same
byte-identical fail-closed empty response.

### Public deployment (Cloudflare Tunnel)

The map can be exposed to the public web without opening a router port. A dedicated
`livemap-public` service (a second copy of the same image) runs alongside the private instance
and is reached only through a `cloudflared` container that dials **out** to Cloudflare: the
home IP never appears in DNS and nothing else in the stack is reachable through the tunnel.
That is how the public map is served. Both live behind the `public` compose profile, so the
default `docker compose up -d` is unaffected until an operator opts in:

```bash
# add CLOUDFLARED_TUNNEL_TOKEN to .env, then:
docker compose --profile public up -d livemap-public cloudflared
```

The tunnel is remotely managed (token mode): the tunnel and its hostname route are created in
the Cloudflare dashboard, pointing at `http://livemap-public:8000`, with a Cache Rule that
makes `/aircraft` cache-eligible — Cloudflare does not cache JSON by default, so the app's
cache header is inert on its own. The public instance runs hardened
(`LIVEMAP_PUBLIC_MODE=1`): per-IP rate limiting, an edge-cache hint on the snapshot endpoint,
and standard security headers. It publishes no host port, and `cloudflared` shares only a
dedicated `edge` network with it.

The public instance serves no receiver anchor. Its `/range-outline` anchors at the
**centroid of the measured coverage outline** (`center_kind: "coverage"`): a pure function of
the polygon every visitor already receives, so it leaks nothing the outline doesn't, and the
terrain-shaped outline biases it away from the receiver. The public map labels that dot
"coverage center" and prefixes its Range/Bearing readouts with `≈`. The private instance keeps
the real receiver anchor and exact readouts, and the feeder coordinates it reads from `.env`
never enter the public instance's environment.

## Tech stack

- Apache Airflow 3.2: TaskFlow, dynamic task mapping, asset chains
- ClickHouse: the columnar batch warehouse (bronze raw landing plus silver/gold marts, with
  self-maintaining `AggregatingMergeTree` views for the cheap aggregates)
- dbt-clickhouse for the mart builds
- Garage: a local S3-compatible object store (the Parquet landing zone)
- polars + pyarrow for in-memory transforms
- Three Postgres instances (Airflow metadata, ingestion manifests, Superset metadata), each
  with its own user and volume: orchestration and analytical data never share an instance, so
  a runaway query that locks tables cannot take down the scheduler
- Redpanda: single-node Kafka broker carrying the ADS-B live hot path
- RisingWave: streaming engine materializing the live enriched views off `adsb.live`
- FastAPI + maplibre + deck.gl: the `livemap` aircraft map over RisingWave

## Project layout

```
sancha1090/
├── docker-compose.yml               # Full stack
├── docker-compose.override.yml      # Host port bindings (loopback only)
├── docker-compose.frontend-dev.yml  # Opt-in livemap static bind mount
├── docker-compose.local.yml         # Host-specific overrides (gitignored)
├── .env.example                     # Secrets template
├── config/                          # Service config files (RisingWave tuning)
├── dags/                            # Thin Airflow DAGs
├── include/                         # Logic imported by DAGs
├── dbt/sancha1090/                  # dbt project (silver + gold marts)
├── clickhouse/sql/                  # Warehouse init DDL (bronze/dim, dictionaries)
├── risingwave/sql/                  # Live MV DDL (source/dims/enriched views)
├── swim-consumer/                   # Always-on FAA SWIM subscriber service
├── livemap/                         # FastAPI + maplibre/deck.gl live aircraft map
├── superset/                        # Superset image + seeded dashboard assets
├── docs/                            # datalake.md (lineage + layer map) + history.md
├── scripts/                         # Operational helpers
└── tests/                           # pytest suite
```

## Tests

```bash
docker compose exec airflow-scheduler bash -c "cd /opt/airflow && pytest tests/ -v"
```

The 1,000-odd tests cover DAG integrity (every DAG parses with its expected schedule and task
set), ingest discovery and the fail-loud boundary guards, manifest bookkeeping, bronze dedup
contracts, parity-check logic, ADS-B schema drift, and the OpenSky credit budget:
`tests/test_credit_budget.py` derives the daily cost from the live region config and the
ingest schedule and asserts it stays under the 8,000/day active-feeder quota. The frontend
has its own harnesses: `make test-js`, `make lint-js`, and `make e2e` (Playwright in fixture
mode, no ClickHouse or RisingWave).

## Acknowledgements

This project stands on three community projects that choose to keep aviation data open:

- **[The OpenSky Network](https://opensky-network.org)** covers the ring around the antenna:
  every state vector beyond the receiver's horizon, and every flight narrative in the
  backstory ring, comes from their crowdsourced receiver network, run as a non-profit for
  research since 2013. This platform feeds back into it.
- **[adsb.lol](https://adsb.lol)** supplies the deep history. Their daily `globe_history`
  releases are one of the very few genuinely open archives of global aircraft traces,
  published under ODbL with no gatekeeping. The whole pre-pipeline backfill exists because
  they publish what others paywall.

  The same full-day traces resolve the overflight route backstory: where a flight that only
  clips the antenna's ring came from and is headed. Each trace is walked into
  airport-to-airport segments, chained back into whole flights across coverage gaps, and
  voted into `gold.fct_flights_reconciled`, the canonical origin/destination mart (chain
  rules: `int_flight_chains_adsblol.sql`; the vote: `fct_flights_reconciled.sql`). At the
  maintainer's request that lane streams the same daily release (~4 GB in ~90 s), not the
  live site.

  Because that consensus mixes observation with inference, every endpoint records how it was
  derived in `origin_source`/`dest_source`, so a guess is never mistaken for a sighting:
  - `swim`: **filed**, the highest authority — the FAA's own system-of-record flight plan,
    US-touching flights only, matched to an airframe by density-scored callsign; an
    ambiguous match is withheld, not guessed.
  - `opensky_flights`: **observed** — OpenSky's own arrival/departure record for this flight.
  - `opensky_states`: **observed** — a low-altitude fix seen inside the tracked box.
    Airline-shaped callsigns only snap to scheduled-service airports, so a 787 is never
    attributed to a military strip.
  - `adsblol`: **inferred** — two coverage-split segments chained because the boundary
    groundspeed looked like cruise. An aircraft that landed and left again inside a gap
    reads as one continuous flight.
  - `curated`: **entered by hand** — an evidence-backed row in the `dim_route_overrides`
    seed, applied only where every source left the endpoint NULL.

  Each endpoint also carries its vote tally and an agreement label (`unanimous`, `majority`,
  `tiebreak`, `single`, `curated`), so a low-trust resolution stays visible.
- **[Virtual Radar Server standing data](https://github.com/vradarserver/standing-data)**
  is the community-curated callsign→route table (via the hourly
  [adsb.lol mirror](https://vrs-standing-data.adsb.lol)) that gives the reconciled mart a
  route opinion independent of position inference.

If you run an ADS-B receiver, feed these networks.

And the open reference datasets that decide how an aircraft is drawn:

- **[Mictronics readsb database](https://github.com/Mictronics/readsb)**: current ICAO
  operator codes → airline names, the same database tar1090 and adsbexchange render, so
  callsign decoding tracks designator reassignments rather than going stale.
- **[Wikidata](https://www.wikidata.org)**: cross-referenced offline to clean those airline
  names into their public brand forms, baked static into the seed.
- **[ICAO Doc 8643](https://github.com/rikgale/ICAOList)**: type designators → the silhouette
  each aircraft is drawn with.
- **[tar1090](https://github.com/wiedehopf/tar1090)**: its ICAO 24-bit address → country table
  drives the registration-country flags.
- **[OurAirports](https://ourairports.com/data/)**: airport names, coordinates, runway
  lengths, and scheduled-service classification.
- **[ADSBExchange basic-ac-db](https://www.adsbexchange.com/data/)**: a daily-updated public
  snapshot, ingested weekly, of registration, type, manufacturer, model, and owner that fills
  the blanks the OpenSky registry leaves, mostly typecode — which the runway feasibility gate
  (`docs/datalake.md`, Endpoint feasibility) depends on.

## License & data attribution

Code: MIT.

Data: live context and flight histories from the
[OpenSky Network](https://opensky-network.org) (research/non-commercial terms);
pre-pipeline historical positions contain data from
[adsb.lol](https://adsb.lol), licensed under the
[Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/).

Reference data: airline operator codes from the
[Mictronics readsb database](https://github.com/Mictronics/readsb), with brand-name
cleanup cross-referenced against [Wikidata](https://www.wikidata.org)
([CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/)); aircraft type
designators from ICAO Doc 8643; the ICAO 24-bit address → country table from
[tar1090](https://github.com/wiedehopf/tar1090); airport data from
[OurAirports](https://ourairports.com/data/), released to the public domain; aircraft identity
data © ADSBExchange, basic-ac-db public download.
