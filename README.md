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
ODbL `globe_history` supplies the deep past, filling in the hours before the pipeline
existed. Each source keeps its own bronze table and its own refresh track, and they fuse
only at well-defined seams, the sharpest being `gold.fct_flight_legs`.

> Data model: the full column-level schema, lineage, and entity map for every
> bronze/silver/gold table live in [`docs/datalake.md`](docs/datalake.md).
> 
## Architecture

The rooftop, OpenSky, and adsb.lol feeds land as Parquet in the Garage S3 zone and load
into ClickHouse bronze via manifest-driven per-file bookkeeping. They stay on separate
refresh tracks, partitioned by dbt tag so they never race, and fuse only in
`gold.fct_flight_legs`. (The FAA SWIM lane below lands in the same zone on its own track.)
Cheap aggregates skip the rebuild cycle entirely: `AggregatingMergeTree` views update on
insert and serve through merge-aware views. Every served value is re-checked hourly against
bronze by the `ch_serving_parity` gate.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/architecture-dark.svg">
    <img src="docs/architecture.svg" alt="sancha1090 architecture: rooftop ADS-B antenna, OpenSky context feed, adsb.lol history, and FAA SWIM filed flight plans land in a Garage S3 zone, load into ClickHouse bronze → silver → gold, serve Superset, with a NAS cold archive and an hourly parity gate" width="520">
  </picture>
</p>

Provenance lives in Postgres (`public.ingestion_manifest` for the OpenSky, adsb.lol, and
FAA SWIM lanes, `public.adsb_ingestion_manifest` for the rooftop antenna), one row per
landed file. The ingest path fails loud on anything it does not recognize: a producer
manifest outside its lane's prefix, or an unregistered object under the ADS-B prefix,
aborts the run rather than blending sources. See [`docs/datalake.md`](docs/datalake.md)
for the full lineage, entity map, and per-table schema.

### FAA SWIM: filed flight plans

A fourth, independent lane taps FAA SWIM (System Wide Information Management), specifically
the SWIM Cloud Distribution Service's TFMData feed of filed flight plans. An always-on
`swim-consumer` service holds a persistent subscription, parses each message, and flushes
rolling Parquet to the same Garage zone (`bronze/swim_raw/`). The write has to land durably
before the message is acknowledged, so a dropped connection cannot silently lose data. A
5-minute `tableize_swim` DAG (skipping ticks with nothing new to load) drains it into
`bronze.swim_flightdata`, and `transform_marts` builds two models on top: `int_swim_flight`,
the latest amendment per flight plus a density-scored callsign→icao24 match against the
states feeds, since SWIM carries no Mode-S hex of its own; and `int_swim_opinion`, an
origin/destination read scoped to US-touching flights, including the foreign endpoint on
international legs that the antenna and OpenSky's Japan box never see (the San Francisco
side of a Tokyo to San Francisco flight, for instance).

`int_swim_opinion` now casts a vote in `gold.fct_flights_reconciled`'s cross-source
consensus, at the top of the source-authority order, though plurality still outvotes
authority: rank only breaks a tie. That vote is scoped further. It fires only for filed
plans with at least one endpoint inside the observation box (20-50°N, 122-165°E), so a pure
overflight, with both endpoints outside the box, stays unresolved instead of picking up a
filed-plan stamp that no in-box source can corroborate.

A second, independent obligation rides the same feed's identity data: the FAA's LADD
privacy list. Any airframe currently listed is tracked SCD2 in `dim.dim_ladd` (a manual
weekly pull) and suppressed at display time everywhere the platform serves live or
historical positions. The livemap's `/aircraft`, `/flights`, and `/track` endpoints all drop
a listed airframe before it reaches a client, the same way the reconciled mart flags it
(`is_ladd`) rather than deleting the row. This is the pipeline's own read of a public FAA
system-wide information feed and a public FAA privacy list. The FAA neither publishes nor
endorses it.

## Architecture evolution

### The Iceberg lakehouse (v2 to v5.12)

sancha1090 began as a local-first medallion lakehouse: the rooftop antenna plus the OpenSky
network feeding bronze → silver → gold Iceberg tables on Garage (S3), a Polaris REST
catalog, and Trino, transformed by dbt-on-Trino and served to Superset. It was deliberately
the full open-table-format stack, with schema evolution, time travel, zero-copy `add_files`
ingest, and a catalog service, and it ran the whole thing on one box. Eighteen released
versions evolved it from a world sweep to a focused Japan feed with a live map, flight legs,
and airline analytics.

