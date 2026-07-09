from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import polars as pl

# Single source of truth for the SWIM bronze column contract — the drain module owns it, don't re-list it.
from include.swim_consumer import _BRONZE_COLS as _SWIM_BRONZE_COLS


# Column order = the bronze landing contract; the CH loader selects by this list so the
# byte-mirror table and the per-tick load can't drift.
STATES_COLUMNS = [
    "icao24", "callsign", "origin_country", "time_position", "last_contact",
    "longitude", "latitude", "baro_altitude", "on_ground", "velocity",
    "true_track", "vertical_rate", "geo_altitude", "squawk", "spi",
    "position_source", "snapshot_time", "region", "ingested_at", "committed_at",
]


# Shared by the per-tick CH load and the adsb.lol backfill loader so the two paths can't drift on the transform.
def transform_states_frame(df: pl.DataFrame) -> pl.DataFrame:
    callsign_trim = pl.col("callsign").str.strip_chars()
    df = df.with_columns(
        pl.from_epoch(pl.col("snapshot_time"), time_unit="s")
            .dt.replace_time_zone("UTC").alias("snapshot_time"),
        pl.from_epoch(pl.col("last_contact"), time_unit="s")
            .dt.replace_time_zone("UTC").alias("last_contact"),
        pl.from_epoch(pl.col("time_position"), time_unit="s")
            .dt.replace_time_zone("UTC").alias("time_position"),
        pl.col("ingested_at").str.to_datetime(time_zone="UTC").alias("ingested_at"),
        pl.col("position_source").cast(pl.Int32),
        pl.when(callsign_trim == "").then(None).otherwise(callsign_trim).alias("callsign"),
        pl.lit(datetime.now(timezone.utc)).alias("committed_at"),
    )
    return df.select(STATES_COLUMNS)


FLIGHTS_COLUMNS = [
    "icao24", "callsign", "first_seen", "last_seen", "est_departure_airport",
    "est_arrival_airport", "flight_duration_seconds", "captured_for_airport",
    "direction", "window_kind", "ingested_at", "committed_at",
]

RAW_FLIGHTS_SCHEMA = {
    "icao24": pl.Utf8,
    "callsign": pl.Utf8,
    "first_seen": pl.Int64,
    "last_seen": pl.Int64,
    "est_departure_airport": pl.Utf8,
    "est_arrival_airport": pl.Utf8,
    "captured_for_airport": pl.Utf8,
    "direction": pl.Utf8,
    "window_kind": pl.Utf8,
}


# Duration subtracts raw epoch ints (one with_columns), not the new datetimes.
def transform_flights_frame(df: pl.DataFrame) -> pl.DataFrame:
    callsign_trim = pl.col("callsign").str.strip_chars()
    df = df.with_columns(
        pl.from_epoch(pl.col("first_seen"), time_unit="s")
            .dt.replace_time_zone("UTC").alias("first_seen"),
        pl.from_epoch(pl.col("last_seen"), time_unit="s")
            .dt.replace_time_zone("UTC").alias("last_seen"),
        (pl.col("last_seen") - pl.col("first_seen")).cast(pl.Int32)
            .alias("flight_duration_seconds"),
        pl.col("ingested_at").str.to_datetime(time_zone="UTC").alias("ingested_at"),
        pl.when(callsign_trim == "").then(None).otherwise(callsign_trim).alias("callsign"),
        pl.lit(datetime.now(timezone.utc)).alias("committed_at"),
    )
    return df.select(FLIGHTS_COLUMNS)


def transform_swim_frame(df: pl.DataFrame) -> pl.DataFrame:
    # Fail loud on contract drift: a silently-dropped column would insert as all-NULL in bronze.
    missing = [c for c in _SWIM_BRONZE_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"swim frame missing bronze columns: {missing}")
    return df.select(_SWIM_BRONZE_COLS)


def flight_row(f: dict[str, Any], icao: str, direction: str, window_kind: str) -> dict[str, Any]:
    return {
        "icao24": f.get("icao24"),
        "callsign": f.get("callsign"),
        "first_seen": f.get("firstSeen"),
        "last_seen": f.get("lastSeen"),
        "est_departure_airport": f.get("estDepartureAirport"),
        "est_arrival_airport": f.get("estArrivalAirport"),
        "captured_for_airport": icao,
        "direction": direction,
        "window_kind": window_kind,
    }


# Subset of OpenSky's aircraft-database.csv columns we keep (identity + operator layer;
# dim_aircraft_types already covers type -> silhouette).
AIRCRAFT_DB_CSV_COLUMNS = [
    "icao24", "registration", "manufacturericao", "manufacturername", "model",
    "typecode", "serialnumber", "icaoaircrafttype", "operator", "operatorcallsign",
    "operatoricao", "owner",
]

# The CSV subset plus the derived/commit columns; the aircraft_db load selects by this order.
AIRCRAFT_DB_COLUMNS = AIRCRAFT_DB_CSV_COLUMNS + ["as_of_date", "ingested_at", "committed_at"]
