from __future__ import annotations

from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASETS_DIR = REPO_ROOT / "superset" / "assets" / "datasets" / "analytics"

# superset/ isn't mounted into the airflow runtime image — skip there, like the
# other tests skip when their backing service/dir is absent.
pytestmark = pytest.mark.skipif(
    not DATASETS_DIR.exists(), reason=f"superset assets not present at {DATASETS_DIR}"
)

TRINO_ICEBERG_UUID = "01234567-0000-0000-0000-000000000002"

# v2.7 re-point target per dataset: every mart moves to the iceberg catalog;
# fact_state_snapshots is the only silver table, the three aggs are gold.
EXPECTED_SCHEMA = {
    "agg_country_traffic_2.yaml": "gold",
    "agg_hourly_traffic_3.yaml": "gold",
    "anomalies_2.yaml": "gold",
    "fact_state_snapshots_5.yaml": "silver",
}

# main_dttm_col must survive the re-point untouched — it is the column
# Superset's Trino dialect DATEADDs against.
EXPECTED_MAIN_DTTM = {
    "agg_country_traffic_2.yaml": "snapshot_ts",
    "agg_hourly_traffic_3.yaml": "snapshot_hour",
    "anomalies_2.yaml": "snapshot_time",
    "fact_state_snapshots_5.yaml": "snapshot_time",
}


def _load(name: str) -> dict:
    return yaml.safe_load((DATASETS_DIR / name).read_text())


@pytest.mark.parametrize("name, schema", EXPECTED_SCHEMA.items())
def test_all_v2_datasets_point_at_trino_iceberg(name, schema):
    doc = _load(name)
    assert doc["database_uuid"] == TRINO_ICEBERG_UUID
    assert doc["catalog"] == "iceberg"
    assert doc["schema"] == schema


@pytest.mark.parametrize("name, col", EXPECTED_MAIN_DTTM.items())
def test_main_dttm_cols_unchanged_post_repoint(name, col):
    assert _load(name)["main_dttm_col"] == col