### Why it changed: right-sizing to the workload (v6.0)

ADS-B is append-heavy, time-ordered telemetry, which is the canonical columnar-OLAP
workload. As the data grew toward hundreds of millions of rows, the
distributed-query-engine and catalog stack was paying its full operational cost (JVM heap
tuning, worker OOM from view re-expansion, OPTIMIZE-vs-rebuild races, a separate metastore)
for none of its multi-engine or petabyte benefit on a single host. v6.0 replaces the Iceberg
+ Polaris + Trino batch warehouse with ClickHouse, keeping Postgres for manifests and
metadata and RisingWave for the live hot path. That leaves one engine instead of three
services, aggregates that maintain themselves, and an entire class of operational gotchas
deleted. The migration landed as eight reviewed, parity-gated phases. The lakehouse history
lives at the `v5.12` tag.

### What it became: a guarded warehouse (v6.1+)

The post-migration releases turned the parity gates from migration scaffolding into
permanent guarantees. Bronze became dedup-immune (`ReplacingMergeTree` with content
fingerprints, so a crash-replay cannot double-count). An hourly served-value gate re-derives
what Superset shows straight from bronze and refuses to let discrepancies age out unseen.
Storage was re-grained: the verbatim raw-JSON column was eliminated in favor of flags baked
at load, per-column ZSTD/T64 codecs went in, and the exact per-hour aggregate states got a
90-day TTL. A NAS cold archive keeps a verified copy-only mirror of the raw landing zone.
Every lane got source-keyed names plus fail-loud ingest boundary guards, so no source can
silently blend into another.

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

Common tasks are wrapped in a `Makefile`, and `make` lists them (`make up`, `make test`,
`make lint`, `make parse`, `make check`).

First boot takes 3 to 5 minutes for image builds plus the initial Postgres migrations.
Airflow UI at <http://localhost:38080> (admin / admin).

`docker compose up` bootstraps the ClickHouse marts automatically. `clickhouse-init`
provisions the bronze/dim schemas and the hex-country dictionary, then the one-shot
`clickhouse-marts-init` seeds the dims, reloads the dict, and loads the aircraft registry,
so the first transform run has its seed, registry, and dict dependencies in place. The
optional multi-year adsb.lol history backfill is a separate manual step:
`scripts/ch_setup_marts.sh` runs the full setup including the adsb.lol history load.

Trigger `ingest_states` in Airflow to start populating. `tableize_states` and
`transform_marts` cascade automatically via asset events.

### Live hot path (v4)

