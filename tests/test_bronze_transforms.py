from __future__ import annotations

import polars as pl

from include import bronze_transforms as bt


def _raw_states_row() -> dict:
    return {
        "icao24": "abc123", "callsign": "ANA123  ", "origin_country": "Japan",
        "time_position": 1_716_000_000, "last_contact": 1_716_000_010,
        "longitude": 139.7, "latitude": 35.6, "baro_altitude": 1000.0,
        "on_ground": False, "velocity": 200.0, "true_track": 90.0,
        "vertical_rate": 0.0, "geo_altitude": 1050.0, "squawk": "1200",
        "spi": False, "position_source": 0, "snapshot_time": 1_716_000_005,
        "region": "east_asia", "ingested_at": "2026-05-18T00:00:05Z",
    }


def test_transform_states_frame_columns_dtypes_and_callsign_trim():
    df = pl.DataFrame([_raw_states_row(), {**_raw_states_row(), "callsign": "   "}])
    out = bt.transform_states_frame(df)

    assert out.columns == bt.STATES_COLUMNS
    for col in ("snapshot_time", "last_contact", "time_position", "ingested_at", "committed_at"):
        assert out.schema[col] == pl.Datetime(time_unit="us", time_zone="UTC"), col
    assert out.schema["position_source"] == pl.Int32
    assert out["callsign"].to_list() == ["ANA123", None]  # trimmed; all-blank → null


def test_transform_flights_duration_uses_raw_epoch_seconds():
    raw = {
        "icao24": "abc123", "callsign": "JAL5 ", "first_seen": 1_716_000_000,
        "last_seen": 1_716_003_600, "est_departure_airport": "RJTT",
        "est_arrival_airport": "RJOO", "captured_for_airport": "RJTT",
        "direction": "departure", "window_kind": "d0", "ingested_at": "2026-05-18T00:00:00Z",
    }
    out = bt.transform_flights_frame(pl.DataFrame([raw]))

    assert out.columns == bt.FLIGHTS_COLUMNS
    assert out["flight_duration_seconds"].to_list() == [3600]  # last_seen - first_seen, seconds
    assert out.schema["flight_duration_seconds"] == pl.Int32
    assert out.schema["first_seen"] == pl.Datetime(time_unit="us", time_zone="UTC")
    assert out["callsign"].to_list() == ["JAL5"]


def test_flights_columns_cover_ingest_columns():
    # transform_flights_frame.select() indexes the frame by FLIGHTS_COLUMNS — every raw column the
    # ingest DAG writes (plus the derived/commit columns) must exist in the contract.
    ingest_columns = {
        "icao24", "callsign", "first_seen", "last_seen",
        "est_departure_airport", "est_arrival_airport",
        "captured_for_airport", "direction", "window_kind", "ingested_at",
    }
    assert ingest_columns <= set(bt.FLIGHTS_COLUMNS)
    assert {"flight_duration_seconds", "committed_at"} <= set(bt.FLIGHTS_COLUMNS)


def test_aircraft_db_columns_cover_csv_subset():
    assert set(bt.AIRCRAFT_DB_CSV_COLUMNS) <= set(bt.AIRCRAFT_DB_COLUMNS)
    assert {"as_of_date", "ingested_at", "committed_at"} <= set(bt.AIRCRAFT_DB_COLUMNS)
