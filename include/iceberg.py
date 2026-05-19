from __future__ import annotations

import os
from typing import Optional

from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError, TableAlreadyExistsError
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import DayTransform
from pyiceberg.types import (
    BooleanType,
    DoubleType,
    IntegerType,
    NestedField,
    StringType,
    TimestamptzType,
)


NAMESPACE = "bronze"
TABLE = "opensky_states"
QUALIFIED = f"{NAMESPACE}.{TABLE}"


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
)

PARTITION_SPEC = PartitionSpec(
    PartitionField(source_id=17, field_id=1000, transform=DayTransform(), name="snapshot_day"),
)


def get_catalog() -> SqlCatalog:
    uri = (
        f"postgresql+psycopg2://"
        f"{os.environ['ANALYTICS_PG_USER']}:{os.environ['ANALYTICS_PG_PASSWORD']}"
        f"@{os.environ['ANALYTICS_PG_HOST']}:{os.environ['ANALYTICS_PG_PORT']}"
        f"/{os.environ['ANALYTICS_PG_DB']}"
    )
    bucket = os.environ.get("S3_BUCKET", "opensky")
    return SqlCatalog(
        "default",
        **{
            "uri": uri,
            "warehouse": f"s3://{bucket}/warehouse",
            "py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO",
            "s3.endpoint": f"http://{os.environ['S3_ENDPOINT']}",
            "s3.access-key-id": os.environ["S3_ACCESS_KEY"],
            "s3.secret-access-key": os.environ["S3_SECRET_KEY"],
            # Garage's configured region; pyarrow validates SigV4 region, s3fs doesn't.
            "s3.region": "garage",
        },
    )


def ensure_namespace_and_table(catalog: Optional[SqlCatalog] = None) -> None:
    cat = catalog or get_catalog()
    try:
        cat.create_namespace(NAMESPACE)
    except NamespaceAlreadyExistsError:
        pass
    try:
        cat.create_table(
            identifier=QUALIFIED,
            schema=SCHEMA,
            partition_spec=PARTITION_SPEC,
        )
    except TableAlreadyExistsError:
        pass