A single-node Redpanda broker (the `redpanda` service) carries the live ADS-B feed. The
rooftop antenna edge unit publishes readsb state to topic `adsb.live` over the LAN,
advertised at `REDPANDA_EXTERNAL_HOST:19092` (set to the main PC's LAN IP in `.env`). The
`redpanda-init` one-shot creates the topic and enforces its ~10-min retention. The external
listener's host port is not published by default. To expose it on the LAN for the edge, add
it to the gitignored `docker-compose.local.yml` (the same pattern as Garage's `:3900`) and
make sure that file is in `COMPOSE_FILE` (see `.env.example`):

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

RisingWave (the `risingwave` service, v4.1) consumes `adsb.live` from the internal listener
(`redpanda:9092`) and maintains the enriched live materialized views that Superset's "Live"
dashboard reads over PG-wire. It runs single-node: meta and state live on one local volume,
with no extra sidecars.

The live views use a 120 s staleness window. `mv_current_aircraft` means "aircraft with a
position update in the last 120 s", which matches tar1090's measured position retention.
Fringe aircraft (>60 nmi, weak signal) decode positions tens of seconds apart, and tar1090
keeps showing their last fix for about two minutes, so a tighter window undercounts contacts
it still renders. Expect the count to sit 0 to 1 below tar1090's total, because tar1090 also
lists aircraft heard without a position fix, and those never enter the position feed the hot
path consumes.

Verify:

```bash
docker compose exec postgres-airflow psql -h risingwave -p 4566 -U root -d dev -c 'SELECT version();'
# or from the host (loopback port from docker-compose.override.yml):
psql -h 127.0.0.1 -p 34566 -U root -d dev -c 'SELECT version();'
```

The `livemap` service is a small FastAPI sidecar that polls `mv_current_aircraft` twice a
second into an in-memory snapshot and serves a dark maplibre + deck.gl map of live aircraft
over Tokyo at <http://localhost:38100>. The server-side cache is the point: every browser tab
shares that one query stream, so N viewers never become N queries against RisingWave.
Aircraft dead-reckon between polls (track and groundspeed, capped at 15 s of projection) and
fade with position age over the 120 s window. It has grown into the platform's showcase
surface, with per-type aircraft silhouettes (ICAO Doc 8643), motion trails, a spotlight card
carrying airline, registration, and owner identity, click-to-select 30-minute track history,
a recent-flights drill-down per airframe that draws each flight's own fused historical path
on click, and the antenna's measured coverage outline. Those accumulated-history features are
computed in the ClickHouse batch lane and shipped to the map, so the hot path stays a thin
120-second window.

### Public deployment (Cloudflare Tunnel)

The map can be exposed to the public web without opening a single router port. A dedicated
`livemap-public` service (a second copy of the same image) runs alongside the private instance
and is reached only through a `cloudflared` container that dials **out** to Cloudflare — the
home IP never appears in DNS and nothing else in the stack is reachable through the tunnel.
This is how <https://sancha1090.tokyo> is served: the apex domain (not a subdomain) routes
through Cloudflare's edge, then over the tunnel to the public instance.
Both live behind the `public` compose profile, so the default `docker compose up -d` is
completely unaffected until an operator opts in:

```bash
# add CLOUDFLARED_TUNNEL_TOKEN to .env, then:
docker compose --profile public up -d livemap-public cloudflared
```

The tunnel is remotely managed (token mode): in the Cloudflare dashboard you create the tunnel,
route the public hostname to `http://livemap-public:8000`, and add a Cache Rule making `/aircraft`
cache-eligible (Cloudflare does not cache JSON by default, so the app's cache header is inert on
its own). The public instance runs in hardened mode (`LIVEMAP_PUBLIC_MODE=1`): a per-IP
token-bucket rate limit on the per-request database endpoints, an edge-cache hint on the snapshot
endpoint that — with that Cache Rule in place — lets Cloudflare absorb viewer fan-out, and standard
security headers. It publishes no host port at all, and `cloudflared` shares only a dedicated `edge`
network with it, so the tunnel reaches the public map and nothing else in the stack.

The public instance serves no receiver anchor: its `/range-outline` returns a null center, so the
map draws no receiver marker, no range rings, and no distance or bearing readouts — only the
measured coverage outline. It also does not receive the feeder coordinates that the private
instance reads from `.env`, so those coordinates never enter its environment.

## Tech stack

- Apache Airflow 3.2: TaskFlow, dynamic task mapping, asset chains
- ClickHouse: the columnar batch warehouse (bronze raw landing plus silver/gold marts, with
  self-maintaining `AggregatingMergeTree` views for the cheap aggregates)
- dbt-clickhouse for the silver/gold mart builds
- Garage as a local S3-compatible object store (the Parquet landing zone)
- polars + pyarrow for in-memory transforms
- Three Postgres instances (Airflow metadata, ingestion manifests, Superset metadata)
- Redpanda: single-node Kafka broker carrying the v4 ADS-B live hot path (edge → `adsb.live`)
- RisingWave: streaming engine materializing the live enriched views off `adsb.live` (v4.1)
- FastAPI + maplibre + deck.gl: the `livemap` live aircraft map over RisingWave (v4.3)
- Docker Compose for the whole stack

## Storage layout

| Service              | Role                                              |
|----------------------|---------------------------------------------------|
| `postgres-airflow`   | Airflow metadata: DAG runs, XCom, etc.            |
| `postgres-analytics` | Ingestion manifests (`ingestion_manifest` + `adsb_ingestion_manifest`) |
| `postgres-superset`  | Superset metadata                                 |
| `garage`             | Raw parquet landing zone (S3-compatible)          |

The rule: don't mix orchestration metadata with analytical data. A runaway query that locks
tables shouldn't be able to take down the scheduler. Each Postgres has its own user, volume,
and backup profile.

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
├── clickhouse/sql/                  # Warehouse init DDL (bronze/dim schemas, dictionaries)
├── risingwave/sql/                  # Live MV DDL (source/dims/enriched views)
├── livemap/                         # FastAPI + maplibre/deck.gl live aircraft map
├── superset/                        # Superset image + seeded dashboard assets
├── docs/                            # Data-model reference (datalake.md) + engineering notes (notes/)
├── scripts/                         # Operational helpers
└── tests/                           # pytest suite (300+ tests)
```

## Tests

```bash
docker compose exec airflow-scheduler bash -c "cd /opt/airflow && pytest tests/ -v"
```

The suite (300+ tests) covers DAG integrity (every DAG parses with its expected schedule and
task set), ingest discovery and the fail-loud boundary guards, manifest bookkeeping, bronze
dedup contracts, parity-gate logic, ADS-B schema drift, and the OpenSky credit budget.
`tests/test_credit_budget.py` computes the daily credit cost from the live region config and
the ingest schedule, then asserts it stays under the 8,000/day active-feeder quota.

## Acknowledgements

This project stands on three community projects that choose to keep aviation data open:

- **[The OpenSky Network](https://opensky-network.org)** covers the ring around the antenna:
  every state vector beyond the receiver's horizon, and every flight narrative in the
  backstory ring, comes from their crowdsourced receiver network, run as a non-profit for
  research since 2013. This platform also feeds back into it.
- **[adsb.lol](https://adsb.lol)** supplies the deep history. Their daily `globe_history`
  releases are one of the very few genuinely open archives of global aircraft traces,
  published under ODbL with no gatekeeping. The entire pre-pipeline backfill exists because
  they publish what others paywall.

  The same full-day traces also resolve the overflight route backstory, meaning where a
  flight that only clips the antenna's ring actually came from and is headed. Each aircraft's
  global trace is walked into airport-to-airport segments
  (`bronze.adsblol_flight_segments`, plus full per-second paths in
  `bronze.adsblol_flight_paths`). The walk breaks at missed landings even without a captured
  ground fix: a sub-1,000 ft fix beside a turnaround-sized gap, or a below-cruise gap of 30+
  minutes crossed at under 100 km/h implied groundspeed (the aircraft must have stopped inside
  it), so an out-and-back rotation doesn't fuse into one same-airport segment. Because a trace
  breaks wherever crowdsourced coverage drops out, those segments are then chained back into
  whole flights (`silver.int_flight_chains_adsblol`), including across UTC trace-day
  boundaries, whenever the implied great-circle groundspeed across the gap is cruise-plausible
  (300 to 1,100 km/h). The exception is a gap that hides a ground stop: an hour-plus gap a jet
  crosses at under 550 km/h, or a sub-1,000 ft fix beside a turnaround-sized gap, breaks the
  chain instead of fusing a tech-stop rotation into one flight. A daily DAG
  (`ingest_adsblol_routes`) makes targeted per-hex fetches for every reconciled flight touching
  the day (swept across a four-day window to catch late-reconciled arrivals), plus a nightly
  fetch of every hex the rooftop antenna itself heard the day before (about 950 a night), and a
  backlog driver (`scripts/backfill_adsblol_routes.py`) streams the historical tarballs.

  `gold.fct_flights_reconciled` is the canonical O/D source. It resolves each flight's
  origin and destination by cross-source consensus, scoped to flights the Japan box is
  actually relevant to: anchored by OpenSky flight-summaries, or with at least one in-box
  states fix inside the flight window (checked against full bronze history, so flights don't
  age out of the mart), since adsb.lol's worldwide chains would otherwise inflate this Japan
  mart about 3x. Windows that fuse a multi-leg rotation under a sticky callsign (longer than
  any real nonstop, or far too slow for their own O/D distance) are rejected before they can
  anchor or vote.

  Endpoints are also feasibility-gated. A jet airliner can't be assigned a short-runway or
  unknown-runway small field (`dim_airports` carries OurAirports runway lengths), so the snap
  resolves to the nearest feasible airport when one exists, and residual infeasible endpoints
  are nullified later. A schedule-derived voter (`dim_vrs_routes`, the community-curated
  Virtual Radar Server route table) supplies the hub pair where position evidence alone can't.
  That schedule vote is scoped the same way SWIM's is: it only fires for routes with at least
  one endpoint inside the observation box (20-50°N, 122-165°E), so a pure overflight, with
  both endpoints outside the box, is left unresolved instead of being stamped with a schedule
  O/D that neither the antenna nor OpenSky ever observed. Multi-stop schedules are exploded
  into adjacent legs before that gate fires, so a tag flight with a layover (Tokyo to Taipei
  to Hong Kong, say) can contribute an applicable adjacent leg instead of being skipped
  entirely. A single surviving leg votes unconditionally, while several surviving legs vote
  only the one the flight's own observed endpoints corroborate, abstaining rather than
  guessing when none do or more than one does.

  Because that consensus mixes observation with inference, every route endpoint records how
  it was derived in `origin_source`/`dest_source`, so any attribution can be audited back to
  its basis and a guess is never mistaken for a sighting:
  - `swim`: filed, and the highest authority, since it is the FAA's own system-of-record plan.
    FAA SWIM TFMData's filed origin and destination, scoped to US-touching flights only. It is
    the one source that can resolve the foreign endpoint on an international leg that neither
    the antenna nor OpenSky's Japan box ever sees. Matched to an airframe by density-scored
    callsign (SWIM carries no Mode-S hex); an ambiguous match is withheld rather than guessed.
    See "FAA SWIM: filed flight plans" above.
  - `opensky_flights`: observed, and ground truth. OpenSky's own arrival/departure
    flight-summary record for this flight.
  - `opensky_states`: observed. A directly seen low-altitude fix at the airport, inside the
    tracked box. Airline-shaped callsigns (`^[A-Z]{3}[0-9]`) only snap to scheduled-service
    airports, so a 787 is never attributed to a military strip; GA and military callsigns
    still snap against the full airport set.
  - `adsblol`: inferred. Two coverage-split segments chained because the boundary groundspeed
    looked like cruise. This can be wrong for a stop the traces never saw, since an aircraft
    that landed and left again inside a gap reads as one continuous flight.
  - `curated`: entered by hand. An evidence-backed row in the `dim_route_overrides` seed,
    applied only where every source left the endpoint NULL, each row carrying its evidence
    string (a FlightAware confirmation, for example).

  Every source with an opinion (FAA SWIM's filed plan, OpenSky flight-summaries, the
  OpenSky-states sessionize-and-snap, and the adsb.lol chain) casts a vote per endpoint, and
  plurality wins. An exact tie prefers a scheduled-service airport for airline-shaped
  callsigns before falling back to the same source-authority order, and is flagged `tiebreak`
  either way rather than silently picked. An endpoint only one source voted on is flagged
  `single`, per endpoint rather than per flight, so a 3-source flight can still be
  origin-`single`. The curated seed still overrides on top. Every flight carries the full vote
  tally and an agreement label (`unanimous`, `majority`, `tiebreak`, `single`, or `curated`)
  per endpoint, so a low-trust resolution stays visible instead of blending in. Consensus
  measurably cuts the same-airport (`RJTT→RJTT`) collapse rate from about 14% under the old
  single-source blend to about 6.7%. A hardened spine merges near-duplicate flight-summary
  anchors for the same physical flight and caps implausibly long single-source anchors, so a
  handful of noisy records can't double-count or fuse two flights into one. Every top-route,
  operator, longest-flight, and daily-airport-movement aggregate now derives from this one
  consensus mart, replacing what used to be two parallel route marts. `fact_flights` stays
  untouched as an input. `gold.fct_flight_legs` remains the single-lane OpenSky-states
  inferred view, sessionized and airport-snapped from the OpenSky-context feed alone with no
  adsb.lol or curated blending, for consumers that want that one source's opinion on its own.

  A companion mart, `gold.fct_flight_path`, fuses the same three position sources — rooftop,
  adsb.lol, and OpenSky — into a per-second trajectory for every reconciled flight, priority
  rooftop over adsb.lol over OpenSky wherever more than one source saw the same second. It
  builds in replaceable daily partitions after a settlement lag, with a rolling repair window
  that absorbs late source loads and recent reconciled-flight re-keys without row mutations.
- **[Virtual Radar Server standing data](https://github.com/vradarserver/standing-data)**
  is the community-curated callsign→route table (consumed via the hourly
  [adsb.lol mirror](https://vrs-standing-data.adsb.lol)) that gives the reconciled mart a
  route opinion independent of position inference.

If you run an ADS-B receiver, feed these networks.

And the open reference datasets that decide how an aircraft is drawn, not whether it appears:

- **[Mictronics readsb database](https://github.com/Mictronics/readsb)**: current ICAO
  operator codes → airline names, the same database tar1090 and adsbexchange render, so
  callsign decoding tracks designator reassignments instead of going stale.
- **[Wikidata](https://www.wikidata.org)**: cross-referenced offline to clean those airline
  names into their public brand forms, baked static into the seed and never queried at
  runtime.
- **[ICAO Doc 8643](https://github.com/rikgale/ICAOList)**: type designators → the silhouette
  each aircraft is drawn with.
- **[tar1090](https://github.com/wiedehopf/tar1090)**: its ICAO 24-bit address → country table
  drives the registration-country flags.
- **[OurAirports](https://ourairports.com/data/)**: airport names, coordinates, and
  scheduled-service classification.

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
[OurAirports](https://ourairports.com/data/), released to the public domain.
