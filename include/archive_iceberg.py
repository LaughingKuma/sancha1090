from __future__ import annotations

from typing import Optional

from pyiceberg.catalog import Catalog
from pyiceberg.schema import Schema
from pyiceberg.table import Table
from pyiceberg.types import NestedField, StringType

from include import iceberg as states_iceberg
from include.flights_iceberg import TABLE_PROPERTIES
from include.iceberg_rest import get_polaris_catalog


NAMESPACE = "bronze"
TABLE = "archive_states"
QUALIFIED = f"{NAMESPACE}.{TABLE}"

# Mirrors bronze.opensky_states fields 1-20 so dbt history models union with the live
# lane column-for-column; field 21 keeps non-OpenSky provenance explicit (ODbL adsb.lol).
SCHEMA = Schema(
    *states_iceberg.SCHEMA.fields,
    NestedField(21, "source", StringType(), required=False),
)

PARTITION_SPEC = states_iceberg.PARTITION_SPEC


def ensure_archive_table(catalog: Optional[Catalog] = None) -> Table:
    cat = catalog or get_polaris_catalog()
    cat.create_namespace_if_not_exists(NAMESPACE)
    return cat.create_table_if_not_exists(
        QUALIFIED,
        schema=SCHEMA,
        partition_spec=PARTITION_SPEC,
        properties=TABLE_PROPERTIES,
    )
