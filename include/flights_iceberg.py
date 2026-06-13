from __future__ import annotations

from typing import Any, Optional

import polars as pl
from pyiceberg.catalog import Catalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.transforms import DayTransform
from pyiceberg.types import (
    DateType,
    IntegerType,
    NestedField,
    StringType,
    TimestamptzType,
)

from include.iceberg_rest import get_polaris_catalog


NAMESPACE = "bronze"

FLIGHTS_TABLE = "opensky_flights"
FLIGHTS_QUALIFIED = f"{NAMESPACE}.{FLIGHTS_TABLE}"

AIRCRAFT_DB_TABLE = "aircraft_db"
AIRCRAFT_DB_QUALIFIED = f"{NAMESPACE}.{AIRCRAFT_DB_TABLE}"


# Field ids are append-only; never renumber once data is written.
FLIGHTS_SCHEMA = Schema(
    NestedField(1, "icao24", StringType(), required=False),
    NestedField(2, "callsign", StringType(), required=False),
    NestedField(3, "first_seen", TimestamptzType(), required=False),
    NestedField(4, "last_seen", TimestamptzType(), required=False),
    NestedField(5, "est_departure_airport", StringType(), required=False),
    NestedField(6, "est_arrival_airport", StringType(), required=False),
    NestedField(7, "flight_duration_seconds", IntegerType(), required=False),
    NestedField(8, "captured_for_airport", StringType(), required=False),
    NestedField(9, "direction", StringType(), required=False),
    # 'd0' = same-day departures (fresh), 'd2' = lagged authoritative window —
    # OpenSky's flight summaries only fully populate ~48h after the fact.
    NestedField(10, "window_kind", StringType(), required=False),
    NestedField(11, "ingested_at", TimestamptzType(), required=False),
    NestedField(12, "committed_at", TimestamptzType(), required=False),
)

FLIGHTS_PARTITION_SPEC = PartitionSpec(
    PartitionField(source_id=3, field_id=1000, transform=DayTransform(), name="first_seen_day"),
)

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
# dim_aircraft_types already covers type → silhouette).
AIRCRAFT_DB_CSV_COLUMNS = [
    "icao24", "registration", "manufacturericao", "manufacturername", "model",
    "typecode", "serialnumber", "icaoaircrafttype", "operator", "operatorcallsign",
    "operatoricao", "owner",
]

AIRCRAFT_DB_SCHEMA = Schema(
    NestedField(1, "icao24", StringType(), required=False),
    NestedField(2, "registration", StringType(), required=False),
    NestedField(3, "manufacturericao", StringType(), required=False),
    NestedField(4, "manufacturername", StringType(), required=False),
    NestedField(5, "model", StringType(), required=False),
    NestedField(6, "typecode", StringType(), required=False),
    NestedField(7, "serialnumber", StringType(), required=False),
    NestedField(8, "icaoaircrafttype", StringType(), required=False),
    NestedField(9, "operator", StringType(), required=False),
    NestedField(10, "operatorcallsign", StringType(), required=False),
    NestedField(11, "operatoricao", StringType(), required=False),
    NestedField(12, "owner", StringType(), required=False),
    NestedField(13, "as_of_date", DateType(), required=False),
    NestedField(14, "ingested_at", TimestamptzType(), required=False),
    NestedField(15, "committed_at", TimestamptzType(), required=False),
)

AIRCRAFT_DB_PARTITION_SPEC = PartitionSpec(
    PartitionField(source_id=13, field_id=1000, transform=DayTransform(), name="as_of_day"),
)

TABLE_PROPERTIES = {
    "write.format.default": "parquet",
    "write.parquet.compression-codec": "zstd",
    "write.metadata.compression-codec": "gzip",
    "format-version": "2",
}


def ensure_flights_table(catalog: Optional[Catalog] = None) -> Table:
    """Idempotent; bronze namespace pre-exists in Polaris (v2.1 bootstrap)."""
    cat = catalog or get_polaris_catalog()
    cat.create_namespace_if_not_exists(NAMESPACE)
    return cat.create_table_if_not_exists(
        FLIGHTS_QUALIFIED,
        schema=FLIGHTS_SCHEMA,
        partition_spec=FLIGHTS_PARTITION_SPEC,
        properties=TABLE_PROPERTIES,
    )


def ensure_aircraft_db_table(catalog: Optional[Catalog] = None) -> Table:
    """Idempotent; bronze namespace pre-exists in Polaris (v2.1 bootstrap)."""
    cat = catalog or get_polaris_catalog()
    cat.create_namespace_if_not_exists(NAMESPACE)
    return cat.create_table_if_not_exists(
        AIRCRAFT_DB_QUALIFIED,
        schema=AIRCRAFT_DB_SCHEMA,
        partition_spec=AIRCRAFT_DB_PARTITION_SPEC,
        properties=TABLE_PROPERTIES,
    )
