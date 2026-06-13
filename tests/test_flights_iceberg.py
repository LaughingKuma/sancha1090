from __future__ import annotations

from include import flights_iceberg as fib


def test_ensure_flights_table_is_idempotent(local_catalog):
    t1 = fib.ensure_flights_table(local_catalog)
    t2 = fib.ensure_flights_table(local_catalog)
    assert local_catalog.table_exists(fib.FLIGHTS_QUALIFIED)
    assert [f.name for f in t1.schema().fields] == [f.name for f in t2.schema().fields]


def test_ensure_aircraft_db_table_is_idempotent(local_catalog):
    t1 = fib.ensure_aircraft_db_table(local_catalog)
    t2 = fib.ensure_aircraft_db_table(local_catalog)
    assert local_catalog.table_exists(fib.AIRCRAFT_DB_QUALIFIED)
    assert [f.name for f in t1.schema().fields] == [f.name for f in t2.schema().fields]


def test_flights_schema_covers_ingest_columns():
    # The tableize select() indexes the frame by schema field names — every raw column
    # the ingest DAG writes (plus the derived/commit columns) must exist in the DDL.
    ingest_columns = {
        "icao24", "callsign", "first_seen", "last_seen",
        "est_departure_airport", "est_arrival_airport",
        "captured_for_airport", "direction", "window_kind", "ingested_at",
    }
    schema_columns = {f.name for f in fib.FLIGHTS_SCHEMA.fields}
    assert ingest_columns <= schema_columns
    assert {"flight_duration_seconds", "committed_at"} <= schema_columns


def test_aircraft_db_schema_covers_csv_subset():
    schema_columns = {f.name for f in fib.AIRCRAFT_DB_SCHEMA.fields}
    assert set(fib.AIRCRAFT_DB_CSV_COLUMNS) <= schema_columns
    assert {"as_of_date", "ingested_at", "committed_at"} <= schema_columns


def test_all_columns_nullable():
    # API rows routinely miss fields (estDepartureAirport ~34-59% filled); the registry
    # CSV is mostly blanks outside icao24 — everything lands nullable.
    assert all(not f.required for f in fib.FLIGHTS_SCHEMA.fields)
    assert all(not f.required for f in fib.AIRCRAFT_DB_SCHEMA.fields)
