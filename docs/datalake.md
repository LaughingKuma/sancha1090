# Data lake schema

`sancha1090` is a local-first medallion warehouse on ClickHouse, fed from the Parquet landing
zone in Garage S3. Raw observations land in **bronze**, are conformed in **silver**, and are
aggregated into **gold** marts, most built by dbt-clickhouse and orchestrated by Airflow;
the physical databases are `bronze`, `silver_ch`, `gold_ch`, `dim`. **Column meaning
lives in the dbt yml** (`models/*.yml`, `sources.yml`, `seeds/_seeds.yml`) for dbt-owned and
dbt-read objects, and in the DDL or spec file (`clickhouse/sql/*.sql`,
`include/ch_incremental_mvs.py`) for the rest — never here. The Iceberg/Trino stack this
replaced is described in [`history.md`](history.md).

Two independent live position feeds run side by side:

| Feed | Source | Bronze table | Coverage | Units |
|---|---|---|---|---|
| **Context** | OpenSky `/states/all` REST, one Japan+ocean bbox, every 4 min | `bronze.opensky_states` | Japan and the surrounding ocean, beyond the antenna's horizon | metres, m/s |
| **Rooftop** | A local `readsb` ADS-B antenna, byte-faithful records | `bronze.adsb_states` | the antenna's reception footprint (Tokyo area) | feet, knots |

Two more feeds carry history and filed intent: adsb.lol `globe_history` (ODbL) and FAA SWIM
TFMData filed flight plans.

## Lineage

[`architecture.svg`](architecture.svg) draws every lane from feed to mart with the fusion
seams marked; the layer map below indexes it.

## Layer map

One row per warehouse object. "Built by" names the DAG (with its dbt selector), the
`ch_incremental_mvs.py` spec, `clickhouse-init`, or a seed.

