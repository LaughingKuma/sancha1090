from __future__ import annotations

from typing import Optional

from pyiceberg.catalog import Catalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.transforms import DayTransform
from pyiceberg.types import (
    BooleanType,
    DoubleType,
    IntegerType,
    NestedField,
    StringType,
    TimestamptzType,
)

from include.flights_iceberg import TABLE_PROPERTIES
from include.iceberg_rest import get_polaris_catalog


NAMESPACE = "bronze"
TABLE = "archive_states"
QUALIFIED = f"{NAMESPACE}.{TABLE}"

# Mirrors bronze.opensky_states fields 1-20 so dbt history models union with the live
# lane column-for-column; field 21 keeps non-OpenSky provenance explicit (ODbL adsb.lol).
# Field ids are append-only; never renumber once data is written.
SCHEMA = Schema(
    NestedField(1, "icao24", StringType(), required=False),
    NestedField(2, "callsign", StringType(), required=False),
    NestedField(3, "origin_country", StringType(), required=False),
    NestedField(4, "time_position", TimestamptzType(), required=False),
    NestedField(5, "last_contact", TimestamptzType(), required=False),
    NestedField(6, "longitude", DoubleType(), required=False),
    NestedField(7, "latitude", DoubleType(), required=False),
    NestedField(8, "baro_altitude", DoubleType(), required=False),
    NestedField(9, "on_ground", BooleanType(), required=False),
    NestedField(10, "velocity", DoubleType(), required=False),
    NestedField(11, "true_track", DoubleType(), required=False),
    NestedField(12, "vertical_rate", DoubleType(), required=False),
    NestedField(13, "geo_altitude", DoubleType(), required=False),
    NestedField(14, "squawk", StringType(), required=False),
    NestedField(15, "spi", BooleanType(), required=False),
    NestedField(16, "position_source", IntegerType(), required=False),
    NestedField(17, "snapshot_time", TimestamptzType(), required=False),
    NestedField(18, "region", StringType(), required=False),
    NestedField(19, "ingested_at", TimestamptzType(), required=False),
    NestedField(20, "committed_at", TimestamptzType(), required=False),
    NestedField(21, "source", StringType(), required=False),
)

PARTITION_SPEC = PartitionSpec(
    PartitionField(source_id=17, field_id=1000, transform=DayTransform(), name="snapshot_day"),
)


def get_catalog() -> Catalog:
    return get_polaris_catalog()


def ensure_archive_table(catalog: Optional[Catalog] = None) -> Table:
    cat = catalog or get_catalog()
    cat.create_namespace_if_not_exists(NAMESPACE)
    return cat.create_table_if_not_exists(
        QUALIFIED,
        schema=SCHEMA,
        partition_spec=PARTITION_SPEC,
        properties=TABLE_PROPERTIES,
    )
