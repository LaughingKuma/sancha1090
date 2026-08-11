{{ config(materialized='table', tags=['adsblol']) }}

-- Backfilled pre-pipeline history (adsb.lol globe_history, Japan-only at write time).
-- No 30-day filter: this table IS the deep past; it only changes when a backfill
-- wave runs, and it stays small (12-min snapshots, Japan box only).
-- region='japan' only: the full-resolution kanto/kansai rows would distort the
-- per-snapshot trend counts — they're raw material for density/path work, not trends.
{{ states_staging_body(source('bronze', 'adsblol_states'), "region = 'japan'", extra_cols=['source']) }}