| Object | Layer | Built by |
|---|---|---|
| `bronze.opensky_states` | bronze | `ingest_states` → `tableize_states` |
| `bronze.adsb_states` | bronze | edge push → `ingest_adsb` → `tableize_adsb` |
| `bronze.opensky_flights` | bronze | `ingest_flights` → `tableize_flights` |
| `bronze.swim_flightdata` | bronze | `swim-consumer` → `tableize_swim` |
| `bronze.adsblol_states` | bronze | `scripts/backfill_adsblol_states.py` |
| `bronze.adsblol_flight_segments` | bronze | `ingest_adsblol_routes` |
| `bronze.adsblol_flight_paths` | bronze | `ingest_adsblol_routes` |
| `bronze.aircraft_db` | bronze | `ingest_aircraft_db` (weekly) |
| `bronze.adsbx_aircraft_db` | bronze | `ingest_adsbx_db` (weekly) |
| `bronze.path_estimates` | bronze | livemap (INSERT-only) |
| `dim.dim_hex_country` | dim | `clickhouse-init` DDL + `dim_hex_country` seed |
| `dim.dict_hex_country` | dim | `clickhouse-init` (`range_hashed` dict) |
| `dim.dim_ladd` | dim | `ingest_ladd` (weekly, SCD2) |
| `dim.ladd_pulls` | dim | `ingest_ladd` |
| `dim.dim_vrs_routes` | dim | `ingest_vrs_routes` |
| `silver_ch.dim_airlines` | dim | `dbt seed` |
| `silver_ch.dim_airports` | dim | `dbt seed --full-refresh --select dim_airports` (own compose step: a plain seed fails on the older column set) |
| `silver_ch.dim_aircraft_types` | dim | `dbt seed` |
| `silver_ch.dim_route_overrides` | dim | `dbt seed` |
| `silver_ch.stg_states` | silver | `transform_marts` (untagged) |
| `silver_ch.fact_state_snapshots` | silver | `transform_marts` |
| `silver_ch.int_flight_legs_opensky` | silver | `transform_marts` (`tag:reconcile`) |
| `silver_ch.stg_states_adsblol` | silver | `transform_marts` (`tag:adsblol`) |
| `silver_ch.stg_flight_segments_adsblol` | silver | `transform_marts` (`tag:adsblol`) |
| `silver_ch.int_flight_routes_adsblol` | silver | `transform_marts` (`tag:adsblol`) |
| `silver_ch.int_flight_chains_adsblol` | silver | `transform_marts` (`tag:adsblol`) |
| `silver_ch.stg_vrs_routes` | silver | `transform_marts` (`tag:reconcile`) |
| `silver_ch.int_swim_latest` | silver | `transform_marts` (`tag:swim`) |
| `silver_ch.int_swim_flight` | silver | `transform_marts` (`tag:swim`) |
| `silver_ch.int_swim_diagnostics` | silver | `transform_marts` (`tag:swim`) |
| `silver_ch.int_swim_opinion` | silver | `transform_marts` (`tag:swim`) |
| `silver_ch.int_flight_spine` | silver | `transform_marts` (`tag:reconcile`) |
| `silver_ch.int_flight_opinions` | silver | `transform_marts` (`tag:reconcile`) |
| `silver_ch.int_flight_attach` | silver | `transform_marts` (`tag:reconcile`) |
| `silver_ch.int_flight_attached_votes` | silver | `transform_marts` (`tag:reconcile`) |
| `silver_ch.int_jet_airframes` | silver | `transform_marts` (`tag:reconcile`) |
| `silver_ch.dim_aircraft` | silver | `transform_adsb_silver` (`tag:adsb`) |
| `silver_ch.fct_adsb_state` | silver | `transform_adsb_silver` |
| `silver_ch.int_adsb_callsign_from_opensky` | silver | `transform_adsb_silver` (incremental) |
| `silver_ch.dim_aircraft_registry` | silver | `transform_flights` (`tag:flights`) |
| `silver_ch.swim_latest` | silver | `swim_latest_acc` MV spec |
| `gold_ch.fct_flights_reconciled` | gold | `transform_marts` (`tag:reconcile`) |
| `gold_ch.fct_flight_legs` | gold | `transform_marts` |
| `gold_ch.fct_flight_path` | gold | `transform_marts` |
| `gold_ch.fct_flight_path_summary` | gold | `transform_marts` |
| `gold_ch.fct_flight_recon_tier` | gold | `transform_marts` |
| `gold_ch.fct_flight_flags` | gold | `transform_marts` |
| `gold_ch.fct_est_settlement` | gold | `transform_marts` |
| `gold_ch.agg_est_usage_daily` | gold | `transform_marts` |
| `gold_ch.agg_est_breakdown_daily` | gold | `transform_marts` |
| `gold_ch.agg_route_traffic` | gold | `transform_marts` (`tag:reconcile`) |
| `gold_ch.agg_operator_traffic` | gold | `transform_marts` (`tag:reconcile`) |
| `gold_ch.agg_airport_daily` | gold | `transform_marts` (`tag:reconcile`) |
| `gold_ch.longest_flights` | gold | `transform_marts` (`tag:reconcile`) |
| `gold_ch.agg_country_traffic` | gold | `transform_marts` |
| `gold_ch.anomalies` | gold | `transform_marts` |
| `gold_ch.fact_flights` | gold | `transform_flights` (`tag:flights`) |
| `gold_ch.agg_hourly_traffic` | gold | `agg_hourly_traffic_acc` MV spec |
| `gold_ch.agg_airline_traffic` | gold | `agg_airline_traffic_acc` MV spec |
| `gold_ch.agg_airline_traffic_adsb` | gold | `agg_airline_traffic_adsb_acc` MV spec |
| `gold_ch.agg_country_traffic_adsb` | gold | `agg_country_traffic_adsb_acc` MV spec |
| `gold_ch.ch_mv_seeded` | ops | `ensure_ch_mvs` |

The MV-spec rows are not dbt models: an `AggregatingMergeTree` `*_acc` table fires on each
bronze insert (kept current by `ensure_ch_mvs` in `transform_marts`), and the name in the
table is its merge-aware serving view. Read the view, never the `_acc` state, or counts come
out low. On a fresh deploy `clickhouse-marts-init` loads the seeds then the dictionary, before
the first `transform_marts` run.

## Join rules worth knowing

Most joins are an equality on `icao24`/`hex`. Three are not:

- **Cross-feed identity.** The rooftop `hex` and the OpenSky `icao24` are the same Mode-S
  address in different case: join on `hex = lower(icao24)`.
- **Airline from callsign.** `dim_airlines.icao = substr(trim(callsign), 1, 3)` only applies
  behind a `^[A-Z]{3}[0-9]` guard — without it, GA tail numbers match airline codes.
- **Registration country.** `hex` → country is a range lookup: the hex is parsed to an integer
  and matched against `[block_lo, block_hi]` through the `dim.dict_hex_country` `range_hashed`
  dictionary (macro `ch_hex_country`).

## Endpoint feasibility

`fct_flights_reconciled` endpoints are feasibility-gated: a jet airliner cannot be assigned a
short-runway or unknown-runway small field (`dim_airports` carries OurAirports runway
lengths), so the snap resolves to the nearest feasible airport when one exists and residual
infeasible endpoints are nullified. A flight where the gate discarded a source's endpoint
carries `feasibility_gated = 1`, so a consensus over a reduced ballot is auditable rather
than silent.

## Trajectories

`gold_ch.fct_flight_path` fuses the three position sources into a per-second trajectory for
every reconciled flight, rooftop over adsb.lol over OpenSky wherever more than one saw the
same second. It builds in replaceable daily partitions after a settlement lag
(`path_settlement_lag_days`), with a rolling repair window that absorbs late source loads and
reconciled-flight re-keys without row mutations. The livemap's `/path` applies the same
fusion, pad, and overlap contest at serve time for a flight whose start day has not reached
the settlement build, marking that response `provisional: true`.

## Estimate serving exhaust

`bronze.path_estimates` is append-only: one request row (non-results logged too) plus one row
per emitted segment, kept 24 months. Flight-keyed requests carry the flight id and the hex;
the flight id is a build-generation hash that does not survive settlement, so the hex is the
durable settlement key. Live requests are hex-keyed (`flight_id` NULL,
`subject_key = 'h:<icao24>'`), and `producer` names the serving instance. Estimate geometry never enters the
flight marts: the gold footprint is serving telemetry only.

`gold_ch.fct_est_settlement` re-keys every served gap segment to `fct_flight_path` (hex plus
time overlap; entry∩exit anchor intersection for legacy rows) and scores the estimate polyline
against coverage that arrived after serving — per-point errors as scalar arrays, no coordinate
columns, full recompute. Scores move as truth arrives and paths repair, so never alert on
day-over-day deltas. Ambiguous re-keys are excluded but counted (`skip_ambiguous`).

The standing drift read pools per-point errors deduped to unique inputs — never a median of
medians, which measurably hides drift — grouped by `config_hash`:

```sql
SELECT uncertainty_bin, config_hash, count() AS settled_segments,
       uniqExact(input_fingerprint) AS subjects,
       arraySort(groupArrayArray(errs_km)) AS pool,
       if(length(pool) = 0, NULL, pool[toUInt32(ceil(0.5 * length(pool)))]) AS pooled_p50_km,
       if(length(pool) = 0, NULL, pool[toUInt32(ceil(0.9 * length(pool)))]) AS pooled_p90_km,
       any(served_p50_km) AS band_p50, any(served_p90_km) AS band_p90
FROM (SELECT * FROM gold_ch.fct_est_settlement
      WHERE skip_ambiguous = 0 AND settled = 1
      ORDER BY computed_at, estimate_id LIMIT 1 BY input_fingerprint, seg_idx)
GROUP BY uncertainty_bin, config_hash ORDER BY uncertainty_bin, config_hash
```

The `gap_180m_plus` band keeps a `≥` floor rather than a calibrated percentile: the
fully-covered long-duration population is loiter orbits, disjoint from the transpacific
cruise gaps the band serves.

## Known limitations

- **Inferred routes are not ground truth.** `fct_flight_legs.route_inferred` snaps to the
  nearest in-coverage airport, not the actual runway, and is a raw single-source snap;
  `fct_flights_reconciled` is the canonical answer.
- **One-sided international endpoints.** A flight that leaves OpenSky-states coverage
  mid-ocean, or has only one side inside the tracked box, resolves one endpoint and NULLs the
  other. Those are excluded from `agg_route_traffic`, skewing it toward domestic routes.
- **Sparse airframe enrichment.** `dim_aircraft` only holds airframes the rooftop has heard;
  `dim_aircraft_registry` backfills registration and typecode where it can.
- **Cross-feed lag.** `fct_flight_legs` geometry refreshes on the `transform_marts` 10-minute
  cron, but its rooftop enrichment can lag a rooftop tick behind.
- **`agg_country_traffic` is point-in-time** — a latest-5-minute snapshot, not full history.
- **Backfilled callsigns are nearest-snapshot.** A small share of `fct_adsb_state` rows take
  their callsign from the OpenSky feed (`callsign_source = 'opensky_backfill'`); at a
  turnaround inside the ±10-minute window the nearest snapshot can carry the adjacent leg's
  callsign; `callsign_source` lets consumers exclude them.
- **SWIM resolves only US-touching flights, by inferred identity.** `int_swim_opinion` matches
  a filed callsign to an airframe hex by sighting density, not a direct identifier; ambiguous
  matches are withheld, so its vote share is smaller than its message volume.
- **LADD suppression is display-time only, public-instance only, and never data deletion.**
  The semantics of the flag, including the open-interval asymmetry, are in the `is_ladd`
  description in `dbt/sancha1090/models/marts/_reconciled.yml`.
